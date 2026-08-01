from __future__ import annotations

"""MERGEN QUANT - Asama 5b, Bolum 5: "Bu seviye kirilirsa ne olur?" motoru.

En onemli direnc/destek bolgesi icin: kirilimin gecerli sayilacagi kapanis
seviyesi, gerekli minimum hacim dogrulamasi, ilk/ikinci hedef, basarisiz
kirilim (sahte kirilim) seviyesi ve riski uretir. Deterministiktir; Groq
veya baska bir LLM burada fiyat/hedef/karar URETMEZ.
"""

from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

MIN_VOLUME_MULTIPLIER = 1.3  # ortalamanin en az bu kati hacim -> "hacimli kirilim"
FALSE_BREAKOUT_LOW_RISK = "dusuk"
FALSE_BREAKOUT_MEDIUM_RISK = "orta"
FALSE_BREAKOUT_HIGH_RISK = "yuksek"


class _ZoneLike(Protocol):
    low: float
    high: float
    mid: float
    confidence: float


@dataclass
class BreakoutCase:
    kind: str  # "direnc_kirilimi" | "destek_kirilimi"
    level_low: float
    level_high: float
    confirmation_close_level: float
    min_volume_note: str
    target_1: Optional[float]
    target_2: Optional[float]
    failure_level: Optional[float]
    false_breakout_risk: str
    false_breakout_note: str
    volume_currently_confirmed: bool
    target_1_reason: str = "ATR ölçülü hareket"
    target_2_reason: str = "ATR ölçülü hareket"
    level_already_broken: bool = False


@dataclass
class BreakoutScenarioResult:
    reliable: bool
    note: str
    resistance_breakout: Optional[BreakoutCase] = None
    support_breakdown: Optional[BreakoutCase] = None


def _measured_move_targets(level_low: float, level_high: float, zone_height_source: float) -> tuple[float, float]:
    """Kirilim sonrasi ilk/ikinci hedefi, kirilan bolgenin genisligine
    (veya ATR'ye) dayali basit bir 'olculu hareket' (measured move) ile
    turetir. Kesin bir tahmin degil, teknik olarak izlenen bir projeksiyondur.
    """
    height = max(level_high - level_low, zone_height_source * 0.5)
    target_1 = round(level_high + height, 2)
    target_2 = round(level_high + height * 2, 2)
    return target_1, target_2


def _next_structural_targets(
    levels: Sequence[tuple[float, str]],
    *,
    boundary: float,
    direction: str,
) -> list[tuple[float, str]]:
    """Kırılan seviyeden sonraki gerçek PD Array/swing seviyelerini sıralar."""
    clean: list[tuple[float, str]] = []
    seen: set[float] = set()
    for raw_price, raw_reason in levels:
        price = float(raw_price)
        key = round(price, 8)
        if price <= 0 or key in seen:
            continue
        if direction == "up" and price <= boundary:
            continue
        if direction == "down" and price >= boundary:
            continue
        seen.add(key)
        clean.append((price, str(raw_reason).strip() or "yapısal seviye"))
    clean.sort(key=lambda item: item[0], reverse=direction == "down")
    return clean


def _false_breakout_risk(adx: Optional[float], relative_volume: Optional[float], liquidity_score: Optional[float]) -> tuple[str, str]:
    """ADX (trend gucu), goreceli hacim ve likiditeye gore basit, aciklanabilir
    bir sahte-kirilim riski siniflandirmasi doner (risk seviyesi, aciklama)."""
    risk_points = 0
    reasons = []
    if adx is not None and adx < 18:
        risk_points += 1
        reasons.append("zayif trend gucu (ADX dusuk)")
    if relative_volume is not None and relative_volume < MIN_VOLUME_MULTIPLIER:
        risk_points += 1
        reasons.append("hacim onayi yetersiz")
    if liquidity_score is not None and liquidity_score < 45.0:
        risk_points += 1
        reasons.append("dusuk likidite")

    if risk_points >= 2:
        level = FALSE_BREAKOUT_HIGH_RISK
    elif risk_points == 1:
        level = FALSE_BREAKOUT_MEDIUM_RISK
    else:
        level = FALSE_BREAKOUT_LOW_RISK

    note = ", ".join(reasons) if reasons else "hacim ve trend gucu kirilimi destekliyor"
    return level, note


def compute_breakout_scenarios(
    resistance_zone: Optional[_ZoneLike],
    support_zone: Optional[_ZoneLike],
    current_price: float,
    atr_value: float,
    relative_volume: Optional[float] = None,
    adx: Optional[float] = None,
    liquidity_score: Optional[float] = None,
    pd_array_levels: Sequence[tuple[float, str]] = (),
) -> BreakoutScenarioResult:
    """En onemli direnc (resistance_zone) ve destek (support_zone) icin
    kirilim senaryolarini hesaplar. Her ikisi de None ise (guvenilir seviye
    yoksa) sonuc `reliable=False` doner - hicbir seviye uydurulmaz.
    """
    if resistance_zone is None and support_zone is None:
        return BreakoutScenarioResult(reliable=False, note="Guvenilir seviye hesaplanamadi.")

    resistance_case = None
    if resistance_zone is not None:
        dynamic = _next_structural_targets(
            pd_array_levels,
            boundary=float(resistance_zone.high),
            direction="up",
        )
        measured_1, measured_2 = _measured_move_targets(resistance_zone.low, resistance_zone.high, atr_value)
        target_1, reason_1 = dynamic[0] if dynamic else (measured_1, "ATR ölçülü hareket (yedek)")
        target_2, reason_2 = (
            dynamic[1]
            if len(dynamic) > 1
            else (max(measured_2, target_1 + max(resistance_zone.high - resistance_zone.low, atr_value * 0.5)), "ATR ölçülü hareket (yedek)")
        )
        risk_level, risk_note = _false_breakout_risk(adx, relative_volume, liquidity_score)
        volume_confirmed_now = relative_volume is not None and relative_volume >= MIN_VOLUME_MULTIPLIER
        resistance_case = BreakoutCase(
            kind="direnc_kirilimi",
            level_low=round(resistance_zone.low, 2),
            level_high=round(resistance_zone.high, 2),
            confirmation_close_level=round(resistance_zone.high, 2),
            min_volume_note=f"Ortalamanin en az {MIN_VOLUME_MULTIPLIER:.1f} kati hacimle dogrulanmali.",
            target_1=target_1,
            target_2=target_2,
            failure_level=round(resistance_zone.low, 2),
            false_breakout_risk=risk_level,
            false_breakout_note=risk_note,
            volume_currently_confirmed=volume_confirmed_now,
            target_1_reason=reason_1,
            target_2_reason=reason_2,
            level_already_broken=current_price > float(resistance_zone.high),
        )

    support_case = None
    if support_zone is not None:
        height = max(support_zone.high - support_zone.low, atr_value * 0.5)
        measured_1 = round(support_zone.low - height, 2)
        measured_2 = round(support_zone.low - height * 2, 2)
        dynamic = _next_structural_targets(
            pd_array_levels,
            boundary=float(support_zone.low),
            direction="down",
        )
        first_decline_low, reason_1 = dynamic[0] if dynamic else (measured_1, "ATR ölçülü hareket (yedek)")
        main_dip_low, reason_2 = (
            dynamic[1]
            if len(dynamic) > 1
            else (min(measured_2, first_decline_low - height), "ATR ölçülü hareket (yedek)")
        )
        risk_level, risk_note = _false_breakout_risk(adx, relative_volume, liquidity_score)
        volume_confirmed_now = relative_volume is not None and relative_volume >= MIN_VOLUME_MULTIPLIER
        support_case = BreakoutCase(
            kind="destek_kirilimi",
            level_low=round(support_zone.low, 2),
            level_high=round(support_zone.high, 2),
            confirmation_close_level=round(support_zone.low, 2),
            min_volume_note=f"Ortalamanin en az {MIN_VOLUME_MULTIPLIER:.1f} kati hacimle dogrulanmali.",
            target_1=first_decline_low,
            target_2=main_dip_low,
            failure_level=round(support_zone.high, 2),
            false_breakout_risk=risk_level,
            false_breakout_note=risk_note,
            volume_currently_confirmed=volume_confirmed_now,
            target_1_reason=reason_1,
            target_2_reason=reason_2,
            level_already_broken=current_price < float(support_zone.low),
        )

    return BreakoutScenarioResult(
        reliable=True,
        note="",
        resistance_breakout=resistance_case,
        support_breakdown=support_case,
    )
