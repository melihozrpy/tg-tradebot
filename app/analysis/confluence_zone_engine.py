from __future__ import annotations

"""MERGEN QUANT - Asama 5, Bolum 3: Cakisan guclu destek/direnc bolgeleri.

Gunluk, haftalik ve aylik seviyeler birbirine yakinsa (fiyat yakinligi +
farkli zaman dilimi sayisi + temas + hacim + yas + trend) bunlari tek bir
"cakisan guclu bolge" olarak birlestirir. Puan yapay sekilde 100'e
tamamlanmaz; her bilesenin katkisi sinirlidir ve aciklanabilirdir.
"""

from dataclasses import dataclass, field
from typing import Optional

from app.analysis.timeframe_levels_engine import LevelDetail, MultiTimeframeLevelsResult

CONFLUENCE_PRICE_TOLERANCE_PCT = 0.015  # ayni bolge sayilmasi icin fiyatin ~%1.5'i

# Cakisma guven puani bilesenleri (toplamda en fazla 100, yapay tamamlama yok).
TIMEFRAME_DIVERSITY_WEIGHT = 12.0  # her farkli zaman dilimi icin
MAX_TIMEFRAME_BONUS = 36.0  # en fazla 3 zaman dilimi (gunluk+haftalik+aylik)
TOUCH_WEIGHT = 3.0
MAX_TOUCH_BONUS = 24.0
VOLUME_BONUS = 10.0
BASE_FROM_AVG_CONFIDENCE_WEIGHT = 0.30  # ortalama tekil guvenin katkisi


@dataclass
class ConfluenceZone:
    kind: str  # "destek" | "direnc"
    low: float
    high: float
    mid: float
    confidence: float
    timeframes: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    total_touches: int = 0
    volume_confirmed: bool = False
    description: str = ""
    successful_reactions: int = 0
    failed_tests: int = 0
    strength_class: str = "Veri yetersiz"
    active: bool = True
    invalidation_condition: str = ""


def _overlaps(a: LevelDetail, b: LevelDetail, current_price: float) -> bool:
    tolerance = current_price * CONFLUENCE_PRICE_TOLERANCE_PCT
    bands_overlap = max(a.low, b.low) <= min(a.high, b.high)
    return bands_overlap or abs(a.mid - b.mid) <= tolerance


def _merge_group(levels: list[LevelDetail], kind: str, current_price: float) -> ConfluenceZone:
    low = min(lvl.low for lvl in levels)
    high = max(lvl.high for lvl in levels)
    mid = sum(lvl.mid for lvl in levels) / len(levels)
    timeframes = list(dict.fromkeys(lvl.timeframe for lvl in levels))
    sources: list[str] = []
    for lvl in levels:
        for src in lvl.sources:
            if src not in sources:
                sources.append(src)
    total_touches = sum(lvl.touches for lvl in levels)
    volume_confirmed = any(lvl.volume_confirmed for lvl in levels)
    successful_reactions = sum(getattr(lvl, "successful_reactions", lvl.rejections) for lvl in levels)
    failed_tests = sum(getattr(lvl, "failed_tests", 0) for lvl in levels)
    avg_single_confidence = sum(lvl.confidence for lvl in levels) / len(levels)

    timeframe_bonus = min(MAX_TIMEFRAME_BONUS, len(timeframes) * TIMEFRAME_DIVERSITY_WEIGHT)
    touch_bonus = min(MAX_TOUCH_BONUS, total_touches * TOUCH_WEIGHT)
    volume_bonus = VOLUME_BONUS if volume_confirmed else 0.0
    base = avg_single_confidence * BASE_FROM_AVG_CONFIDENCE_WEIGHT

    reaction_bonus = min(12.0, successful_reactions * 2.0)
    failure_penalty = min(18.0, failed_tests * 3.0)
    confidence = round(min(97.0, max(0.0, base + timeframe_bonus + touch_bonus + volume_bonus + reaction_bonus - failure_penalty)), 1)
    if confidence >= 82 and len(timeframes) >= 3:
        strength_class = "Çok güçlü"
    elif confidence >= 68:
        strength_class = "Güçlü"
    elif confidence >= 48:
        strength_class = "Orta"
    else:
        strength_class = "Zayıf"
    active = all(getattr(lvl, "active", True) for lvl in levels)
    invalidation = (
        f"{low:.2f} altında doğrulanmış kapanış"
        if kind == "destek"
        else f"{high:.2f} üzerinde doğrulanmış kapanış"
    )

    tf_text = ", ".join(sorted(timeframes))
    description = (
        f"{len(timeframes)} zaman diliminde ({tf_text}) dogrulanan, "
        f"toplam {total_touches} temasli {kind} bolgesi."
    )

    return ConfluenceZone(
        kind=kind,
        low=round(low, 2),
        high=round(high, 2),
        mid=round(mid, 2),
        confidence=confidence,
        timeframes=timeframes,
        sources=sources,
        total_touches=total_touches,
        volume_confirmed=volume_confirmed,
        description=description,
        successful_reactions=successful_reactions,
        failed_tests=failed_tests,
        strength_class=strength_class,
        active=active,
        invalidation_condition=invalidation,
    )


def _find_confluences(levels: list[LevelDetail], kind: str, current_price: float) -> list[ConfluenceZone]:
    if not levels:
        return []
    sorted_levels = sorted((lvl for lvl in levels if getattr(lvl, "active", True)), key=lambda lvl: lvl.mid)
    groups: list[list[LevelDetail]] = []
    for lvl in sorted_levels:
        placed = False
        for group in groups:
            if any(_overlaps(lvl, member, current_price) for member in group):
                group.append(lvl)
                placed = True
                break
        if not placed:
            groups.append([lvl])

    zones: list[ConfluenceZone] = []
    for group in groups:
        # Yalnizca en az 2 FARKLI zaman diliminden gelen cakismalar "guclu
        # ortak bolge" sayilir; tek zaman diliminden gelen tekil seviyeler
        # burada tekrar listelenmez (onlar zaten TimeframeLevelResult'ta var).
        distinct_timeframes = {lvl.timeframe for lvl in group}
        if len(distinct_timeframes) < 2:
            continue
        zones.append(_merge_group(group, kind, current_price))

    zones.sort(key=lambda z: -z.confidence)
    return zones


def find_confluence_zones(
    levels_result: MultiTimeframeLevelsResult, current_price: float
) -> tuple[list[ConfluenceZone], list[ConfluenceZone]]:
    """Butun (gunluk+haftalik+aylik) destek/direnc seviyeleri arasinda
    cakisan guclu bolgeleri bulur. Fiyatin altindaki cakismalar destek,
    ustundeki cakismalar direnc olarak degerlendirilir.
    """
    all_levels = levels_result.all_zones()
    support_candidates = [lvl for lvl in all_levels if lvl.mid <= current_price]
    resistance_candidates = [lvl for lvl in all_levels if lvl.mid > current_price]

    support_zones = _find_confluences(support_candidates, "destek", current_price)
    resistance_zones = _find_confluences(resistance_candidates, "direnc", current_price)
    return support_zones, resistance_zones


def strongest_confluence(
    levels_result: MultiTimeframeLevelsResult, current_price: float
) -> tuple[Optional[ConfluenceZone], Optional[ConfluenceZone]]:
    supports, resistances = find_confluence_zones(levels_result, current_price)
    best_support = supports[0] if supports else None
    best_resistance = resistances[0] if resistances else None
    return best_support, best_resistance
