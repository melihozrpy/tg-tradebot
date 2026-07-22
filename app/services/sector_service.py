from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.config.settings import get_settings, load_sector_map, save_sector_map


@dataclass
class SectorInfo:
    symbol: str
    sector_name: str
    sector_index: str


def get_sector_info(symbol: str) -> Optional[SectorInfo]:
    """Sembolun sektor eslestirmesini doner. Mapping'de yoksa None doner
    (KESINLIKLE otomatik/tahmini bir sektor uydurulmaz).
    """
    settings = get_settings()
    sector_map = load_sector_map(settings.sector_map_path)
    entry = sector_map.get("symbols", {}).get(symbol.upper())
    if entry is None:
        return None
    return SectorInfo(
        symbol=symbol.upper(),
        sector_name=entry.get("sector_name", "Bilinmiyor"),
        sector_index=entry.get("sector_index", ""),
    )


def set_sector_mapping(symbol: str, sector_index: str, sector_name: str) -> SectorInfo:
    settings = get_settings()
    sector_map = load_sector_map(settings.sector_map_path)
    if "symbols" not in sector_map:
        sector_map["symbols"] = {}
    symbol = symbol.upper()
    sector_map["symbols"][symbol] = {"sector_name": sector_name, "sector_index": sector_index}
    save_sector_map(sector_map, settings.sector_map_path)
    return SectorInfo(symbol=symbol, sector_name=sector_name, sector_index=sector_index)


def list_sector_mappings() -> dict:
    settings = get_settings()
    return load_sector_map(settings.sector_map_path).get("symbols", {})
