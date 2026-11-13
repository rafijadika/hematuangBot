import os
from datetime import datetime
from io import BytesIO
from typing import Any, Dict, List

# Gunakan folder cache lokal agar Matplotlib tidak membuat ulang font cache setiap startup
_mpl_cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".mplcache")
os.makedirs(_mpl_cache, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", _mpl_cache)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pytz

WIB = pytz.timezone("Asia/Jakarta")


def format_rupiah(amount: float) -> str:
    """Format angka ke format Rupiah (contoh: Rp 150.000)."""
    sign = "-" if amount < 0 else ""
    val = abs(int(round(amount)))
    formatted = f"{val:,}".replace(",", ".")
    return f"{sign}Rp {formatted}"


def render_daily_table(date_str: str, transactions: List[Dict[str, Any]]) -> BytesIO:
    """
    Merender daftar transaksi harian menjadi gambar tabel PNG beresolusi tinggi (BytesIO).
    Kolom: Jam, Tipe, Kategori, Keterangan, Nominal
    Dilengkapi baris ringkasan TOTAL MASUK, TOTAL KELUAR, dan SELISIH HARI INI di bagian bawah.
    """
    col_labels = ["Jam", "Tipe", "Kategori", "Keterangan", "Nominal"]
    table_data: List[List[str]] = []

    total_income = 0.0
    total_expense = 0.0

    for tx in transactions:
        created_at = tx.get("created_at")
        if isinstance(created_at, datetime):
            if created_at.tzinfo is not None:
                created_at = created_at.astimezone(WIB)
            time_str = created_at.strftime("%H:%M")
        else:
            time_str = str(created_at)[11:16] if created_at else "-"

        tx_type = tx.get("type", "EXPENSE")
        tipe_label = "MASUK" if tx_type == "INCOME" else "KELUAR"
        amount = float(tx.get("amount", 0))

        if tx_type == "INCOME":
            total_income += amount
        else:
            total_expense += amount

        category = str(tx.get("category", "-"))
        description = str(tx.get("description", "-"))
        if len(description) > 38:
            description = description[:35] + "..."

        table_data.append([
            time_str,
            tipe_label,
            category,
            description,
            format_rupiah(amount),
        ])

    if not table_data:
        table_data.append(["-", "-", "-", "Belum ada transaksi", "Rp 0"])

    num_tx_rows = len(table_data)
    net_today = total_income - total_expense

    # Tambahkan baris ringkasan di bagian bawah
    summary_rows = [
        ["", "", "", "TOTAL MASUK", format_rupiah(total_income)],
        ["", "", "", "TOTAL KELUAR", format_rupiah(total_expense)],
        ["", "", "", "SELISIH HARI INI", format_rupiah(net_today)],
    ]
    full_data = table_data + summary_rows
    total_rows = len(full_data)

    # Hitung ukuran figure secara dinamis berdasarkan jumlah baris
    fig_height = max(3.0, 1.4 + (total_rows + 1) * 0.36)
    fig, ax = plt.subplots(figsize=(8.8, fig_height), dpi=115)
    ax.axis("off")

    # Judul tabel
    ax.set_title(
        f"REKAP KEUANGAN HARIAN — {date_str}",
        fontsize=13,
        fontweight="bold",
        pad=14,
        color="#1E293B",
    )

    col_widths = [0.10, 0.12, 0.20, 0.36, 0.22]
    table = ax.table(
        cellText=full_data,
        colLabels=col_labels,
        colWidths=col_widths,
        loc="center",
        cellLoc="left",
    )

    table.auto_set_font_size(False)
    table.set_fontsize(9.5)
    table.scale(1, 1.45)

    # Styling sel-sel tabel
    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor("#CBD5E1")
        cell.set_linewidth(0.8)

        # Header
        if row == 0:
            cell.set_facecolor("#1E293B")
            cell.get_text().set_color("#FFFFFF")
            cell.get_text().set_weight("bold")
            cell.get_text().set_ha("center")
            continue

        data_row_idx = row - 1

        # Baris Transaksi
        if data_row_idx < num_tx_rows:
            bg_color = "#FFFFFF" if data_row_idx % 2 == 0 else "#F8FAFC"
            cell.set_facecolor(bg_color)

            if col in (0, 1):
                cell.get_text().set_ha("center")
            elif col == 4:
                cell.get_text().set_ha("right")
                cell.get_text().set_weight("bold")

            # Warna khusus tipe MASUK / KELUAR
            tipe_val = full_data[data_row_idx][1]
            if tipe_val == "MASUK" and col in (1, 4):
                cell.get_text().set_color("#15803D")  # Hijau
            elif tipe_val == "KELUAR" and col in (1, 4):
                cell.get_text().set_color("#B91C1C")  # Merah

        # Baris Ringkasan (Summary)
        else:
            summary_idx = data_row_idx - num_tx_rows
            if summary_idx == 0:  # TOTAL MASUK
                cell.set_facecolor("#ECFDF5")
                cell.get_text().set_color("#065F46")
            elif summary_idx == 1:  # TOTAL KELUAR
                cell.set_facecolor("#FEF2F2")
                cell.get_text().set_color("#991B1B")
            else:  # SELISIH HARI INI
                cell.set_facecolor("#EFF6FF")
                cell.get_text().set_color("#1E40AF" if net_today >= 0 else "#991B1B")

            cell.get_text().set_weight("bold")
            if col == 3:
                cell.get_text().set_ha("right")
            elif col == 4:
                cell.get_text().set_ha("right")

    plt.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=115, bbox_inches="tight", facecolor="#FFFFFF")
    plt.close(fig)
    buf.seek(0)
    return buf


def render_monthly_table(year_month: str, summary: Dict[str, Any]) -> BytesIO:
    """
    Merender tabel rekap bulanan per tanggal menjadi gambar PNG beresolusi tinggi (BytesIO).
    Kolom: Tanggal, Jml Transaksi, Total Masuk, Total Keluar, Selisih Harian
    Beserta baris ringkasan TOTAL BULAN INI di bagian bawah.
    """
    col_labels = ["Tanggal", "Transaksi", "Total Masuk", "Total Keluar", "Selisih"]
    table_data: List[List[str]] = []

    daily_rows = summary.get("daily_recap", [])
    total_income = float(summary.get("total_income", 0))
    total_expense = float(summary.get("total_expense", 0))
    net_month = total_income - total_expense

    for row in daily_rows:
        d_str = str(row.get("session_date", "-"))
        cnt = int(row.get("tx_count", 0))
        inc = float(row.get("income", 0))
        exp = float(row.get("expense", 0))
        diff = inc - exp
        table_data.append([
            d_str,
            f"{cnt}x",
            format_rupiah(inc),
            format_rupiah(exp),
            format_rupiah(diff),
        ])

    if not table_data:
        table_data.append(["-", "0x", "Rp 0", "Rp 0", "Rp 0"])

    num_data_rows = len(table_data)

    summary_rows = [
        ["", "", "", "TOTAL MASUK BULAN INI", format_rupiah(total_income)],
        ["", "", "", "TOTAL KELUAR BULAN INI", format_rupiah(total_expense)],
        ["", "", "", "SALDO / SELISIH BULAN INI", format_rupiah(net_month)],
    ]
    full_data = table_data + summary_rows
    total_rows = len(full_data)

    fig_height = max(3.0, 1.4 + (total_rows + 1) * 0.36)
    fig, ax = plt.subplots(figsize=(8.8, fig_height), dpi=115)
    ax.axis("off")

    ax.set_title(
        f"REKAP KEUANGAN BULANAN — PERIODE {year_month}",
        fontsize=13,
        fontweight="bold",
        pad=14,
        color="#1E293B",
    )

    col_widths = [0.16, 0.12, 0.24, 0.24, 0.24]
    table = ax.table(
        cellText=full_data,
        colLabels=col_labels,
        colWidths=col_widths,
        loc="center",
        cellLoc="left",
    )

    table.auto_set_font_size(False)
    table.set_fontsize(9.5)
    table.scale(1, 1.45)

    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor("#CBD5E1")
        cell.set_linewidth(0.8)

        if row == 0:
            cell.set_facecolor("#1E293B")
            cell.get_text().set_color("#FFFFFF")
            cell.get_text().set_weight("bold")
            cell.get_text().set_ha("center")
            continue

        data_row_idx = row - 1
        if data_row_idx < num_data_rows:
            bg_color = "#FFFFFF" if data_row_idx % 2 == 0 else "#F8FAFC"
            cell.set_facecolor(bg_color)
            if col in (0, 1):
                cell.get_text().set_ha("center")
            else:
                cell.get_text().set_ha("right")
                if col == 2:
                    cell.get_text().set_color("#15803D")
                elif col == 3:
                    cell.get_text().set_color("#B91C1C")
                elif col == 4:
                    cell.get_text().set_weight("bold")
        else:
            summary_idx = data_row_idx - num_data_rows
            if summary_idx == 0:
                cell.set_facecolor("#ECFDF5")
                cell.get_text().set_color("#065F46")
            elif summary_idx == 1:
                cell.set_facecolor("#FEF2F2")
                cell.get_text().set_color("#991B1B")
            else:
                cell.set_facecolor("#EFF6FF")
                cell.get_text().set_color("#1E40AF" if net_month >= 0 else "#991B1B")

            cell.get_text().set_weight("bold")
            if col in (3, 4):
                cell.get_text().set_ha("right")

    plt.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=115, bbox_inches="tight", facecolor="#FFFFFF")
    plt.close(fig)
    buf.seek(0)
    return buf


