import os
import re
from typing import Literal, Optional
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()

SYSTEM_PROMPT = """
Kamu adalah asisten pencatat keuangan pintar berbahasa Indonesia.
Tugasmu adalah mengekstrak informasi transaksi keuangan dari pesan teks pengguna atau foto strukbelanja / bukti transfer.

Aturan ekstraksi:
1. type:
   - "EXPENSE" jika merupakan pengeluaran, pembelian, pembayaran, transfer keluar, belanja, bayar tagihan, dll.
   - "INCOME" jika merupakan pemasukan, gaji, bonus, transfer masuk, hasil penjualan, kembalian/refund masuk, dll.
2. amount:
   - Nilai nominal uang dalam bilangan bulat (integer) Rupiah tanpa titik/koma.
   - Pahami singkatan bahasa Indonesia: "rb", "ribu", "k" = x1.000 (contoh: "25rb" atau "25k" -> 25000).
   - "jt", "juta" = x1.000.000 (contoh: "1,5jt" atau "1.5 juta" -> 1500000).
   - Jika berupa struk/bukti transfer, ambil total akhir (grand total / nominal transaksi).
3. category:
   - Kategori singkat dan jelas (1-2 kata), misalnya: "Makanan & Minuman", "Transportasi", "Belanja", "Tagihan", "Hiburan", "Kesehatan", "Gaji", "Bonus", "Transfer", "Lainnya".
4. description:
   - Deskripsi singkat transaksi (maksimal 100 karakter), misalnya nama item, toko/merchant, atau keterangan transaksi.
""".strip()


class TransactionExtraction(BaseModel):
    type: Literal["INCOME", "EXPENSE"] = Field(
        description="Tipe transaksi: INCOME untuk pemasukan, EXPENSE untuk pengeluaran."
    )
    amount: int = Field(
        description="Nominal transaksi dalam angka bulat (integer) Rupiah."
    )
    category: str = Field(
        description="Kategori transaksi singkat, contoh: Makanan, Transportasi, Gaji, Belanja, Tagihan."
    )
    description: str = Field(
        description="Keterangan singkat mengenai transaksi."
    )


MODEL_CANDIDATES = [
    os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
    "gemini-3.5-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
]

_client: Optional[genai.Client] = None
_preferred_model: Optional[str] = None

# Kata kunci untuk deteksi cepat (< 1 ms) pada teks sederhana
_INCOME_KEYWORDS = {
    "gaji", "gajian", "bonus", "thr", "masuk", "pemasukan", "dapat", "dapet",
    "terima", "dikasih", "refund", "kembalian", "jualan", "untung", "cuan",
    "komisi", "honor", "fee", "dividen", "cashback",
}

_CATEGORY_RULES = [
    ("Makanan & Minuman", {"makan", "minum", "kopi", "nasi", "ayam", "bakso", "mie", "soto", "sarapan", "jajan", "cemilan", "snack", "teh", "es", "warteg", "gofood", "grabfood", "shopeefood", "roti", "sate", "geprek", "seblak"}),
    ("Transportasi", {"bensin", "pertamax", "pertalite", "shell", "parkir", "tol", "gojek", "goride", "gocar", "grab", "maxim", "kereta", "krl", "mrt", "transjakarta", "bus", "ojol", "servis", "oli", "ban"}),
    ("Tagihan & Utilitas", {"listrik", "token", "pln", "pdam", "air", "wifi", "indihome", "internet", "pulsa", "kuota", "paket data", "kos", "kost", "kontrakan", "sewa", "cicilan", "bpjs", "pajak"}),
    ("Belanja", {"belanja", "beli", "indomaret", "alfamart", "supermarket", "shopee", "tokopedia", "lazada", "tiktok", "baju", "celana", "sepatu", "sabun", "shampoo", "skincare", "deterjen"}),
    ("Hiburan", {"nonton", "bioskop", "netflix", "spotify", "youtube", "game", "topup", "top up", "karaoke", "liburan", "wisata", "nongkrong"}),
    ("Kesehatan", {"obat", "apotek", "dokter", "rs", "klinik", "vitamin", "rumah sakit", "periksa"}),
    ("Gaji & Penghasilan", {"gaji", "gajian", "honor", "upah", "komisi", "fee"}),
    ("Bonus & Hadiah", {"bonus", "thr", "hadiah", "angpao", "cashback", "dikasih"}),
]

_AMOUNT_PATTERN = re.compile(
    r"(?:rp\.?\s*)?(\d+(?:[.,]\d+)?)\s*(rb|ribu|k|jt|juta)?\b",
    re.IGNORECASE,
)


def _try_fast_parse(text: str) -> Optional[TransactionExtraction]:
    """
    Parser instan (< 1ms) untuk format chat harian sederhana seperti:
    'beli kopi 20rb', 'makan siang 35.000', 'gaji masuk 5,5jt'.
    Jika teks terlalu kompleks/ambigu, kembalikan None agar diproses oleh Gemini AI.
    """
    cleaned = text.strip()
    if "\n" in cleaned or len(cleaned) > 80:
        return None

    matches = list(_AMOUNT_PATTERN.finditer(cleaned))
    if len(matches) != 1:
        return None

    m = matches[0]
    raw_num = m.group(1)
    unit = (m.group(2) or "").lower()

    if unit in ("rb", "ribu", "k"):
        val = float(raw_num.replace(",", ".")) * 1_000
    elif unit in ("jt", "juta"):
        val = float(raw_num.replace(",", ".")) * 1_000_000
    else:
        # Contoh: "25.000" atau "1.500.000" atau "25000"
        normalized = raw_num.replace(".", "").replace(",", "")
        if not normalized.isdigit():
            return None
        val = float(normalized)

    amount = int(round(val))
    if amount < 100:
        return None

    desc_raw = (cleaned[:m.start()] + " " + cleaned[m.end():]).strip(" -:,.")
    desc_raw = re.sub(r"\b(rp\.?|sebesar|senilai)\b", "", desc_raw, flags=re.IGNORECASE).strip(" -:,.")
    if len(desc_raw) < 2:
        return None

    lower_desc = desc_raw.lower()
    words = set(re.findall(r"[a-z0-9]+", lower_desc))

    tx_type: Literal["INCOME", "EXPENSE"] = (
        "INCOME" if (words & _INCOME_KEYWORDS) else "EXPENSE"
    )

    category = "Pemasukan Lain" if tx_type == "INCOME" else "Lainnya"
    for cat_name, kw_set in _CATEGORY_RULES:
        if any(kw in lower_desc for kw in kw_set):
            category = cat_name
            break

    return TransactionExtraction(
        type=tx_type,
        amount=amount,
        category=category,
        description=desc_raw[:100].capitalize(),
    )


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY belum diatur di dalam file .env")
        _client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=8000,  # Hard timeout 8 detik (dalam ms) agar tidak pernah gantung berjam-jam
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
    return _client


async def _generate_structured(contents) -> TransactionExtraction:
    global _preferred_model
    client = _get_client()
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=TransactionExtraction,
        temperature=0.1,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    candidates = (
        [_preferred_model] + MODEL_CANDIDATES
        if _preferred_model
        else MODEL_CANDIDATES
    )

    last_exc = None
    seen = set()
    for model_name in candidates:
        if not model_name or model_name in seen:
            continue
        seen.add(model_name)
        try:
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
            _preferred_model = model_name
            if response.parsed:
                return response.parsed
            return TransactionExtraction.model_validate_json(response.text)
        except Exception as exc:
            last_exc = exc
            err_str = str(exc)
            if any(code in err_str for code in ("404", "503", "429", "UNAVAILABLE", "NOT_FOUND", "400")):
                continue
            raise
    raise last_exc


async def parse_transaction_text(text: str) -> TransactionExtraction:
    """
    Mengekstrak data transaksi dari pesan teks:
    - Menggunakan fast-path instan (< 1ms) untuk teks transaksi standar
    - Fallback ke Gemini AI dengan thinking_budget=0 untuk kalimat kompleks
    """
    fast_result = _try_fast_parse(text)
    if fast_result is not None:
        return fast_result

    return await _generate_structured(
        contents=f"Ekstrak data transaksi keuangan dari teks berikut:\n{text}"
    )


async def parse_transaction_image(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    caption: Optional[str] = None,
) -> TransactionExtraction:
    """Mengekstrak data transaksi dari gambar struk/bukti transfer beserta caption opsional."""
    prompt = "Ekstrak data transaksi keuangan dari gambar bukti transfer atau struk ini."
    if caption:
        prompt += f"\nKeterangan tambahan dari pengguna: {caption}"

    image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
    return await _generate_structured(contents=[image_part, prompt])
