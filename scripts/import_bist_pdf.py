from __future__ import annotations

"""BIST sembol PDF'sini tekrar üretilebilir bir JSON evrenine dönüştürür.

Bu araç yalnızca bakım/güncelleme sırasında çalıştırılır. Botun üretim çalışma
zamanı PDF veya pypdf bağımlılığı taşımaz; doğrulanmış JSON dosyasını okur.
"""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


SYMBOL_PATTERN = re.compile(r"[A-Z0-9]{4,6}")


def extract_symbols(pdf_path: Path) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - bakım aracı bağımlılığı
        raise RuntimeError("PDF içe aktarma için 'pypdf' kurulmalıdır.") from exc

    reader = PdfReader(str(pdf_path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    symbols: list[str] = []
    for raw_line in text.splitlines():
        raw_value = raw_line.strip()
        # "Fiyat" gibi tablo başlıklarını sonradan upper() ederek sembole
        # dönüştürmeyiz; kaynak satır zaten tamamen büyük olmalıdır.
        if raw_value != raw_value.upper():
            continue
        value = raw_value.upper()
        if SYMBOL_PATTERN.fullmatch(value) and value not in symbols:
            symbols.append(value)
    return symbols


def build_payload(symbols: list[str], pdf_path: Path) -> dict:
    return {
        "schema_version": 1,
        "source": {
            "name": "BIST_Tum_Hisseler_Listesi.pdf",
            "original_file": pdf_path.name,
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "instrument_count": len(symbols),
            "note": "PDF'deki kodlar değişmeden, ilk görünme sırasıyla aktarılmıştır.",
        },
        "instruments": [
            {
                "symbol": symbol,
                "exchange": "BIST",
                "provider_symbol": f"{symbol}.IS",
                "active": True,
            }
            for symbol in symbols
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-count", type=int, default=None)
    args = parser.parse_args()

    symbols = extract_symbols(args.pdf)
    if args.expected_count is not None and len(symbols) != args.expected_count:
        raise RuntimeError(
            f"PDF sembol sayısı beklenenden farklı: {len(symbols)} != {args.expected_count}"
        )
    payload = build_payload(symbols, args.pdf)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"{len(symbols)} sembol yazıldı: {args.output}")


if __name__ == "__main__":
    main()
