import base64
from collections import defaultdict
import hashlib
import hmac
import os
from typing import Any, Dict, List, Optional

import aiomysql
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv()

_pool: Optional[aiomysql.Pool] = None
_fernet_cache: Dict[int, Fernet] = {}


def _get_user_fernet(chat_id: int) -> Fernet:
    """
    Menghasilkan instance Fernet (AES-128-CBC + HMAC-SHA256) dengan kunci unik per user (chat_id)
    yang di-cache di memori agar proses enkripsi/dekripsi sangat cepat.
    """
    cached = _fernet_cache.get(chat_id)
    if cached is not None:
        return cached

    master_secret = os.getenv("ENCRYPTION_KEY", "default-fallback-secret-key-hematuang")
    derived_bytes = hmac.new(
        master_secret.encode("utf-8"),
        msg=f"hematuang_user:{chat_id}".encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    fernet_key = base64.urlsafe_b64encode(derived_bytes)
    fernet_obj = Fernet(fernet_key)
    _fernet_cache[chat_id] = fernet_obj
    return fernet_obj


def encrypt_field(value: str, chat_id: int) -> str:
    """Mengenkripsi teks menggunakan kunci unik milik chat_id."""
    if not value:
        return ""
    f = _get_user_fernet(chat_id)
    return f.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_field(token: str, chat_id: int) -> str:
    """
    Mendekripsi ciphertext milik chat_id kembali menjadi plain text.
    Jika data lama belum terenkripsi, otomatis mengembalikan teks aslinya.
    """
    if not token:
        return ""
    f = _get_user_fernet(chat_id)
    try:
        return f.decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return token


async def init_pool() -> aiomysql.Pool:
    """Inisialisasi connection pool ke database MySQL/MariaDB secara cepat."""
    global _pool
    if _pool is None:
        _pool = await aiomysql.create_pool(
            host=os.getenv("DB_HOST", "127.0.0.1"),
            port=int(os.getenv("DB_PORT", "3306")),
            user=os.getenv("DB_USER", "root"),
            password=os.getenv("DB_PASSWORD", ""),
            db=os.getenv("DB_NAME", "hematuang_db"),
            charset="utf8mb4",
            autocommit=True,
            minsize=1,
            maxsize=10,
        )
    return _pool


async def close_pool() -> None:
    """Menutup connection pool database."""
    global _pool
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
        _pool = None


async def is_session_closed(date_str: str, chat_id: int) -> bool:
    """Mengembalikan True jika sesi pada tanggal tersebut berstatus CLOSED (di-stop)."""
    pool = await init_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT status FROM daily_sessions WHERE session_date = %s AND chat_id = %s",
                (date_str, chat_id),
            )
            row = await cur.fetchone()
            return bool(row and row["status"] == "CLOSED")


async def reopen_daily_session(date_str: str, chat_id: int) -> None:
    """Membuka kembali sesi yang berstatus CLOSED menjadi OPEN (dipanggil saat /start atau /lanjut)."""
    pool = await init_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO daily_sessions (session_date, chat_id, status, closed_at)
                VALUES (%s, %s, 'OPEN', NULL)
                ON DUPLICATE KEY UPDATE status = 'OPEN', closed_at = NULL
                """,
                (date_str, chat_id),
            )


async def ensure_daily_session(date_str: str, chat_id: int) -> bool:
    """
    Memastikan sesi harian untuk (date_str, chat_id) sudah ada.
    Mengembalikan True jika sesi BARU saja dibuat (belum ada sebelumnya),
    atau False jika sesi sudah ada.
    """
    pool = await init_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT session_date, status FROM daily_sessions WHERE session_date = %s AND chat_id = %s",
                (date_str, chat_id),
            )
            row = await cur.fetchone()
            if row is None:
                await cur.execute(
                    "INSERT INTO daily_sessions (session_date, chat_id, status) VALUES (%s, %s, 'OPEN')",
                    (date_str, chat_id),
                )
                return True
            return False


async def insert_transaction(
    date_str: str,
    chat_id: int,
    tx_type: str,
    amount: float,
    category: str,
    description: str,
) -> int:
    """Menyimpan transaksi baru ke tabel transactions (dengan enkripsi AES-Fernet) dan mengembalikan ID transaksi."""
    enc_category = encrypt_field(category[:50], chat_id)
    enc_description = encrypt_field(description[:255], chat_id)

    pool = await init_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO transactions (session_date, chat_id, type, amount, category, description)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (date_str, chat_id, tx_type, amount, enc_category, enc_description),
            )
            return cur.lastrowid


async def get_transactions_by_date(date_str: str, chat_id: int) -> List[Dict[str, Any]]:
    """Mengambil seluruh daftar transaksi pada tanggal dan chat_id tertentu lalu mendekripsinya."""
    pool = await init_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                SELECT id, session_date, chat_id, type, amount, category, description, created_at
                FROM transactions
                WHERE session_date = %s AND chat_id = %s
                ORDER BY created_at ASC, id ASC
                """,
                (date_str, chat_id),
            )
            rows = await cur.fetchall()
            if not rows:
                return []

            result = []
            for r in rows:
                item = dict(r)
                item["category"] = decrypt_field(item["category"], chat_id)
                item["description"] = decrypt_field(item["description"], chat_id)
                result.append(item)
            return result


async def get_open_sessions(date_str: str) -> List[Dict[str, Any]]:
    """Mengambil semua sesi yang berstatus OPEN pada tanggal tertentu."""
    pool = await init_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                SELECT session_date, chat_id, status, created_at
                FROM daily_sessions
                WHERE session_date = %s AND status = 'OPEN'
                """,
                (date_str,),
            )
            rows = await cur.fetchall()
            return list(rows) if rows else []


async def close_session(date_str: str, chat_id: int) -> None:
    """Menutup sesi harian (mengubah status menjadi CLOSED dan mengisi closed_at)."""
    pool = await init_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO daily_sessions (session_date, chat_id, status, closed_at)
                VALUES (%s, %s, 'CLOSED', CURRENT_TIMESTAMP)
                ON DUPLICATE KEY UPDATE status = 'CLOSED', closed_at = CURRENT_TIMESTAMP
                """,
                (date_str, chat_id),
            )


async def delete_last_transaction(date_str: str, chat_id: int) -> Optional[Dict[str, Any]]:
    """
    Menghapus transaksi terakhir pada tanggal dan chat_id tertentu.
    Mengembalikan dict data transaksi yang dihapus (sudah didekripsi), atau None jika tidak ada transaksi.
    """
    pool = await init_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                SELECT id, type, amount, category, description, created_at
                FROM transactions
                WHERE session_date = %s AND chat_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (date_str, chat_id),
            )
            last_tx = await cur.fetchone()
            if not last_tx:
                return None

            await cur.execute(
                "DELETE FROM transactions WHERE id = %s",
                (last_tx["id"],),
            )
            item = dict(last_tx)
            item["category"] = decrypt_field(item["category"], chat_id)
            item["description"] = decrypt_field(item["description"], chat_id)
            return item


async def delete_transaction_by_id(tx_id: int, chat_id: int) -> Optional[Dict[str, Any]]:
    """
    Menghapus satu transaksi berdasarkan ID dan chat_id.
    Mengembalikan dict data transaksi yang dihapus (sudah didekripsi), atau None jika tidak ditemukan.
    """
    pool = await init_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                SELECT id, session_date, type, amount, category, description, created_at
                FROM transactions
                WHERE id = %s AND chat_id = %s
                """,
                (tx_id, chat_id),
            )
            tx = await cur.fetchone()
            if not tx:
                return None

            await cur.execute(
                "DELETE FROM transactions WHERE id = %s AND chat_id = %s",
                (tx_id, chat_id),
            )
            item = dict(tx)
            item["category"] = decrypt_field(item["category"], chat_id)
            item["description"] = decrypt_field(item["description"], chat_id)
            return item


async def get_monthly_summary(year_month: str, chat_id: int) -> Dict[str, Any]:
    """
    Mengambil ringkasan keuangan bulanan untuk format year_month 'YYYY-MM' (dengan dekripsi kategori):
    - total_income: Total pemasukan bulan tersebut
    - total_expense: Total pengeluaran bulan tersebut
    - tx_count: Jumlah transaksi bulan tersebut
    - categories: Daftar pengeluaran per kategori (diurutkan dari terbesar)
    - daily_recap: Rekap total masuk & keluar per tanggal di bulan tersebut
    """
    pool = await init_pool()
    like_pattern = f"{year_month}-%"
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                SELECT session_date, type, amount, category
                FROM transactions
                WHERE CAST(session_date AS CHAR) LIKE %s AND chat_id = %s
                ORDER BY session_date ASC, created_at ASC
                """,
                (like_pattern, chat_id),
            )
            rows = await cur.fetchall()

            total_income = 0.0
            total_expense = 0.0
            tx_count = 0
            cat_totals: Dict[str, float] = defaultdict(float)
            cat_counts: Dict[str, int] = defaultdict(int)
            daily_map: Dict[str, Dict[str, Any]] = {}

            for r in rows or []:
                tx_count += 1
                amt = float(r["amount"] or 0)
                tx_type = r["type"]
                d_str = str(r["session_date"])
                dec_cat = decrypt_field(r["category"], chat_id)

                if d_str not in daily_map:
                    daily_map[d_str] = {
                        "session_date": d_str,
                        "income": 0.0,
                        "expense": 0.0,
                        "tx_count": 0,
                    }
                daily_map[d_str]["tx_count"] += 1

                if tx_type == "INCOME":
                    total_income += amt
                    daily_map[d_str]["income"] += amt
                else:
                    total_expense += amt
                    daily_map[d_str]["expense"] += amt
                    cat_totals[dec_cat] += amt
                    cat_counts[dec_cat] += 1

            categories = [
                {"category": cat, "total": tot, "count": cat_counts[cat]}
                for cat, tot in sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)
            ]

            return {
                "total_income": total_income,
                "total_expense": total_expense,
                "tx_count": tx_count,
                "categories": categories,
                "daily_recap": list(daily_map.values()),
            }


async def clear_transactions_by_date(date_str: str, chat_id: int) -> int:
    """
    Menghapus semua transaksi pada tanggal dan chat_id tertentu (fitur Restart Sesi Harian).
    Mengembalikan jumlah baris transaksi yang dihapus.
    """
    pool = await init_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM transactions WHERE session_date = %s AND chat_id = %s",
                (date_str, chat_id),
            )
            deleted_count = cur.rowcount
            await cur.execute(
                "UPDATE daily_sessions SET status = 'OPEN', closed_at = NULL WHERE session_date = %s AND chat_id = %s",
                (date_str, chat_id),
            )
            return deleted_count

