from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.config.settings import BASE_DIR


_SYMBOL_RE = re.compile(r"^[A-Z0-9^=.-]{3,24}$")


@dataclass(frozen=True)
class InstrumentDefinition:
    symbol: str
    exchange: str = "BIST"
    provider_symbol: str | None = None
    active: bool = True


def normalize_instrument(raw: str) -> str:
    symbol = str(raw or "").strip().upper()
    if symbol.endswith(".IS"):
        symbol = symbol[:-3]
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError(f"Geçersiz enstrüman kodu: {raw!r}")
    return symbol


def _deduplicate(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for raw in values:
        symbol = normalize_instrument(raw)
        if symbol not in seen:
            output.append(symbol)
            seen.add(symbol)
    return output


def parse_instruments_env(raw: str | None) -> list[str]:
    """INSTRUMENTS için JSON dizi veya virgüllü metin kabul eder."""

    value = (raw or "").strip()
    if not value:
        return []
    if value.startswith("["):
        parsed = json.loads(value)
        if not isinstance(parsed, list):
            raise ValueError("INSTRUMENTS JSON değeri bir liste olmalıdır.")
        return _deduplicate(str(item) for item in parsed)
    return _deduplicate(part for part in value.split(",") if part.strip())


def _resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else BASE_DIR / candidate


def load_universe(path: str | Path) -> list[InstrumentDefinition]:
    target = _resolve_path(path)
    if not target.exists():
        raise RuntimeError(f"BIST enstrüman evreni bulunamadı: {target}")
    payload = json.loads(target.read_text(encoding="utf-8"))
    rows = payload.get("instruments") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise RuntimeError("Enstrüman JSON dosyasında 'instruments' listesi bulunamadı.")

    output: list[InstrumentDefinition] = []
    seen: set[str] = set()
    for item in rows:
        if isinstance(item, str):
            item = {"symbol": item}
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        symbol = normalize_instrument(item["symbol"])
        if symbol in seen:
            continue
        output.append(
            InstrumentDefinition(
                symbol=symbol,
                exchange=str(item.get("exchange") or "BIST"),
                provider_symbol=item.get("provider_symbol"),
                active=bool(item.get("active", True)),
            )
        )
        seen.add(symbol)
    if not output:
        raise RuntimeError("Enstrüman evreni boş olamaz.")
    return output


def resolve_report_instruments(settings) -> list[str]:
    """Rapor evreninde env önceliklidir; boşsa ana endeks kullanılır.

    571 kodun tamamı Telegram'daki Tüm Hisseler ve evren taraması içindir.
    Sabah/akşam otomatik raporda hangi enstrümanların yer alacağı INSTRUMENTS
    ile ayrıca seçilir; böylece ücretsiz sağlayıcı gereksiz yere yüzlerce istek
    almaz.
    """

    configured = parse_instruments_env(getattr(settings, "instruments", ""))
    if configured:
        index_symbol = normalize_instrument(getattr(settings, "xu100_symbol", "XU100"))
        return _deduplicate([index_symbol, *configured])
    return [normalize_instrument(getattr(settings, "xu100_symbol", "XU100"))]


def universe_symbols(path: str | Path) -> list[str]:
    return [item.symbol for item in load_universe(path) if item.active]
