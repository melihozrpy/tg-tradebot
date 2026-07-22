from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from app.analysis.indicator_engine import atr as compute_atr

MIN_BARS_FOR_LIQUIDITY = 25

LIQUIDITY_VERY_HIGH = "cok_yuksek"
LIQUIDITY_HIGH = "yuksek"
LIQUIDITY_MEDIUM = "orta"
LIQUIDITY_LOW = "dusuk"
LIQUIDITY_VERY_LOW = "cok_dusuk"
LIQUIDITY_INSUFFICIENT = "veri_yetersiz"

DEFAULT_LIQUIDITY_CONFIG = {
    "minimum_average_volume": 100_000.0,
    "minimum_average_turnover_try": 5_000_000.0,
    "maximum_atr_percent": 12.0,
    "strong_signal_minimum_score": 45.0,
}


@dataclass
class LiquidityResult:
    available: bool
    score: float = 0.0
    liquidity_class: str = LIQUIDITY_INSUFFICIENT
    avg_volume_20d: Optional[float] = None
    avg_volume_60d: Optional[float] = None
    avg_turnover_20d_try: Optional[float] = None
    last_turnover_try: Optional[float] = None
    relative_volume: Optional[float] = None
    volume_stability: Optional[float] = None  # 0-1 arasi, 1'e yakin = daha istikrarli
    volume_declining: bool = False
    abnormal_volume: bool = False
    price_volume_harmony: bool = True
    gap_frequency: Optional[float] = None  # son 20 barda gap orani (0-1)
    atr_percent: Optional[float] = None
    sudden_price_jump: bool = False
    body_wick_ratio: Optional[float] = None
    manipulation_risk: bool = False
    allow_strong_signal: bool = True
    risk_note: str = ""
    reasons: list[str] = field(default_factory=list)


def compute_liquidity(df: pd.DataFrame, config: Optional[dict] = None) -> LiquidityResult:
    """Bolum 5 spesifikasyonuna gore likidite skorunu (0-100) hesaplar.

    Yetersiz veri durumunda 'veri_yetersiz' doner ve HICBIR skor uydurmaz;
    cagiran taraf (sinyal/karar motoru) bu durumda guclu AL sinifini
    engellemeli ve acik risk uyarisi gostermelidir.
    """
    cfg = {**DEFAULT_LIQUIDITY_CONFIG, **(config or {})}

    if df is None or len(df) < MIN_BARS_FOR_LIQUIDITY:
        return LiquidityResult(available=False, risk_note="Likidite hesaplamak icin yeterli veri yok.")

    df = df.sort_values("timestamp").reset_index(drop=True)
    close, open_, high, low, volume = df["close"], df["open"], df["high"], df["low"], df["volume"]

    last20 = df.tail(20)
    last60 = df.tail(60) if len(df) >= 60 else df

    avg_volume_20 = float(last20["volume"].mean())
    avg_volume_60 = float(last60["volume"].mean())
    avg_turnover_20 = float((last20["close"] * last20["volume"]).mean())
    last_turnover = float(close.iloc[-1] * volume.iloc[-1])

    relative_volume = float(volume.iloc[-1] / avg_volume_20) if avg_volume_20 > 0 else 0.0

    vol_std = float(last20["volume"].std() or 0.0)
    stability = max(0.0, min(1.0, 1 - (vol_std / avg_volume_20))) if avg_volume_20 > 0 else 0.0
    volume_declining = bool(avg_volume_60 > 0 and avg_volume_20 < avg_volume_60 * 0.7)
    abnormal_volume = relative_volume >= 3.0

    recent = df.tail(5)
    price_change = recent["close"].pct_change().abs()
    vol_change = recent["volume"].pct_change()
    harmony = True
    if len(recent) >= 2:
        big_moves = price_change[price_change > 0.03]
        if not big_moves.empty:
            harmony = bool((vol_change.loc[big_moves.index].fillna(0) > 0).all())

    prev_close = close.shift(1)
    gap_pct = ((open_ - prev_close) / prev_close).abs()
    window = min(20, len(df))
    gap_days = int((gap_pct.tail(window) > 0.02).sum())
    gap_frequency = gap_days / window if window else 0.0

    atr_series = compute_atr(df, 14)
    atr_last = float(atr_series.iloc[-1]) if not atr_series.empty else 0.0
    atr_percent = (atr_last / float(close.iloc[-1]) * 100) if close.iloc[-1] else None

    daily_ret = close.pct_change().abs()
    sudden_price_jump = bool((daily_ret.tail(5) > 0.10).any())

    body = (close - open_).abs()
    wick = (high - low) - body
    wick_tail_mean = float(wick.tail(20).mean())
    body_wick_ratio = float(body.tail(20).mean() / wick_tail_mean) if wick_tail_mean > 0 else None

    manipulation_risk = bool(abnormal_volume and sudden_price_jump and gap_frequency > 0.2)

    reasons: list[str] = []
    score = 0.0

    min_vol = cfg["minimum_average_volume"]
    if avg_volume_20 >= min_vol * 5:
        score += 35
        reasons.append("Ortalama hacim cok yuksek")
    elif avg_volume_20 >= min_vol * 2:
        score += 25
        reasons.append("Ortalama hacim yuksek")
    elif avg_volume_20 >= min_vol:
        score += 15
        reasons.append("Ortalama hacim yeterli seviyede")
    else:
        reasons.append("Ortalama hacim minimum esigin altinda")

    min_turnover = cfg["minimum_average_turnover_try"]
    if avg_turnover_20 >= min_turnover * 5:
        score += 25
        reasons.append("Ortalama islem tutari cok yuksek")
    elif avg_turnover_20 >= min_turnover * 2:
        score += 18
        reasons.append("Ortalama islem tutari yuksek")
    elif avg_turnover_20 >= min_turnover:
        score += 10
        reasons.append("Ortalama islem tutari yeterli seviyede")
    else:
        reasons.append("Ortalama islem tutari minimum esigin altinda")

    score += stability * 15

    max_atr = cfg["maximum_atr_percent"]
    if atr_percent is not None:
        if atr_percent <= max_atr * 0.5:
            score += 15
        elif atr_percent <= max_atr:
            score += 8
        else:
            reasons.append(f"Gunluk ATR yuzdesi yuksek (%{atr_percent:.1f})")

    if gap_frequency <= 0.05:
        score += 10
    elif gap_frequency <= 0.15:
        score += 5
    else:
        reasons.append("Gap (fiyat sicramasi) sikligi yuksek")

    score = round(min(max(score, 0.0), 100.0), 1)

    if volume_declining:
        score = round(max(0.0, score - 8), 1)
        reasons.append("Hacim son donemde belirgin dusus egiliminde")
    if abnormal_volume:
        reasons.append("Anormal hacim tespit edildi")
    if not harmony:
        reasons.append("Fiyat-hacim uyumu zayif (buyuk hareket hacimle dogrulanmiyor)")
    if manipulation_risk:
        score = round(max(0.0, score - 15), 1)
        reasons.append("Manipulasyon riski gostergeleri mevcut (hacim+sicrama+gap birlikte)")

    if score >= 80:
        liquidity_class = LIQUIDITY_VERY_HIGH
    elif score >= 60:
        liquidity_class = LIQUIDITY_HIGH
    elif score >= 40:
        liquidity_class = LIQUIDITY_MEDIUM
    elif score >= 20:
        liquidity_class = LIQUIDITY_LOW
    else:
        liquidity_class = LIQUIDITY_VERY_LOW

    strong_min = cfg["strong_signal_minimum_score"]
    allow_strong_signal = score >= strong_min and not manipulation_risk

    risk_note = ""
    if liquidity_class in (LIQUIDITY_LOW, LIQUIDITY_VERY_LOW):
        risk_note = "Ani fiyat hareketi ve yuksek kayma (slippage) riski."

    return LiquidityResult(
        available=True,
        score=score,
        liquidity_class=liquidity_class,
        avg_volume_20d=round(avg_volume_20, 0),
        avg_volume_60d=round(avg_volume_60, 0),
        avg_turnover_20d_try=round(avg_turnover_20, 2),
        last_turnover_try=round(last_turnover, 2),
        relative_volume=round(relative_volume, 2),
        volume_stability=round(stability, 2),
        volume_declining=volume_declining,
        abnormal_volume=abnormal_volume,
        price_volume_harmony=harmony,
        gap_frequency=round(gap_frequency, 2),
        atr_percent=round(atr_percent, 2) if atr_percent is not None else None,
        sudden_price_jump=sudden_price_jump,
        body_wick_ratio=round(body_wick_ratio, 2) if body_wick_ratio is not None else None,
        manipulation_risk=manipulation_risk,
        allow_strong_signal=allow_strong_signal,
        risk_note=risk_note,
        reasons=reasons,
    )
