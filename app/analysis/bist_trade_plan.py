from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.analysis.indicator_engine import atr, ema, macd, rsi
from app.analysis.support_resistance_engine import compute_support_resistance


@dataclass(frozen=True)
class DirectionPlan:
    direction: str
    score: int
    status: str
    entry_low: float
    entry_high: float
    trigger: float
    stop_aggressive: float
    stop_standard: float
    stop_conservative: float
    targets: tuple[float, float, float, float, float]
    risk_multiples: tuple[float, float, float, float, float]
    invalidation: str


@dataclass(frozen=True)
class BistTradePlan:
    symbol: str
    current_price: float
    atr: float
    atr_percent: float
    trend: str
    rsi: float
    support_levels: tuple[float, ...]
    resistance_levels: tuple[float, ...]
    long: DirectionPlan
    short: DirectionPlan
    warnings: tuple[str, ...]


def _ordered_unique(values, *, reverse: bool = False) -> list[float]:
    clean = {round(float(value), 2) for value in values if value is not None and float(value) > 0}
    return sorted(clean, reverse=reverse)


def _five_targets(entry: float, stop: float, structural: list[float], direction: str) -> tuple[float, ...]:
    risk = abs(entry - stop)
    if risk <= 0:
        raise ValueError("Stop mesafesi pozitif olmali.")
    sign = 1 if direction == "LONG" else -1
    candidates = [level for level in structural if (level - entry) * sign > risk * 0.45]
    candidates += [entry + sign * risk * multiple for multiple in (1.0, 1.5, 2.0, 3.0, 4.0, 5.0)]
    candidates = sorted({round(value, 2) for value in candidates if value > 0}, reverse=sign < 0)
    selected: list[float] = []
    for value in candidates:
        if not selected or abs(value - selected[-1]) >= risk * 0.35:
            selected.append(value)
        if len(selected) == 5:
            break
    while len(selected) < 5:
        selected.append(round(entry + sign * risk * (len(selected) + 1), 2))
    return tuple(selected[:5])


def _direction_score(direction: str, close: float, ema20: float, ema50: float, rsi_value: float, macd_hist: float) -> int:
    bullish = direction == "LONG"
    score = 20
    score += 25 if ((close > ema20) == bullish) else 0
    score += 20 if ((ema20 > ema50) == bullish) else 0
    score += 20 if ((macd_hist > 0) == bullish) else 0
    score += 15 if ((rsi_value >= 50) == bullish) else 0
    return min(score, 100)


def _status(score: int) -> str:
    if score >= 80:
        return "GUCLU"
    if score >= 60:
        return "TEYIT BEKLE"
    return "ZAYIF / PAS GEC"


def build_bist_trade_plan(df: pd.DataFrame, symbol: str) -> BistTradePlan:
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if not required.issubset(df.columns) or len(df) < 60:
        raise ValueError("Islem plani icin en az 60 OHLCV mumu gerekir.")
    data = df.sort_values("timestamp").reset_index(drop=True).copy()
    close_series = data["close"].astype(float)
    close = float(close_series.iloc[-1])
    atr_value = float(atr(data, 14).iloc[-1])
    if not pd.notna(atr_value) or atr_value <= 0:
        raise ValueError("Gecerli ATR hesaplanamadi.")
    ema20 = float(ema(close_series, 20).iloc[-1])
    ema50 = float(ema(close_series, 50).iloc[-1])
    rsi_value = float(rsi(close_series, 14).iloc[-1])
    _, _, histogram = macd(close_series)
    macd_hist = float(histogram.iloc[-1])
    sr = compute_support_resistance(data, close, ema20, ema50, atr_value)
    supports = _ordered_unique((sr.support_1, sr.support_2, sr.main_support), reverse=True)
    resistances = _ordered_unique((sr.resistance_1, sr.resistance_2, sr.main_resistance))

    long_entry_low = round(max((supports[0] if supports else close - atr_value * 0.35), close - atr_value * 0.65), 2)
    long_entry_high = round(close + atr_value * 0.15, 2)
    long_trigger = round(resistances[0] if resistances and resistances[0] <= close + atr_value else close + atr_value * 0.25, 2)
    long_stops = (
        round(long_entry_low - atr_value * 0.55, 2),
        round(long_entry_low - atr_value * 1.00, 2),
        round(long_entry_low - atr_value * 1.50, 2),
    )
    long_targets = _five_targets(long_entry_high, long_stops[1], resistances, "LONG")

    short_entry_low = round(close - atr_value * 0.15, 2)
    short_entry_high = round(min((resistances[0] if resistances else close + atr_value * 0.65), close + atr_value * 0.65), 2)
    short_trigger = round(supports[0] if supports and supports[0] >= close - atr_value else close - atr_value * 0.25, 2)
    short_stops = (
        round(short_entry_high + atr_value * 0.55, 2),
        round(short_entry_high + atr_value * 1.00, 2),
        round(short_entry_high + atr_value * 1.50, 2),
    )
    short_targets = _five_targets(short_entry_low, short_stops[1], supports, "SHORT")

    long_score = _direction_score("LONG", close, ema20, ema50, rsi_value, macd_hist)
    short_score = _direction_score("SHORT", close, ema20, ema50, rsi_value, macd_hist)
    long_r = tuple(round((target - long_entry_high) / (long_entry_high - long_stops[1]), 2) for target in long_targets)
    short_r = tuple(round((short_entry_low - target) / (short_stops[1] - short_entry_low), 2) for target in short_targets)

    return BistTradePlan(
        symbol=symbol.upper().removesuffix(".IS"), current_price=round(close, 2), atr=round(atr_value, 2),
        atr_percent=round(atr_value / close * 100, 2),
        trend="YUKSELIS" if ema20 > ema50 else "DUSUS" if ema20 < ema50 else "YATAY",
        rsi=round(rsi_value, 1), support_levels=tuple(supports), resistance_levels=tuple(resistances),
        long=DirectionPlan("LONG", long_score, _status(long_score), long_entry_low, long_entry_high, long_trigger,
                           *long_stops, long_targets, long_r,
                           f"{long_stops[1]:.2f} TL altinda kapanis veya hacimli destek kirilimi"),
        short=DirectionPlan("SHORT", short_score, _status(short_score), short_entry_low, short_entry_high, short_trigger,
                            *short_stops, short_targets, short_r,
                            f"{short_stops[1]:.2f} TL ustunde kapanis veya hacimli direnc kirilimi"),
        warnings=(
            "Short senaryosu spot BIST'te her hissede uygulanamaz; aciga satis listesi, odunc pay ve VIOP uygunlugu kontrol edilmelidir.",
            "Seviyeler kapanmis mumlara dayanir; emir vermeden once guncel fiyat, kademe ve likidite yeniden kontrol edilmelidir.",
        ),
    )


def format_bist_trade_plan(plan: BistTradePlan) -> str:
    def side_block(icon: str, side: DirectionPlan) -> str:
        targets = "\n".join(
            f"  {'ğŸŸ©' if side.direction == 'LONG' else 'ğŸŸ¥'} TP{i}: {price:.2f} TL  â€¢  {rr:.2f}R"
            for i, (price, rr) in enumerate(zip(side.targets, side.risk_multiples), 1)
        )
        return (
            f"{icon} {side.direction} SENARYOSU  â€¢  {side.score}/100  â€¢  {side.status}\n"
            f"ğŸ¯ GiriÅŸ bÃ¶lgesi: {side.entry_low:.2f} â€“ {side.entry_high:.2f} TL\n"
            f"âš¡ Tetik: {side.trigger:.2f} TL kapanÄ±ÅŸ/hacim teyidi\n"
            f"ğŸ›¡ï¸ SL agresif: {side.stop_aggressive:.2f} TL\n"
            f"ğŸ›‘ SL standart: {side.stop_standard:.2f} TL\n"
            f"ğŸ° SL korumacÄ±: {side.stop_conservative:.2f} TL\n"
            f"{targets}\n"
            f"âŒ GeÃ§ersizlik: {side.invalidation}"
        )

    supports = " / ".join(f"{value:.2f}" for value in plan.support_levels) or "-"
    resistances = " / ".join(f"{value:.2f}" for value in plan.resistance_levels) or "-"
    return (
        f"ğŸ“ŠğŸ”¥ MERGEN BIST Ä°ÅLEM HARÄ°TASI â€” {plan.symbol}\n\n"
        f"ğŸ’° Son fiyat: {plan.current_price:.2f} TL\n"
        f"ğŸ§­ Ana trend: {plan.trend}  â€¢  RSI: {plan.rsi:.1f}\n"
        f"ğŸŒªï¸ ATR: {plan.atr:.2f} TL (%{plan.atr_percent:.2f})\n"
        f"ğŸŸ¢ Destekler: {supports}\nğŸ”´ DirenÃ§ler: {resistances}\n\n"
        f"{side_block('ğŸš€', plan.long)}\n\n"
        f"{side_block('ğŸ»', plan.short)}\n\n"
        "âš ï¸ BIST NOTU\n" + "\n".join(f"â€¢ {warning}" for warning in plan.warnings) +
        "\n\nğŸ§  Plan koÅŸulludur; tetik gelmeden iÅŸlem aktif sayÄ±lmaz. YatÄ±rÄ±m tavsiyesi deÄŸildir."
    )

