from __future__ import annotations

"""SMXM/ICT bölgelerinden tek, açıklanabilir ana işlem senaryosu üretir.

Bu motor fiyat tahmini yapmaz. Grafikte gerçekten tespit edilmiş OB/FVG
bölgelerini; son MSS/BOS yönünü, yapısal destek/dirençleri ve ATR'yi birlikte
değerlendirir. Böylece Telegram mesajında birbirine karışan LONG/SHORT planları
yerine bir ana bölge, bir koşullu senaryo ve en fazla bir alternatif gösterilir.
"""

from dataclasses import dataclass
from math import isfinite
from typing import Iterable, Sequence

from app.analysis.smart_money_engine import PriceZone, SmartMoneyResult, StructureEvent


@dataclass(frozen=True)
class AlternativeQualityZone:
    kind: str
    low: float
    high: float
    direction: str
    distance_points: float
    distance_percent: float


@dataclass(frozen=True)
class QualityZoneScenario:
    zone_kind: str
    zone_low: float
    zone_high: float
    direction: str
    location: str
    distance_points: float
    distance_percent: float
    entry: float
    invalidation: float
    target_1: float | None
    target_1_label: str | None
    target_2: float | None
    target_2_label: str | None
    rr_1: float | None
    rr_2: float | None
    quality_score: int
    structure_kind: str | None
    structure_direction: str | None
    structure_confirmed: bool
    rr_is_sufficient: bool
    alternative: AlternativeQualityZone | None = None


def _last_structure(smart: SmartMoneyResult) -> StructureEvent | None:
    return next((event for event in reversed(smart.structure) if event.kind == "MSS"), None) or (
        smart.structure[-1] if smart.structure else None
    )


def _zone_distance(zone: PriceZone, current_price: float) -> tuple[float, str]:
    if zone.low <= current_price <= zone.high:
        return 0.0, "içinde"
    if zone.high < current_price:
        return current_price - zone.high, "altında"
    return zone.low - current_price, "üstünde"


def _entry_for_zone(zone: PriceZone, current_price: float) -> float:
    if zone.kind == "OB":
        return (float(zone.low) + float(zone.high)) / 2.0
    # FVG ilk dokunuşu: fiyat yukarıdaysa üst sınır, aşağıdaysa alt sınır.
    if current_price >= zone.high:
        return float(zone.high)
    if current_price <= zone.low:
        return float(zone.low)
    return current_price


def _target_candidates(
    smart: SmartMoneyResult,
    *,
    entry: float,
    current_price: float,
    direction: str,
    support_levels: Sequence[float],
    resistance_levels: Sequence[float],
) -> list[tuple[float, str]]:
    candidates: list[tuple[float, str]] = []
    if direction == "LONG":
        candidates.extend((float(level), "yapısal direnç") for level in resistance_levels)
        candidates.extend(
            (float(zone.low), f"karşı {zone.kind}")
            for zone in (*smart.order_blocks, *smart.fvg)
            if zone.direction == "bearish"
        )
        candidates.extend(
            (float(event.price), f"{event.kind} / üst likidite")
            for event in smart.structure
            if event.direction == "bullish"
        )
        # Retest planında güncel fiyatın zaten geçtiği eski likidite hedef olmaz.
        candidates = [item for item in candidates if item[0] > max(entry, current_price)]
        candidates.sort(key=lambda item: item[0])
    else:
        candidates.extend((float(level), "yapısal destek") for level in support_levels)
        candidates.extend(
            (float(zone.high), f"karşı {zone.kind}")
            for zone in (*smart.order_blocks, *smart.fvg)
            if zone.direction == "bullish"
        )
        candidates.extend(
            (float(event.price), f"{event.kind} / alt likidite")
            for event in smart.structure
            if event.direction == "bearish"
        )
        candidates = [item for item in candidates if 0 < item[0] < min(entry, current_price)]
        candidates.sort(key=lambda item: item[0], reverse=True)

    unique: list[tuple[float, str]] = []
    seen: set[float] = set()
    for price, label in candidates:
        key = round(price, 8)
        if key in seen or not isfinite(price):
            continue
        seen.add(key)
        unique.append((price, label))
    return unique


def _risk_reward(entry: float, stop: float, target: float | None, direction: str) -> float | None:
    if target is None:
        return None
    risk = abs(entry - stop)
    reward = target - entry if direction == "LONG" else entry - target
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def _rank_zone(
    zone: PriceZone,
    *,
    current_price: float,
    atr_value: float,
    structure: StructureEvent | None,
) -> tuple[float, float]:
    distance, _location = _zone_distance(zone, current_price)
    distance_in_atr = distance / atr_value
    proximity = max(0.0, 48.0 - distance_in_atr * 18.0)
    zone_quality = 24.0 if zone.kind == "OB" else 19.0
    expected = "bullish" if zone.direction == "bullish" else "bearish"
    structure_score = 0.0
    if structure is not None:
        structure_score = 25.0 if structure.direction == expected else -16.0
        if structure.kind == "MSS" and structure.direction == expected:
            structure_score += 8.0
    recency = min(10.0, max(0.0, float(zone.index)) * 0.01)
    return zone_quality + proximity + structure_score + recency, distance


def select_closest_quality_zone(
    current_price: float,
    atr_value: float,
    smart: SmartMoneyResult,
    *,
    support_levels: Sequence[float] = (),
    resistance_levels: Sequence[float] = (),
) -> QualityZoneScenario | None:
    """En yakın *kaliteli* OB/FVG'yi seçer ve tek ana senaryo üretir.

    Yakınlık tek ölçüt değildir: son MSS/BOS ile ters düşen çok yakın bir bölge,
    aynı yönde teyitli ve makul mesafedeki bölgenin önüne geçirilmez.
    """

    if current_price <= 0 or atr_value <= 0:
        return None
    zones = [
        zone
        for zone in (*smart.order_blocks, *smart.fvg)
        if isfinite(zone.low) and isfinite(zone.high) and 0 < zone.low < zone.high
        and (
            (zone.direction == "bullish" and zone.low <= current_price)
            or (zone.direction == "bearish" and zone.high >= current_price)
        )
    ]
    if not zones:
        return None

    structure = _last_structure(smart)
    ranked = sorted(
        (
            (*_rank_zone(zone, current_price=current_price, atr_value=atr_value, structure=structure), zone)
            for zone in zones
        ),
        key=lambda item: (-item[0], item[1], -item[2].index),
    )
    raw_score, distance, zone = ranked[0]
    direction = "LONG" if zone.direction == "bullish" else "SHORT"
    _distance, location = _zone_distance(zone, current_price)
    entry = _entry_for_zone(zone, current_price)
    buffer = max((zone.high - zone.low) * 0.08, atr_value * 0.08)
    invalidation = zone.low - buffer if direction == "LONG" else zone.high + buffer

    targets = _target_candidates(
        smart,
        entry=entry,
        current_price=current_price,
        direction=direction,
        support_levels=support_levels,
        resistance_levels=resistance_levels,
    )
    target_1, label_1 = targets[0] if targets else (None, None)
    target_2, label_2 = targets[1] if len(targets) > 1 else (None, None)
    rr_1 = _risk_reward(entry, invalidation, target_1, direction)
    rr_2 = _risk_reward(entry, invalidation, target_2, direction)
    best_rr = max((value for value in (rr_1, rr_2) if value is not None), default=0.0)

    expected_structure = "bullish" if direction == "LONG" else "bearish"
    structure_confirmed = bool(structure and structure.direction == expected_structure)
    quality_score = max(0, min(100, round(raw_score + min(best_rr, 3.0) * 3.0)))

    alternative = None
    for _alt_score, alt_distance, alt in ranked[1:]:
        if alt_distance > atr_value * 1.5:
            continue
        alt_direction = "LONG" if alt.direction == "bullish" else "SHORT"
        if alt.kind == zone.kind and alt.direction == zone.direction and abs(alt.low - zone.low) < 1e-8:
            continue
        alternative = AlternativeQualityZone(
            kind=alt.kind,
            low=float(alt.low),
            high=float(alt.high),
            direction=alt_direction,
            distance_points=alt_distance,
            distance_percent=alt_distance / current_price * 100.0,
        )
        break

    return QualityZoneScenario(
        zone_kind=zone.kind,
        zone_low=float(zone.low),
        zone_high=float(zone.high),
        direction=direction,
        location=location,
        distance_points=distance,
        distance_percent=distance / current_price * 100.0,
        entry=entry,
        invalidation=invalidation,
        target_1=target_1,
        target_1_label=label_1,
        target_2=target_2,
        target_2_label=label_2,
        rr_1=rr_1,
        rr_2=rr_2,
        quality_score=quality_score,
        structure_kind=structure.kind if structure else None,
        structure_direction=structure.direction if structure else None,
        structure_confirmed=structure_confirmed,
        rr_is_sufficient=best_rr >= 2.0,
        alternative=alternative,
    )


def _price(value: float | None, decimals: int) -> str:
    return "hesaplanamadı" if value is None else f"{value:.{decimals}f}"


def format_quality_zone_scenario(scenario: QualityZoneScenario, *, decimals: int = 2) -> str:
    direction_word = "yukarı yön" if scenario.direction == "LONG" else "aşağı yön"
    lines = [
        f"🎯 EN YAKIN KALİTELİ BÖLGE: {scenario.zone_kind} "
        f"{scenario.zone_low:.{decimals}f}-{scenario.zone_high:.{decimals}f}",
        f"Konum: Güncel fiyatın {scenario.distance_points:.{decimals}f} puan / "
        f"%{scenario.distance_percent:.2f} {scenario.location}",
        f"Senaryo: {scenario.direction} — \"Bölge retest edilirse {direction_word} teyidi aranır\"",
        "",
        f"✅ Giriş: {scenario.entry:.{decimals}f} "
        f"({'OB orta noktası (%50)' if scenario.zone_kind == 'OB' else 'FVG ilk dokunuşu'})",
        f"🛑 Invalidation (Stop): {scenario.invalidation:.{decimals}f} — bölge dışı kapanış",
        f"🎯 TP1: {_price(scenario.target_1, decimals)}"
        + (f" ({scenario.target_1_label})" if scenario.target_1_label else ""),
        f"🎯 TP2: {_price(scenario.target_2, decimals)}"
        + (f" ({scenario.target_2_label})" if scenario.target_2_label else ""),
    ]
    rr_parts = []
    if scenario.rr_1 is not None:
        rr_parts.append(f"TP1 1:{scenario.rr_1:.2f}")
    if scenario.rr_2 is not None:
        rr_parts.append(f"TP2 1:{scenario.rr_2:.2f}")
    lines.append(f"⚖️ RR: {' • '.join(rr_parts) if rr_parts else 'hesaplanamadı'}")
    if not scenario.rr_is_sufficient:
        lines.append("⚠️ UYARI: Ulaşılabilir yapısal hedeflerde minimum 1:2 RR yok; işlem alma.")
    if scenario.structure_kind:
        state = "yön teyidi" if scenario.structure_confirmed else "yönle çelişiyor"
        lines.append(
            f"🧭 Son {scenario.structure_kind}: {scenario.structure_direction} — {state}; "
            "mum kapanışı/retest görülmeden senaryo aktif değildir."
        )
    else:
        lines.append("🧭 MSS/BOS teyidi yok; yapı teyidi oluşmadan senaryo aktif değildir.")
    if scenario.alternative is not None:
        alt = scenario.alternative
        lines.append(
            f"↪️ Alternatif bölge: {alt.kind} {alt.low:.{decimals}f}-{alt.high:.{decimals}f} "
            f"• {alt.direction} • %{alt.distance_percent:.2f} uzakta"
        )
    lines.append("ℹ️ Güncel fiyattan doğrudan giriş önerilmez; yalnız retest + yapı/hacim teyidi izlenir.")
    return "\n".join(lines)
