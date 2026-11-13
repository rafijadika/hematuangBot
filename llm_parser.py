import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

load_dotenv()

logger = logging.getLogger("hematuang_bot.llm_parser")

DEFAULT_MODEL_PATH = os.getenv("LOCAL_MODEL_PATH", "model/model.gguf")
FALLBACK_MODEL_PATH = "model/hematuang-q4_k_m.gguf"


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

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, v: Any) -> str:
        s = str(v or "EXPENSE").strip().upper()
        if s in ("INCOME", "PEMASUKAN", "MASUK", "IN", "CREDIT"):
            return "INCOME"
        return "EXPENSE"

    @field_validator("amount", mode="before")
    @classmethod
    def normalize_amount(cls, v: Any) -> int:
        if isinstance(v, (int, float)):
            return int(round(v))
        s = str(v or "0").strip().lower()
        s = re.sub(r"[^0-9.,a-z]", "", s)
        if s.endswith(("rb", "ribu", "k")):
            num_part = re.sub(r"[a-z]+", "", s).replace(",", ".")
            return int(round(float(num_part) * 1_000))
        if s.endswith(("jt", "juta")):
            num_part = re.sub(r"[a-z]+", "", s).replace(",", ".")
            return int(round(float(num_part) * 1_000_000))
        digits = re.sub(r"[^0-9]", "", s)
        return int(digits) if digits else 0


_llm_instance = None


def _resolve_model_path(model_path: str = "model/model.gguf") -> str:
    """Mencari lokasi file model GGUF di folder model/."""
    if os.path.exists(model_path):
        return model_path
    if os.path.exists(DEFAULT_MODEL_PATH):
        return DEFAULT_MODEL_PATH
    if os.path.exists(FALLBACK_MODEL_PATH):
        return FALLBACK_MODEL_PATH
    if os.path.isdir("model"):
        for fname in os.listdir("model"):
            if fname.lower().endswith(".gguf"):
                return os.path.join("model", fname)
    return model_path


def get_llm():
    """Memuat model GGUF menggunakan llama_cpp.Llama (singleton)."""
    global _llm_instance
    if _llm_instance is None:
        from llama_cpp import Llama

        resolved_path = _resolve_model_path("model/model.gguf")
        if not os.path.exists(resolved_path):
            raise FileNotFoundError(
                f"File model GGUF tidak ditemukan di '{resolved_path}' maupun '{FALLBACK_MODEL_PATH}'."
            )
        logger.info("Memuat model GGUF lokal dari: %s", resolved_path)
        cpu_threads = max(2, (os.cpu_count() or 4))
        _llm_instance = Llama(
            model_path=resolved_path,
            n_ctx=1024,
            n_threads=cpu_threads,
            n_threads_batch=cpu_threads,
            n_batch=512,
            verbose=False,
        )
    return _llm_instance


def split_items(text: str) -> List[str]:
    """
    Memecah input teks multi-item menjadi beberapa segmen transaksi menggunakan regex.
    Contoh:
    - "beli kopi 30k + makan 550k" -> ["beli kopi 30k", "makan 550k"]
    - "kopi 15rb\\nbensin 25rb" -> ["kopi 15rb", "bensin 25rb"]
    """
    if not text or not text.strip():
        return []

    # 1. Pecah berdasarkan baris baru (\n), tanda '+', atau ';'
    primary_parts = re.split(r"\s*(?:[\r\n;]+|\+)\s*", text.strip())
    segments: List[str] = []

    for part in primary_parts:
        part = part.strip()
        if not part:
            continue

        # 2. Pecah berdasarkan koma diikuti spasi+huruf atau kata hubung ("dan", "sama", "terus", "lalu")
        #    hanya jika setiap potongan sama-sama mengandung angka nominal
        sub_parts = re.split(
            r"(?:,\s+(?=[A-Za-z])|\s+(?:dan|sama|terus|trs|lalu)\s+)",
            part,
            flags=re.IGNORECASE,
        )
        if len(sub_parts) > 1 and all(re.search(r"\d", sp) for sp in sub_parts):
            segments.extend(sp.strip() for sp in sub_parts if sp.strip())
        else:
            segments.append(part)

    return [seg for seg in segments if seg]


def _extract_json_dict(raw_output: str) -> Dict[str, Any]:
    """Mengekstrak objek JSON valid dari teks keluaran model."""
    cleaned = raw_output.strip()
    match = re.search(r"\{.*?\}", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)
    data = json.loads(cleaned)
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError(f"Output JSON bukan objek dict: {raw_output}")

    return {
        "type": data.get("type") or data.get("tipe") or data.get("jenis") or "EXPENSE",
        "amount": data.get("amount") or data.get("nominal") or data.get("jumlah") or data.get("harga") or 0,
        "category": data.get("category") or data.get("kategori") or "Lainnya",
        "description": data.get("description") or data.get("deskripsi") or data.get("keterangan") or data.get("item") or "-",
    }


def parse_segment_sync(segmen: str) -> TransactionExtraction:
    """Menjalankan inferensi Qwen GGUF untuk satu segmen teks transaksi."""
    llm = get_llm()
    prompt = (
        "<|im_start|>system\n"
        "Ekstrak transaksi keuangan ke JSON valid.<|im_end|>\n"
        "<|im_start|>user\n"
        f"{segmen}<|im_end|>\n"
        "<|im_start|>assistant\n"
    )
    output = llm(
        prompt,
        max_tokens=256,
        temperature=0.0,
        stop=["<|im_end|>", "<|endoftext|>"],
    )
    raw_text = output["choices"][0]["text"].strip()
    parsed_dict = _extract_json_dict(raw_text)
    if not parsed_dict.get("description") or parsed_dict["description"] == "-":
        parsed_dict["description"] = segmen[:100]
    return TransactionExtraction.model_validate(parsed_dict)


def parse_transactions_sync(text: str) -> List[TransactionExtraction]:
    """
    Memecah teks input dengan split_items(text) lalu mengekstrak setiap segmen
    menggunakan model GGUF lokal dan mengembalikan list TransactionExtraction.
    """
    segments = split_items(text)
    if not segments:
        return []

    results: List[TransactionExtraction] = []
    for seg in segments:
        tx = parse_segment_sync(seg)
        results.append(tx)
    return results


async def parse_transactions(text: str) -> List[TransactionExtraction]:
    """Fungsi async wrapper untuk dipanggil dari bot.py (aiogram)."""
    return await asyncio.to_thread(parse_transactions_sync, text)

