from __future__ import annotations

"""MERGEN QUANT - Asama 5b, Bolum 4: Dusus/yukselis senaryo bolgeleri.

"Bu hisse kesin sunu fiyata duser/cikar" dili KULLANILMAZ. Bunun yerine,
mevcut cok-zamanli destek/direnc ve cakisan bolge ciktilarindan turetilen,
GUVEN PUANLI ve AKTIVASYON KOSULU acik olan senaryo bolgeleri uretilir.

Girdi olarak agir hesaplama motorlarini (indicator/timeframe/confluence)
TEKRAR calistirmaz; onlarin ciktilarini (LevelDetail/ConfluenceZone benzeri
nesneler, ATR, EMA100/200, likidite) parametre olarak alir - boylece test
edilebilir ve mevcut motorlarla gevsek baglidir (loosely coupled).
"""

from dataclasses import dataclass, field
from typing import Optional, Protocol

from app.analysis.timeframe_levels_engine import MultiTimeframeLevelsResult

RELIABLE_NOTE = "Guvenilir senaryo hesaplanamadi."

# Ayni fiyat bolgesi sayilmasi icin tolerans (fiyatin yuzdesi).
TIER_TOLERANCE_PCT = 0.02
# "Asiri" senaryo bolgesinin makul sayilmasi icin ust sinir: fiyatin bu
# yuzdesinden daha uzaktaki seviyeler "cok uzak ve anlamsiz" kabul edilip
# filtrelenir.
MAX_EXTREME_DISTANCE_PCT = 0.35
# Likiditesi dusuk hisselerde uzak senaryolara guven carpanla dusurulur.
LOW_LIQUIDITY_CONFIDENCE_FACTOR = 0.6


class _ZoneLike(Protocol):
    low: float
    high: float
    mid: float
    confidence: float


@dataclass
class ScenarioZone:
    direction: str  # "dusus" | "yukselis"
    tier: str  # yakin|ana|guclu_kirilim|asiri
    low: float
    high: float
    confidence: float
    activation_condition: str
    sources: list[str] = field(default_factory=list)

    def as_range_text(self) -> str:
        return f"{self.low:.2f}-{self.high:.2f} TL"


@dataclass
class PriceScenarioResult:
    current_price: float
    reliable: bool
    note: str
    decline_near: Optional[ScenarioZone] = None
    decline_main: Optional[ScenarioZone] = None
    decline_extreme: Optional[ScenarioZone] = None
    rise_near: Optional[ScenarioZone] = None
    rise_main: Optional[ScenarioZone] = None
    rise_breakout: Optional[ScenarioZone] = None
    rise_extreme: Optional[ScenarioZone] = None


def _collect_tiers(zones: list[_ZoneLike], current_price: float) -> list[_ZoneLike]:
    """Aday bolgeleri fiyata olan mesafeye gore siralar ve birbirine cok
    yakin olanlari (TIER_TOLERANCE_PCT icinde) tek bir kademe (tier) olarak
    birlestirir - boylece 'ayni bolgeye' ait 3-5 farkli aday tek senaryo
    kademesi olarak sayilmaz."""
    if not zones:
        return []
    tolerance = current_price * TIER_TOLERANCE_PCT
    sorted_zones = sorted(zones, key=lambda z: z.mid)

    tiers: list[list[_ZoneLike]] = []
    for z in sorted_zones:
        if tiers and abs(z.mid - tiers[-1][-1].mid) <= tolerance:
            tiers[-1].append(z)
        else:
            tiers.append([z])

    merged: list[_ZoneLike] = []
    for group in tiers:
        low = min(g.low for g in group)
        high = max(g.high for g in group)
        mid = sum(g.mid for g in group) / len(group)
        confidence = max(g.confidence for g in group)
        merged.append(_MergedTier(low=low, high=high, mid=mid, confidence=confidence))
    return merged


@dataclass
class _MergedTier:
    low: float
    high: float
    mid: float
    confidence: float


def _within_extreme_range(zone: _ZoneLike, current_price: float) -> bool:
    distance_pct = abs(zone.mid - current_price) / current_price
    return distance_pct <= MAX_EXTREME_DISTANCE_PCT


def _liquidity_adjusted_confidence(confidence: float, liquidity_score: Optional[float]) -> float:
    if liquidity_score is not None and liquidity_score < 45.0:
        return round(confidence * LOW_LIQUIDITY_CONFIDENCE_FACTOR, 1)
    return confidence


def compute_price_scenarios(
    levels_result: MultiTimeframeLevelsResult,
    confluence_supports: list,
    confluence_resistances: list,
    current_price: float,
    liquidity_score: Optional[float] = None,
) -> PriceScenarioResult:
    """Gunluk/haftalik/aylik seviyeler + cakisan bolgelerden dusus/yukselis
    senaryo bolgeleri turetir. Yeterli teknik dayanak yoksa (ornegin ana
    veya asiri kademe icin ayirt edici ikinci/ucuncu bir seviye yoksa)
    ilgili senaryo alani None birakilir - uydurulmaz.
    """
    all_support_candidates: list[_ZoneLike] = []
    all_resistance_candidates: list[_ZoneLike] = []

    for tf_result in (levels_result.daily, levels_result.weekly, levels_result.monthly):
        if not tf_result.reliable:
            continue
        for lvl in (tf_result.support_1, tf_result.support_2, tf_result.main_support):
            if lvl is not None:
                all_support_candidates.append(lvl)
        for lvl in (tf_result.resistance_1, tf_result.resistance_2, tf_result.main_resistance):
            if lvl is not None:
                all_resistance_candidates.append(lvl)

    all_support_candidates.extend(confluence_supports)
    all_resistance_candidates.extend(confluence_resistances)

    support_tiers = sorted(
        [z for z in _collect_tiers(all_support_candidates, current_price) if z.mid < current_price],
        key=lambda z: -z.mid,  # fiyata en yakindan en uzaga
    )
    resistance_tiers = sorted(
        [z for z in _collect_tiers(all_resistance_candidates, current_price) if z.mid > current_price],
        key=lambda z: z.mid,  # fiyata en yakindan en uzaga
    )

    if not support_tiers and not resistance_tiers:
        return PriceScenarioResult(current_price=current_price, reliable=False, note=RELIABLE_NOTE)

    def make_zone(tier: _MergedTier, direction: str, tier_name: str, condition: str) -> ScenarioZone:
        confidence = _liquidity_adjusted_confidence(tier.confidence, liquidity_score)
        return ScenarioZone(
            direction=direction,
            tier=tier_name,
            low=round(tier.low, 2),
            high=round(tier.high, 2),
            confidence=confidence,
            activation_condition=condition,
        )

    # --- DUSUS SENARYOLARI ---
    decline_near = decline_main = decline_extreme = None
    if len(support_tiers) >= 1:
        t = support_tiers[0]
        decline_near = make_zone(
            t, "dusus", "yakin",
            f"Bu senaryo {t.high:.2f} TL altinda gunluk kapanislarla guclenir.",
        )
    if len(support_tiers) >= 2:
        t = support_tiers[1]
        decline_main = make_zone(
            t, "dusus", "ana",
            f"Bu senaryo yalnizca {t.high:.2f} TL altinda haftalik kapanis ve artan satis hacmi ile guclenir.",
        )
    if len(support_tiers) >= 3 and _within_extreme_range(support_tiers[-1], current_price):
        t = support_tiers[-1]
        decline_extreme = make_zone(
            t, "dusus", "asiri",
            "Teknik olarak izlenen en uzak dusus senaryo bolgesi; yalnizca genis capli, "
            "hacimli ve uzun sureli satis baskisinda gundeme gelir.",
        )

    # --- YUKSELIS SENARYOLARI ---
    rise_near = rise_main = rise_breakout = rise_extreme = None
    if len(resistance_tiers) >= 1:
        t = resistance_tiers[0]
        rise_near = make_zone(
            t, "yukselis", "yakin",
            f"Bu senaryo {t.low:.2f} TL uzerinde hacimli gunluk kapanis ile guclenir.",
        )
    if len(resistance_tiers) >= 2:
        t = resistance_tiers[1]
        rise_main = make_zone(
            t, "yukselis", "ana",
            f"Bu senaryo yalnizca {t.low:.2f} TL uzerinde haftalik kapanis ve hacim onayi ile guclenir.",
        )
    if len(resistance_tiers) >= 3:
        t = resistance_tiers[2]
        rise_breakout = make_zone(
            t, "yukselis", "guclu_kirilim",
            f"Bu senaryo, ana direnc bolgesinin hacimli kirilip {t.low:.2f} TL uzerinde "
            f"kalinmasi durumunda gundeme gelir.",
        )
    if len(resistance_tiers) >= 4 and _within_extreme_range(resistance_tiers[-1], current_price):
        t = resistance_tiers[-1]
        rise_extreme = make_zone(
            t, "yukselis", "asiri",
            "Teknik olarak izlenen en uzak yukselis senaryo bolgesi; yalnizca guclu "
            "kirilim sonrasi surdurulen alim ile gundeme gelir.",
        )

    return PriceScenarioResult(
        current_price=current_price,
        reliable=True,
        note="",
        decline_near=decline_near,
        decline_main=decline_main,
        decline_extreme=decline_extreme,
        rise_near=rise_near,
        rise_main=rise_main,
        rise_breakout=rise_breakout,
        rise_extreme=rise_extreme,
    )
