from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.utils.financial_formatter import percent_change, price_multiple, round_money


@dataclass
class TargetRealismResult:
    current_price: float
    target_price: float
    required_change_percent: Optional[float]
    required_price_multiple: Optional[float]
    current_market_cap: Optional[float]
    target_market_cap: Optional[float]
    market_cap_increase: Optional[float]
    technical_probability_class: str
    fundamental_support_class: str
    liquidity_risk: str
    valuation_risk: str
    speculation_risk: str
    manipulation_indicator: str
    realism_score: Optional[float]
    reasons: list[str] = field(default_factory=list)


def evaluate_target_realism(
    current_price: float,
    target_price: float,
    *,
    shares_outstanding: Optional[float] = None,
    current_market_cap: Optional[float] = None,
    free_float_ratio: Optional[float] = None,
    average_daily_turnover: Optional[float] = None,
    liquidity_score: Optional[float] = None,
    historical_volatility: Optional[float] = None,
    atr_percent: Optional[float] = None,
    earnings_growth: Optional[float] = None,
    equity_growth: Optional[float] = None,
    nav: Optional[float] = None,
    relative_strength: Optional[float] = None,
    abnormal_price_volume: bool = False,
    consecutive_limit_ups: int = 0,
    fundamental_available: bool = False,
) -> TargetRealismResult:
    current = round_money(current_price) or 0.0
    target = round_money(target_price) or 0.0
    change = percent_change(target, current)
    multiple = price_multiple(target, current)
    reasons: list[str] = []
    shares = float(shares_outstanding) if shares_outstanding and shares_outstanding > 0 else None
    current_cap = float(current_market_cap) if current_market_cap and current_market_cap > 0 else (
        current * shares if shares else None
    )
    target_cap = target * shares if shares else None
    cap_increase = target_cap - current_cap if target_cap is not None and current_cap is not None else None

    if change is None or target <= current:
        technical = "Mevcut verilerle desteklenmiyor"
        base_score = 10.0
    elif change <= 25:
        technical, base_score = "Makul", 75.0
    elif change <= 75:
        technical, base_score = "Agresif", 58.0
    elif change <= 200:
        technical, base_score = "Çok agresif", 38.0
    else:
        technical, base_score = "Aşırı spekülatif", 18.0
        reasons.append("Hedef için gereken fiyat katı çok yüksek")

    if liquidity_score is None:
        liquidity_risk = "Veri yetersiz"
        base_score -= 8
    elif liquidity_score < 30:
        liquidity_risk = "Çok yüksek"
        base_score -= 22
        reasons.append("Düşük likidite nedeniyle hareket kırılgan")
    elif liquidity_score < 50:
        liquidity_risk = "Yüksek"
        base_score -= 12
    else:
        liquidity_risk = "Düşük-Orta"
        base_score += 4

    positive_growth = any(value is not None and value > 10 for value in (earnings_growth, equity_growth))
    if not fundamental_available:
        fundamental = "Veri yetersiz"
        valuation_risk = "Veri yetersiz"
        base_score = min(base_score, 55.0)
        reasons.append("Temel veri bulunmadığı için yüksek gerçekçilik puanı verilmedi")
    elif positive_growth or nav is not None:
        fundamental = "Kısmen destekleniyor"
        valuation_risk = "Orta"
        base_score += 8
    else:
        fundamental = "Sınırlı destek"
        valuation_risk = "Yüksek"
        base_score = min(base_score, 48.0)

    if average_daily_turnover and cap_increase and average_daily_turnover > 0:
        turnover_days = cap_increase / average_daily_turnover
        if turnover_days > 500:
            base_score -= 12
            reasons.append("Gereken piyasa değeri artışı mevcut işlem hacmine göre çok büyük")
    if relative_strength is not None:
        base_score += max(-5.0, min(5.0, relative_strength / 20.0))
    if atr_percent is not None and atr_percent > 10:
        base_score -= 6
        reasons.append("ATR yüksek; hedef yolu oynak")
    if historical_volatility is not None and historical_volatility > 80:
        base_score -= 5
    if free_float_ratio is not None and free_float_ratio < 10:
        base_score -= 8
        reasons.append("Fiilî dolaşım oranı düşük")

    abnormal = abnormal_price_volume or consecutive_limit_ups >= 3
    manipulation_indicator = (
        "Anormal fiyat/hacim davranışı gözleniyor; bu bir manipülasyon iddiası değildir"
        if abnormal else "Kesin bir anormal davranış göstergesi yok"
    )
    if abnormal:
        base_score -= 15
    score = round(max(0.0, min(100.0, base_score)), 1)
    speculation = "Yüksek" if (multiple or 0) >= 3 or abnormal else ("Orta" if (multiple or 0) >= 1.75 else "Düşük-Orta")
    return TargetRealismResult(
        current, target, change, multiple, current_cap, target_cap, cap_increase,
        technical, fundamental, liquidity_risk, valuation_risk, speculation,
        manipulation_indicator, score, reasons,
    )
