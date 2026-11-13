import os
import sys
import time

# Tambahkan root project ke sys.path agar bisa mengimpor llm_parser
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from llm_parser import get_llm, parse_transactions_sync, split_items


def main() -> None:
    print("=" * 60)
    print("UJI COBA INFERENSI MODEL GGUF LOKAL (llm_parser.py)")
    print("=" * 60)

    t0 = time.perf_counter()
    print("[1] Memuat model GGUF...")
    get_llm()
    print(f"    Model berhasil dimuat dalam {time.perf_counter() - t0:.2f} detik.\n")

    sample_inputs = [
        "beli kopi 30k + makan 550k",
        "kopi 15rb\nbensin 25rb",
        "gaji bulanan masuk 5jt",
    ]

    if len(sys.argv) > 1:
        sample_inputs = [" ".join(sys.argv[1:])]

    for idx, sample in enumerate(sample_inputs, start=1):
        print(f"[{idx}] Input Teks : {sample!r}")
        segments = split_items(sample)
        print(f"    Segmen     : {segments}")

        t_start = time.perf_counter()
        results = parse_transactions_sync(sample)
        elapsed = time.perf_counter() - t_start

        print(f"    Hasil ({len(results)} transaksi, {elapsed:.2f}s):")
        for item in results:
            print(f"      -> {item.model_dump_json()}")
        print("-" * 60)


if __name__ == "__main__":
    main()

