import asyncio
import logging
import os
import socket
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from io import BytesIO
from typing import List, Optional

import aiohttp
import pytz
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command
from aiogram.utils.backoff import BackoffConfig
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

import db
from llm_parser import (
    TransactionExtraction,
    get_llm,
    parse_transactions,
)
from table_renderer import format_rupiah, render_daily_table, render_monthly_table

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Redam warning reconnect polling rutin dari ISP agar log tetap bersih
logging.getLogger("aiogram.dispatcher").setLevel(logging.CRITICAL)
logger = logging.getLogger("hematuang_bot")

WIB = pytz.timezone("Asia/Jakarta")

dp = Dispatcher()


def get_today_str() -> str:
    """Mengembalikan tanggal hari ini dalam format YYYY-MM-DD sesuai zona waktu WIB."""
    return datetime.now(WIB).strftime("%Y-%m-%d")


def get_main_action_keyboard() -> InlineKeyboardMarkup:
    """
    Menu tombol interaktif yang selalu muncul setelah bot membalas chat/mencatat transaksi.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Lanjut Catat", callback_data="menu:lanjut"),
                InlineKeyboardButton(text="📊 Rekap Hari Ini", callback_data="menu:rekap"),
            ],
            [
                InlineKeyboardButton(text="🗑️ Hapus Transaksi", callback_data="menu:hapus"),
                InlineKeyboardButton(text="📆 Rekap Bulanan", callback_data="menu:bulanan"),
            ],
            [
                InlineKeyboardButton(text="🛑 Stop / Tutup Buku", callback_data="menu:stop"),
                InlineKeyboardButton(text="📖 Panduan (/help)", callback_data="menu:help"),
            ],
        ]
    )


def get_stopped_keyboard() -> InlineKeyboardMarkup:
    """Menu tombol saat sesi sedang berstatus STOP / CLOSED (data tetap tersimpan aman)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Lanjut Catat (Data Tetap Ada)",
                    callback_data="menu:lanjut",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="▶️ Start / Buka Kembali Sesi",
                    callback_data="menu:start",
                ),
            ],
            [
                InlineKeyboardButton(text="📊 Rekap Hari Ini", callback_data="menu:rekap"),
                InlineKeyboardButton(text="📆 Rekap Bulanan", callback_data="menu:bulanan"),
            ],
            [
                InlineKeyboardButton(text="📖 Panduan (/help)", callback_data="menu:help"),
            ],
        ]
    )


async def send_daily_recap_image(
    bot: Bot,
    chat_id: int,
    date_str: str,
    caption: str,
    reply_to_message_id: Optional[int] = None,
    disable_notification: bool = False,
    with_menu: bool = True,
    custom_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Mengambil transaksi dari database, merender tabel PNG, dan mengirimkannya ke chat beserta menu."""
    transactions = await db.get_transactions_by_date(date_str, chat_id)
    img_buf = await asyncio.to_thread(render_daily_table, date_str, transactions)
    png_bytes = img_buf.getvalue()
    markup = custom_markup if custom_markup is not None else (get_main_action_keyboard() if with_menu else None)

    for attempt in range(3):
        try:
            photo_file = BufferedInputFile(png_bytes, filename=f"rekap_{date_str}.png")
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo_file,
                caption=caption,
                reply_to_message_id=reply_to_message_id,
                disable_notification=disable_notification,
                reply_markup=markup,
            )
            return
        except TelegramNetworkError:
            if attempt == 2:
                raise
            await asyncio.sleep(0.5)


async def build_final_closing_caption(date_str: str, chat_id: int) -> str:
    """
    Menyusun caption laporan penutupan harian lengkap dengan:
    - Ringkasan Masuk, Keluar, & Selisih hari tersebut
    - Kategori pengeluaran terbesar hari tersebut
    - Akumulasi saldo bulan berjalan (tanggal 1 s/d hari tersebut)
    """
    transactions = await db.get_transactions_by_date(date_str, chat_id)
    day_income = 0.0
    day_expense = 0.0
    expense_by_cat = defaultdict(float)

    for tx in transactions:
        amt = float(tx["amount"])
        if tx["type"] == "INCOME":
            day_income += amt
        else:
            day_expense += amt
            expense_by_cat[tx["category"]] += amt

    day_net = day_income - day_expense
    top_cat_line = ""
    if expense_by_cat:
        top_cat, top_amt = max(expense_by_cat.items(), key=lambda x: x[1])
        top_cat_line = f"• <b>Pengeluaran Terbesar:</b> {top_cat} ({format_rupiah(top_amt)})\n"

    year_month = date_str[:7]
    monthly = await db.get_monthly_summary(year_month, chat_id)
    m_inc = monthly["total_income"]
    m_exp = monthly["total_expense"]
    m_net = m_inc - m_exp

    return (
        f"🔒 <b>Tutup Buku Harian — {date_str}</b>\n\n"
        f"📌 <b>Ringkasan Tanggal {date_str}:</b>\n"
        f"• <b>Total Masuk:</b> {format_rupiah(day_income)}\n"
        f"• <b>Total Keluar:</b> {format_rupiah(day_expense)}\n"
        f"• <b>Selisih Hari Ini:</b> {format_rupiah(day_net)}\n"
        f"{top_cat_line}\n"
        f"📅 <b>Akumulasi Bulan Ini ({year_month}):</b>\n"
        f"• <b>Pemasukan Bulan Ini:</b> {format_rupiah(m_inc)}\n"
        f"• <b>Pengeluaran Bulan Ini:</b> {format_rupiah(m_exp)}\n"
        f"• <b>Sisa / Saldo Bulan Ini:</b> {format_rupiah(m_net)}\n\n"
        f"⚠️ <i>Sesi tanggal {date_str} sedang di-STOP (transaksi Anda tetap tersimpan aman). Tekan tombol <b>➕ Lanjut Catat</b> atau ketik <b>/lanjut</b> / <b>/start</b> untuk menambah catatan lagi.</i>"
    )


async def process_and_record_transactions(
    message: Message,
    bot: Bot,
    extracted_list: List[TransactionExtraction],
) -> None:
    """
    Memastikan sesi harian tersedia, menyimpan satu atau banyak transaksi sekaligus
    (hasil split_items dari llm_parser), lalu mengirim gambar rekap terbaru + menu.
    """
    if not extracted_list:
        await message.reply(
            "⚠️ Tidak ada transaksi yang dapat dikenali dari pesan tersebut.",
            reply_markup=get_main_action_keyboard(),
        )
        return

    chat_id = message.chat.id
    today_str = get_today_str()

    # Cek apakah session_date hari ini sudah ada
    is_new_session = await db.ensure_daily_session(today_str, chat_id)
    if is_new_session:
        await message.answer(
            f"📅 <b>Memulai catatan keuangan untuk tanggal {today_str}</b>\n"
            f"Semua transaksi hari ini akan tercatat dalam sesi ini."
        )

    # Simpan seluruh transaksi hasil ekstraksi ke database (otomatis terenkripsi)
    for extracted in extracted_list:
        await db.insert_transaction(
            date_str=today_str,
            chat_id=chat_id,
            tx_type=extracted.type,
            amount=extracted.amount,
            category=extracted.category,
            description=extracted.description,
        )

    if len(extracted_list) == 1:
        item = extracted_list[0]
        tipe_indo = "Pemasukan (INCOME)" if item.type == "INCOME" else "Pengeluaran (EXPENSE)"
        caption = (
            f"✅ <b>Transaksi Berhasil Dicatat!</b>\n"
            f"• <b>Tipe:</b> {tipe_indo}\n"
            f"• <b>Kategori:</b> {item.category}\n"
            f"• <b>Keterangan:</b> {item.description}\n"
            f"• <b>Nominal:</b> {format_rupiah(item.amount)}\n\n"
            f"👇 <i>Silakan kirim transaksi lagi atau pilih menu di bawah:</i>"
        )
    else:
        lines = [f"✅ <b>{len(extracted_list)} Transaksi Berhasil Dicatat Sekaligus!</b>"]
        for idx, item in enumerate(extracted_list, start=1):
            icon = "🟢" if item.type == "INCOME" else "🔴"
            lines.append(
                f"{idx}. {icon} <b>{item.description}</b> ({item.category}) — <b>{format_rupiah(item.amount)}</b>"
            )
        lines.append("\n👇 <i>Silakan kirim transaksi lagi atau pilih menu di bawah:</i>")
        caption = "\n".join(lines)

    await send_daily_recap_image(
        bot=bot,
        chat_id=chat_id,
        date_str=today_str,
        caption=caption,
        reply_to_message_id=message.message_id,
        with_menu=True,
    )


# =====================================================================
# HELPER ACTIONS (Dipakai bersama oleh Command "/" & Tombol Menu Inline)
# =====================================================================

def get_help_text() -> str:
    return (
        "📖 <b>PANDUAN PENGGUNAAN HEMATUANG BOT</b>\n\n"
        "<b>1️⃣ Mencatat lewat Teks Chat</b>\n"
        "Tidak perlu format kaku! Cukup ketik seperti chat biasa, AI akan mengenali pemasukan atau pengeluaran:\n"
        "• <code>Makan siang soto ayam 25rb</code>\n"
        "• <code>Bayar listrik 350.000</code>\n"
        "• <code>Dapat bonus proyek 1,5jt</code>\n"
        "<i>(Mendukung singkatan: rb, ribu, k, jt, juta)</i>\n\n"
        "<b>2️⃣ Mencatat lewat Foto Struk / Bukti Transfer</b>\n"
        "Kirimkan foto struk belanja atau screenshot bukti transfer (boleh ditambah caption). Bot otomatis membaca total nominalnya.\n\n"
        "<b>3️⃣ Daftar Perintah (Ketik / di chat)</b>\n"
        "• /start — Tampilkan menu utama\n"
        "• /lanjut — Lanjut mencatat transaksi baru\n"
        "• /rekap — Lihat tabel rekap transaksi hari ini\n"
        "• /bulanan — Lihat tabel rekap & rincian kategori bulan ini\n"
        "• /hapus — Pilih & hapus transaksi tertentu hari ini\n"
        "• /restart — Reset/ulang semua catatan hari ini dari nol\n"
        "• /stop atau /tutup — Stop & tutup buku hari ini\n"
        "• /help — Tampilkan panduan penggunaan ini\n\n"
        "<b>4️⃣ Keamanan & Tutup Buku Otomatis (00:00 WIB)</b>\n"
        "• Seluruh catatan & kategori Anda <b>dienkripsi (AES-Fernet)</b> di database.\n"
        "• Setiap pukul 00:00 WIB, bot otomatis menutup buku hari sebelumnya (tanpa suara notifikasi) dan mengirim rekap final."
    )


async def do_rekap_action(bot: Bot, chat_id: int, reply_to_message_id: Optional[int] = None) -> None:
    today_str = get_today_str()
    transactions = await db.get_transactions_by_date(today_str, chat_id)

    if not transactions:
        await bot.send_message(
            chat_id=chat_id,
            text=f"📭 Belum ada transaksi yang tercatat untuk hari ini (<b>{today_str}</b>).\nSilakan kirim teks atau foto transaksi Anda.",
            reply_to_message_id=reply_to_message_id,
            reply_markup=get_main_action_keyboard(),
        )
        return

    await send_daily_recap_image(
        bot=bot,
        chat_id=chat_id,
        date_str=today_str,
        caption=f"📊 <b>Rekap Transaksi Hari Ini ({today_str})</b>\n👇 <i>Pilih menu aksi di bawah:</i>",
        reply_to_message_id=reply_to_message_id,
        with_menu=True,
    )


async def do_bulanan_action(bot: Bot, chat_id: int, reply_to_message_id: Optional[int] = None) -> None:
    year_month = datetime.now(WIB).strftime("%Y-%m")
    summary = await db.get_monthly_summary(year_month, chat_id)

    if summary["tx_count"] == 0:
        await bot.send_message(
            chat_id=chat_id,
            text=f"📭 Belum ada transaksi yang tercatat pada bulan <b>{year_month}</b>.",
            reply_to_message_id=reply_to_message_id,
            reply_markup=get_main_action_keyboard(),
        )
        return

    img_buf = await asyncio.to_thread(render_monthly_table, year_month, summary)
    photo_file = BufferedInputFile(
        img_buf.getvalue(),
        filename=f"rekap_bulanan_{year_month}.png",
    )

    total_inc = summary["total_income"]
    total_exp = summary["total_expense"]
    net_month = total_inc - total_exp

    cat_lines = []
    for idx, cat in enumerate(summary["categories"][:6], start=1):
        c_name = cat["category"]
        c_total = float(cat["total"])
        pct = (c_total / total_exp * 100) if total_exp > 0 else 0
        cat_lines.append(f"  {idx}. {c_name}: <b>{format_rupiah(c_total)}</b> ({pct:.1f}%)")

    cat_section = (
        "\n\n📂 <b>Rincian Pengeluaran per Kategori:</b>\n" + "\n".join(cat_lines)
        if cat_lines
        else ""
    )

    caption = (
        f"📆 <b>Laporan Keuangan Bulan {year_month}</b>\n"
        f"• <b>Total Transaksi:</b> {summary['tx_count']}x\n"
        f"• <b>Total Pemasukan:</b> {format_rupiah(total_inc)}\n"
        f"• <b>Total Pengeluaran:</b> {format_rupiah(total_exp)}\n"
        f"• <b>Saldo / Selisih Bulan Ini:</b> {format_rupiah(net_month)}"
        f"{cat_section}"
    )

    await bot.send_photo(
        chat_id=chat_id,
        photo=photo_file,
        caption=caption,
        reply_to_message_id=reply_to_message_id,
        reply_markup=get_main_action_keyboard(),
    )


async def do_hapus_menu_action(bot: Bot, chat_id: int, reply_to_message_id: Optional[int] = None) -> None:
    today_str = get_today_str()
    transactions = await db.get_transactions_by_date(today_str, chat_id)

    if not transactions:
        await bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ Tidak ada transaksi yang dapat dihapus untuk hari ini (<b>{today_str}</b>).",
            reply_to_message_id=reply_to_message_id,
            reply_markup=get_main_action_keyboard(),
        )
        return

    buttons = []
    for idx, tx in enumerate(transactions, start=1):
        created_at = tx.get("created_at")
        if isinstance(created_at, datetime):
            time_str = created_at.strftime("%H:%M")
        else:
            time_str = str(created_at)[11:16] if created_at else "--:--"

        icon = "🟢" if tx["type"] == "INCOME" else "🔴"
        desc = str(tx["description"])
        if len(desc) > 22:
            desc = desc[:19] + "..."
        amt_str = format_rupiah(float(tx["amount"]))

        btn_text = f"{idx}. {icon} [{time_str}] {desc} ({amt_str})"
        buttons.append(
            [InlineKeyboardButton(text=btn_text, callback_data=f"del_tx:{tx['id']}")]
        )

    buttons.append([InlineKeyboardButton(text="❌ Batal", callback_data="del_cancel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await bot.send_message(
        chat_id=chat_id,
        text=f"🗑️ <b>Pilih transaksi tanggal {today_str} yang ingin dihapus:</b>",
        reply_to_message_id=reply_to_message_id,
        reply_markup=keyboard,
    )


async def do_stop_action(bot: Bot, chat_id: int, reply_to_message_id: Optional[int] = None) -> None:
    today_str = get_today_str()
    await db.close_session(today_str, chat_id)
    transactions = await db.get_transactions_by_date(today_str, chat_id)

    if not transactions:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"🛑 <b>Sesi Pencatatan Dihentikan (STOP)</b>\n"
                f"Belum ada transaksi hari ini (<b>{today_str}</b>). Bot saat ini dinonaktifkan sementara.\n\n"
                f"⚠️ <i>Untuk mulai mencatat transaksi lagi, silakan ketik <b>/start</b> atau tekan tombol <b>▶️ Start / Mulai Mencatat Lagi</b> di bawah terlebih dahulu.</i>"
            ),
            reply_to_message_id=reply_to_message_id,
            reply_markup=get_stopped_keyboard(),
        )
        return

    caption = await build_final_closing_caption(today_str, chat_id)
    await send_daily_recap_image(
        bot=bot,
        chat_id=chat_id,
        date_str=today_str,
        caption=caption,
        reply_to_message_id=reply_to_message_id,
        custom_markup=get_stopped_keyboard(),
    )


async def do_restart_prompt_action(bot: Bot, chat_id: int, reply_to_message_id: Optional[int] = None) -> None:
    today_str = get_today_str()
    transactions = await db.get_transactions_by_date(today_str, chat_id)

    if not transactions:
        await bot.send_message(
            chat_id=chat_id,
            text=f"📭 Catatan hari ini (<b>{today_str}</b>) masih kosong, belum ada yang perlu di-restart.",
            reply_to_message_id=reply_to_message_id,
            reply_markup=get_main_action_keyboard(),
        )
        return

    confirm_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⚠️ Ya, Hapus Semua ({len(transactions)} transaksi)",
                    callback_data="restart_confirm",
                )
            ],
            [
                InlineKeyboardButton(text="❌ Batal", callback_data="restart_cancel"),
            ],
        ]
    )
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"🔄 <b>Konfirmasi Restart Catatan Hari Ini ({today_str})</b>\n\n"
            f"Apakah Anda yakin ingin mereset/menghapus seluruh <b>{len(transactions)} transaksi</b> hari ini dan mengulang dari nol?"
        ),
        reply_to_message_id=reply_to_message_id,
        reply_markup=confirm_kb,
    )


# =====================================================================
# COMMAND HANDLERS ("/start", "/lanjut", "/rekap", "/bulanan", "/hapus", "/restart", "/stop", "/tutup", "/help")
# =====================================================================

@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Handler untuk command /start (sekaligus membuka kunci sesi jika sebelumnya di-STOP)."""
    today_str = get_today_str()
    await db.reopen_daily_session(today_str, message.chat.id)
    text = (
        "👋 <b>Selamat datang di HematUang Bot!</b>\n"
        "✅ <i>Status Sesi: AKTIF (Siap Mencatat)</i>\n\n"
        "Langsung kirimkan catatan transaksi Anda lewat <b>chat biasa</b> atau <b>foto struk / bukti transfer</b>:\n"
        "• <i>\"Beli kopi susu 22rb\"</i>\n"
        "• <i>\"Isi bensin pertamax 50k\"</i>\n"
        "• <i>\"Gaji bulanan masuk 6,5jt\"</i>\n\n"
        "👇 <i>Atau pilih menu aksi di bawah ini:</i>"
    )
    await message.answer(text, reply_markup=get_main_action_keyboard())


@dp.message(Command("lanjut"))
async def cmd_lanjut(message: Message) -> None:
    """Handler untuk command /lanjut."""
    today_str = get_today_str()
    await db.reopen_daily_session(today_str, message.chat.id)
    await message.answer(
        f"✍️ <b>Sesi Aktif! Siap mencatat untuk tanggal {today_str}.</b>\n"
        "Silakan langsung ketik transaksi Anda (contoh: <code>Makan siang 30rb</code>) atau kirim foto struk/bukti transfer.",
        reply_markup=get_main_action_keyboard(),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Handler untuk command /help."""
    await message.answer(get_help_text(), reply_markup=get_main_action_keyboard())


@dp.message(Command("rekap"))
async def cmd_rekap(message: Message, bot: Bot) -> None:
    """Menampilkan gambar tabel rekap transaksi hari ini."""
    await do_rekap_action(bot, message.chat.id, reply_to_message_id=message.message_id)


@dp.message(Command("bulanan", "rekap_bulan"))
async def cmd_bulanan(message: Message, bot: Bot) -> None:
    """Menampilkan laporan rekap keuangan bulan berjalan beserta rincian per kategori."""
    await do_bulanan_action(bot, message.chat.id, reply_to_message_id=message.message_id)


@dp.message(Command("tutup", "stop"))
async def cmd_tutup(message: Message, bot: Bot) -> None:
    """Menutup sesi hari ini secara manual (/stop atau /tutup)."""
    await do_stop_action(bot, message.chat.id, reply_to_message_id=message.message_id)


@dp.message(Command("restart", "reset"))
async def cmd_restart(message: Message, bot: Bot) -> None:
    """Mereset seluruh transaksi hari ini setelah konfirmasi."""
    await do_restart_prompt_action(bot, message.chat.id, reply_to_message_id=message.message_id)


@dp.message(Command("hapus"))
async def cmd_hapus(message: Message, bot: Bot) -> None:
    """Menampilkan daftar pilihan transaksi hari ini yang ingin dihapus menggunakan Inline Keyboard."""
    await do_hapus_menu_action(bot, message.chat.id, reply_to_message_id=message.message_id)


# =====================================================================
# CALLBACK QUERY HANDLERS (Tombol Menu Interaktif & Hapus/Restart)
# =====================================================================

async def safe_callback_answer(
    callback: CallbackQuery,
    text: Optional[str] = None,
    show_alert: bool = False,
) -> None:
    """
    Menjawab CallbackQuery dengan aman tanpa error jika query sudah kedaluwarsa
    (misal tombol ditekan saat bot sedang mati/restart atau koneksi lambat).
    """
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest as exc:
        logger.debug("Mengabaikan callback.answer yang kedaluwarsa: %s", exc)
    except Exception as exc:
        logger.debug("Gagal callback.answer: %s", exc)


@dp.callback_query(F.data.startswith("menu:"))
async def cb_main_menu(callback: CallbackQuery, bot: Bot) -> None:
    """Menangani klik tombol menu cepat di bawah pesan."""
    if not callback.data or not callback.message:
        return

    action = callback.data.split(":", 1)[1]
    chat_id = callback.message.chat.id
    await safe_callback_answer(callback)

    if action in ("lanjut", "start"):
        today_str = get_today_str()
        await db.reopen_daily_session(today_str, chat_id)
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ <b>Sesi Diaktifkan Kembali ({today_str})!</b>\n"
                "Silakan kirimkan teks transaksi (misal: <code>Beli cemilan 15rb</code>) atau kirim foto struk/bukti transfer."
            ),
            reply_markup=get_main_action_keyboard(),
        )
    elif action == "rekap":
        await do_rekap_action(bot, chat_id)
    elif action == "bulanan":
        await do_bulanan_action(bot, chat_id)
    elif action == "hapus":
        await do_hapus_menu_action(bot, chat_id)
    elif action == "restart":
        await do_restart_prompt_action(bot, chat_id)
    elif action == "stop":
        await do_stop_action(bot, chat_id)
    elif action == "help":
        await bot.send_message(
            chat_id=chat_id,
            text=get_help_text(),
            reply_markup=get_main_action_keyboard(),
        )


@dp.callback_query(F.data == "restart_cancel")
async def cb_restart_cancel(callback: CallbackQuery) -> None:
    await safe_callback_answer(callback, "Restart dibatalkan.")
    if callback.message:
        try:
            await callback.message.edit_text(
                "❎ Restart catatan hari ini dibatalkan. Data Anda tetap aman.",
                reply_markup=get_main_action_keyboard(),
            )
        except TelegramBadRequest:
            pass


@dp.callback_query(F.data == "restart_confirm")
async def cb_restart_confirm(callback: CallbackQuery) -> None:
    if not callback.message:
        return
    await safe_callback_answer(callback, "Catatan hari ini sedang di-reset...")
    chat_id = callback.message.chat.id
    today_str = get_today_str()
    deleted_count = await db.clear_transactions_by_date(today_str, chat_id)
    try:
        await callback.message.edit_text(
            f"🔄 <b>Restart Berhasil!</b>\n"
            f"Sebanyak <b>{deleted_count} transaksi</b> pada tanggal <b>{today_str}</b> telah dihapus. "
            f"Silakan mulai mencatat ulang dari nol.",
            reply_markup=get_main_action_keyboard(),
        )
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data == "del_cancel")
async def cb_delete_cancel(callback: CallbackQuery) -> None:
    """Membatalkan proses penghapusan transaksi."""
    await safe_callback_answer(callback, "Penghapusan dibatalkan.")
    if callback.message:
        try:
            await callback.message.edit_text(
                "❎ Penghapusan transaksi dibatalkan.",
                reply_markup=get_main_action_keyboard(),
            )
        except TelegramBadRequest:
            pass


@dp.callback_query(F.data.startswith("del_tx:"))
async def cb_delete_transaction(callback: CallbackQuery, bot: Bot) -> None:
    """Menghapus transaksi yang dipilih pengguna dari tombol inline lalu mengirim tabel rekap terbaru."""
    if not callback.data or not callback.message:
        return

    await safe_callback_answer(callback, "Menghapus transaksi...")
    tx_id = int(callback.data.split(":", 1)[1])
    chat_id = callback.message.chat.id

    deleted_tx = await db.delete_transaction_by_id(tx_id, chat_id)
    if not deleted_tx:
        try:
            await callback.message.edit_text(
                "⚠️ Transaksi tidak ditemukan atau sudah dihapus.",
                reply_markup=get_main_action_keyboard(),
            )
        except TelegramBadRequest:
            pass
        return

    date_str = str(deleted_tx["session_date"])
    try:
        await callback.message.edit_text(
            f"🗑️ <b>Transaksi Berhasil Dihapus!</b>\n"
            f"• <b>Kategori:</b> {deleted_tx['category']}\n"
            f"• <b>Keterangan:</b> {deleted_tx['description']}\n"
            f"• <b>Nominal:</b> {format_rupiah(float(deleted_tx['amount']))}"
        )
    except TelegramBadRequest:
        pass

    await send_daily_recap_image(
        bot=bot,
        chat_id=chat_id,
        date_str=date_str,
        caption=f"📊 <b>Rekap terbaru setelah penghapusan ({date_str})</b>\n👇 <i>Pilih menu aksi di bawah:</i>",
        with_menu=True,
    )


# =====================================================================
# MESSAGE HANDLERS (Foto Struk & Teks Transaksi)
# =====================================================================

async def check_stopped_and_warn(message: Message) -> bool:
    """
    Mengecek apakah sesi hari ini sedang di-STOP (CLOSED).
    Jika ya, kirim peringatan agar user melakukan /start atau /lanjut terlebih dahulu dan kembalikan True.
    """
    today_str = get_today_str()
    if await db.is_session_closed(today_str, message.chat.id):
        await message.reply(
            "⚠️ <b>Peringatan: Sesi Pencatatan Sedang Di-STOP!</b>\n"
            "Transaksi ini belum dicatat karena Anda telah menghentikan sesi sebelumnya (data transaksi hari ini tetap aman & tidak hilang).\n\n"
            "👉 Silakan tekan tombol <b>➕ Lanjut Catat (Data Tetap Ada)</b> atau ketik <b>/start</b> / <b>/lanjut</b> terlebih dahulu untuk melanjutkan.",
            reply_markup=get_stopped_keyboard(),
        )
        return True
    return False


@dp.message(F.photo)
async def handle_photo_message(message: Message, bot: Bot) -> None:
    """Jika pengguna mengirim foto disertai caption teks transaksi, ekstrak dari caption."""
    if await check_stopped_and_warn(message):
        return

    if not message.caption or not message.caption.strip():
        await message.reply(
            "ℹ️ Mode AI lokal (GGUF) bekerja berbasis teks. Silakan sertakan keterangan transaksi pada caption foto atau kirim lewat chat teks (contoh: <code>beli kopi 30k + makan 550k</code>).",
            reply_markup=get_main_action_keyboard(),
        )
        return

    try:
        extracted_list = await parse_transactions(message.caption.strip())
        await process_and_record_transactions(message, bot, extracted_list)
    except Exception as exc:
        logger.exception("Gagal memproses caption transaksi:")
        await message.reply(
            f"❌ Gagal memproses transaksi: <code>{exc}</code>",
            reply_markup=get_main_action_keyboard(),
        )


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text_message(message: Message, bot: Bot) -> None:
    """Menerima pesan teks biasa (satu atau banyak item), mengekstrak via GGUF lokal, dan mencatatnya."""
    if not message.text or not message.text.strip():
        return

    if await check_stopped_and_warn(message):
        return

    try:
        extracted_list = await parse_transactions(message.text.strip())
        await process_and_record_transactions(message, bot, extracted_list)
    except Exception as exc:
        logger.exception("Gagal memproses teks transaksi:")
        await message.reply(
            f"❌ Gagal memahami transaksi: <code>{exc}</code>",
            reply_markup=get_main_action_keyboard(),
        )


async def close_yesterday_sessions_job(bot: Bot) -> None:
    """
    Job otomatis setiap pukul 00:00 WIB:
    Menutup sesi kemarin (CLOSED) yang belum ditutup manual, mengirimkan gambar rekap final
    beserta ringkasan kategori & akumulasi bulanan secara silent (tanpa bunyi notifikasi).
    """
    now_wib = datetime.now(WIB)
    yesterday_str = (now_wib - timedelta(days=1)).strftime("%Y-%m-%d")
    logger.info("Menjalankan penutupan sesi otomatis untuk tanggal %s", yesterday_str)

    open_sessions = await db.get_open_sessions(yesterday_str)
    for session in open_sessions:
        chat_id = session["chat_id"]
        try:
            await db.close_session(yesterday_str, chat_id)
            caption = await build_final_closing_caption(yesterday_str, chat_id)
            await send_daily_recap_image(
                bot=bot,
                chat_id=chat_id,
                date_str=yesterday_str,
                caption=caption,
                disable_notification=True,
                with_menu=True,
            )
        except Exception:
            logger.exception(
                "Gagal menutup sesi atau mengirim rekap final untuk chat_id=%s tanggal=%s",
                chat_id,
                yesterday_str,
            )


async def setup_bot_commands(bot: Bot) -> None:
    """
    Mendaftarkan daftar command ke Telegram di background agar tidak menghambat startup bot.
    """
    commands = [
        BotCommand(command="start", description="Menu utama & mulai bot"),
        BotCommand(command="lanjut", description="Lanjut mencatat transaksi baru"),
        BotCommand(command="rekap", description="Lihat tabel rekap transaksi hari ini"),
        BotCommand(command="bulanan", description="Lihat rekap & rincian kategori bulan ini"),
        BotCommand(command="hapus", description="Pilih & hapus transaksi hari ini"),
        BotCommand(command="restart", description="Reset / ulang semua catatan hari ini dari nol"),
        BotCommand(command="stop", description="Stop & tutup buku catatan hari ini"),
        BotCommand(command="tutup", description="Tutup buku hari ini & lihat ringkasan akhir"),
        BotCommand(command="help", description="Panduan lengkap cara memakai bot"),
    ]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        logger.info("Menu command '/' berhasil disinkronkan ke Telegram.")
    except Exception as exc:
        logger.debug("Sinkronisasi menu command dilewati (sudah terdaftar sebelumnya): %s", exc)


class StableAiohttpSession(AiohttpSession):
    """
    Custom AiohttpSession untuk mencegah freeze & crash jaringan di Windows / ISP Indonesia:
    - Memaksa IPv4 (AF_INET)
    - Mengirim ClientTimeout(sock_connect=5.0) langsung ke session.post() agar tidak ditimpa integer
      oleh aiogram bawaan, serta auto-retry otomatis jika koneksi ISP kedip saat kirim pesan/foto.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._connector_init["family"] = socket.AF_INET
        self._connector_init["keepalive_timeout"] = 20
        self._connector_init["enable_cleanup_closed"] = True

    async def make_request(self, bot: Bot, method, timeout: Optional[int] = None):
        url = self.api.api_url(token=bot.token, method=method.__api_method__)

        is_polling = method.__api_method__ == "getUpdates"
        max_attempts = 2 if is_polling else 4

        if is_polling:
            poll_wait = float(getattr(method, "timeout", None) or 3)
            client_timeout = aiohttp.ClientTimeout(
                total=poll_wait + 3.0,
                connect=2.5,
                sock_connect=2.5,
                sock_read=poll_wait + 2.0,
            )
        else:
            client_timeout = aiohttp.ClientTimeout(
                total=12.0,
                connect=3.0,
                sock_connect=3.0,
                sock_read=10.0,
            )

        for attempt in range(max_attempts):
            session = await self.create_session()
            form = self.build_form_data(bot=bot, method=method)
            try:
                async with session.post(url, data=form, timeout=client_timeout) as resp:
                    raw_result = await resp.text()
                response = self.check_response(
                    bot=bot,
                    method=method,
                    status_code=resp.status,
                    content=raw_result,
                )
                return response.result
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                # Paksa buat koneksi TCP baru karena socket lama kemungkinan diputus NAT/ISP
                self._should_reset_connector = True
                if attempt + 1 < max_attempts:
                    await asyncio.sleep(0.15)
                    continue
                raise TelegramNetworkError(
                    method=method,
                    message=f"{type(exc).__name__}: {exc}",
                ) from exc


async def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token == "your_telegram_bot_token_here":
        raise RuntimeError("TELEGRAM_BOT_TOKEN belum dikonfigurasi di dalam file .env")

    await db.init_pool()

    # Pre-load model GGUF lokal di memori saat startup agar chat pertama langsung cepat
    await asyncio.to_thread(get_llm)

    session = StableAiohttpSession(timeout=25)
    bot = Bot(
        token=token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Pre-cache info bot agar dp.start_polling() tidak pernah crash akibat timeout getMe() di awal
    try:
        bot_id = int(token.split(":", 1)[0])
        bot._me = User(id=bot_id, is_bot=True, first_name="moneytalks", username="hematuangbot")
    except Exception:
        pass

    scheduler = AsyncIOScheduler(timezone=WIB)
    scheduler.add_job(
        close_yesterday_sessions_job,
        trigger=CronTrigger(hour=0, minute=0, timezone=WIB),
        kwargs={"bot": bot},
        id="close_yesterday_sessions",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("AsyncIOScheduler aktif (Cron 00:00 WIB).")

    if "--sync-commands" in sys.argv:
        await setup_bot_commands(bot)

    try:
        logger.info("Bot mulai berjalan (polling)...")
        backoff = BackoffConfig(min_delay=0.1, max_delay=0.5, factor=1.1, jitter=0.02)
        await dp.start_polling(bot, polling_timeout=3, backoff_config=backoff)
    finally:
        scheduler.shutdown(wait=False)
        await db.close_pool()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
