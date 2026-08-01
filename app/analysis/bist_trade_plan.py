from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from app.analysis.indicator_engine import compute_technical_snapshot
from app.analysis.quality_zone_engine import QualityZoneScenario, select_closest_quality_zone
from app.analysis.smart_money_engine import SmartMoneyResult, detect_smart_money
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
    entry_method: str
    score_breakdown: tuple[str, ...]
    confirmations: tuple[str, ...]
    risks: tuple[str, ...]


@dataclass(frozen=True)
class BistTradePlan:
    symbol: str
    current_price: float
    atr: float
    atr_percent: float
    trend: str
    rsi: float
    adx: float
    relative_volume: float
    support_levels: tuple[float, ...]
    resistance_levels: tuple[float, ...]
    long: DirectionPlan
    short: DirectionPlan
    preferred_direction: str | None
    decision: str
    data_timestamp: datetime | pd.Timestamp
    warnings: tuple[str, ...]
    quality_zone: QualityZoneScenario | None = None


def _ordered_unique(values, *, reverse: bool = False) -> list[float]:
    clean = {round(float(value), 2) for value in values if value is not None and float(value) > 0}
    return sorted(clean, reverse=reverse)


def _five_targets(entry: float, stop: float, structural: list[float], direction: str) -> tuple[float, ...]:
    """Build five monotonic targets from structure first, then honest R multiples."""
    risk = abs(entry - stop)
    if risk <= 0:
        raise ValueError("Stop mesafesi pozitif olmalı.")
    sign = 1 if direction == "LONG" else -1
    candidates = [level for level in structural if (level - entry) * sign >= risk * 0.75]
    candidates += [entry + sign * risk * multiple for multiple in (1.0, 1.5, 2.0, 3.0, 4.0, 5.0)]
    candidates = sorted({round(value, 2) for value in candidates if value > 0}, reverse=sign < 0)
    selected: list[float] = []
    for value in candidates:
        if not selected or abs(value - selected[-1]) >= risk * 0.30:
            selected.append(value)
        if len(selected) == 5:
            break
    while len(selected) < 5:
        selected.append(round(entry + sign * risk * (len(selected) + 1), 2))
    return tuple(selected[:5])


def _best_anchor(
    direction: str,
    close: float,
    atr_value: float,
    candidates: list[tuple[float, str, float]],
) -> tuple[float, tuple[str, ...]]:
    """Choose the nearest *confluent* level instead of an arbitrary ATR offset."""
    if direction == "LONG":
        eligible = [item for item in candidates if item[0] <= close + atr_value * 0.12]
        fallback = close - atr_value * 0.35
    else:
        eligible = [item for item in candidates if item[0] >= close - atr_value * 0.12]
        fallback = close + atr_value * 0.35
    if not eligible:
        return fallback, ("Yakın yapısal seviye bulunamadı; ATR tabanlı izleme bölgesi",)

    ranked: list[tuple[float, float, str]] = []
    for price, label, weight in eligible:
        confluence = sum(
            other_weight
            for other_price, _other_label, other_weight in eligible
            if abs(other_price - price) <= atr_value * 0.45
        )
        distance_penalty = abs(close - price) / atr_value * 0.65
        ranked.append((confluence + weight - distance_penalty, price, label))
    ranked.sort(reverse=True)
    _rank, anchor, _label = ranked[0]
    evidence = tuple(
        dict.fromkeys(
            label
            for price, label, _weight in eligible
            if abs(price - anchor) <= atr_value * 0.45
        )
    )
    return anchor, evidence


def _score_direction(
    direction: str,
    snapshot,
    smart: SmartMoneyResult,
    *,
    support_reliable: bool,
    resistance_reliable: bool,
    entry_reference: float,
    stop: float,
    first_target: float,
) -> tuple[int, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Transparent evidence score. It is a setup-quality score, not a probability."""
    bullish = direction == "LONG"
    points: list[tuple[str, int, int, str]] = []
    confirmations: list[str] = []
    risks: list[str] = []

    close = snapshot.close
    trend_points = 0
    trend_evidence: list[str] = []
    if (close > snapshot.ema20) == bullish:
        trend_points += 8
        trend_evidence.append("fiyat EMA20 ile uyumlu")
    if (snapshot.ema20 > snapshot.ema50) == bullish:
        trend_points += 8
        trend_evidence.append("EMA20/50 sıralaması uyumlu")
    if snapshot.ema200 is not None:
        if (snapshot.ema50 > snapshot.ema200) == bullish:
            trend_points += 9
            trend_evidence.append("EMA50/200 ana trendi uyumlu")
    else:
        risks.append("EMA200 için yeterli geçmiş yok")
    points.append(("Trend", trend_points, 25, ", ".join(trend_evidence) or "trend uyumu yok"))

    momentum_points = 0
    healthy_rsi = 48 <= snapshot.rsi <= 68 if bullish else 32 <= snapshot.rsi <= 52
    if healthy_rsi:
        momentum_points += 8
        confirmations.append(f"RSI sağlıklı bölgede ({snapshot.rsi:.1f})")
    elif (snapshot.rsi > 68 and bullish) or (snapshot.rsi < 32 and not bullish):
        momentum_points += 3
        risks.append(f"RSI uzamış bölgede ({snapshot.rsi:.1f})")
    if (snapshot.macd_histogram > 0) == bullish:
        momentum_points += 7
        confirmations.append("MACD yönü destekliyor")
    if snapshot.adx >= 25:
        momentum_points += 5
        confirmations.append(f"Trend gücü belirgin (ADX {snapshot.adx:.1f})")
    elif snapshot.adx < 18:
        risks.append(f"Trend gücü düşük (ADX {snapshot.adx:.1f})")
    points.append(("Momentum", momentum_points, 20, f"RSI {snapshot.rsi:.1f}, ADX {snapshot.adx:.1f}"))

    structure_points = 0
    level_reliable = support_reliable if bullish else resistance_reliable
    if level_reliable:
        structure_points += 6
        confirmations.append("Destek/direnç birden fazla teknik kaynaktan doğrulandı")
    direction_name = "bullish" if bullish else "bearish"
    nearby_zone = any(
        zone.direction == direction_name
        and min(abs(entry_reference - zone.low), abs(entry_reference - zone.high)) <= snapshot.atr * 0.75
        for zone in (*smart.fvg, *smart.order_blocks)
    )
    if nearby_zone:
        structure_points += 7
        confirmations.append("Giriş bölgesi FVG/Order Block ile çakışıyor")
    recent_structure = next((event for event in reversed(smart.structure) if event.direction == direction_name), None)
    if recent_structure is not None:
        structure_points += 7
        confirmations.append(f"Son yapı olayı {recent_structure.kind} yönle uyumlu")
    else:
        risks.append("Yakın tarihli BOS/MSS teyidi yok")
    points.append(("Yapı", structure_points, 20, "S/R + FVG/OB + BOS/MSS"))

    volume_points = 0
    if snapshot.relative_volume >= 1.5:
        volume_points += 10
        confirmations.append(f"Hacim güçlü ({snapshot.relative_volume:.2f}x)")
    elif snapshot.relative_volume >= 1.1:
        volume_points += 7
    elif snapshot.relative_volume >= 0.8:
        volume_points += 4
    else:
        risks.append(f"Göreli hacim zayıf ({snapshot.relative_volume:.2f}x)")
    if snapshot.obv_trend_up == bullish:
        volume_points += 5
    points.append(("Hacim", volume_points, 15, f"göreli hacim {snapshot.relative_volume:.2f}x"))

    risk_amount = abs(entry_reference - stop)
    reward = (first_target - entry_reference) if bullish else (entry_reference - first_target)
    rr = reward / risk_amount if risk_amount > 0 else 0.0
    risk_points = 12 if rr >= 1.5 else 8 if rr >= 1.0 else 4 if rr >= 0.75 else 0
    atr_percent = snapshot.atr / close * 100 if close else 0.0
    if 0.8 <= atr_percent <= 5.0:
        risk_points += 5
    elif atr_percent <= 8.0:
        risk_points += 3
    else:
        risks.append(f"Volatilite yüksek (ATR %{atr_percent:.2f})")
    if abs(close - entry_reference) <= snapshot.atr:
        risk_points += 3
    points.append(("Risk/Getiri", risk_points, 20, f"TP1 {rr:.2f}R, ATR %{atr_percent:.2f}"))

    total = max(0, min(100, sum(item[1] for item in points)))
    breakdown = tuple(f"{name}: {score}/{maximum} • {evidence}" for name, score, maximum, evidence in points)
    return total, breakdown, tuple(dict.fromkeys(confirmations)), tuple(dict.fromkeys(risks))


def _status(score: int) -> str:
    if score >= 78:
        return "A-KALİTE • TETİK BEKLE"
    if score >= 68:
        return "B-KALİTE • TETİK BEKLE"
    if score >= 58:
        return "C-KALİTE • SADECE İZLE"
    return "ZAYIF • PAS GEÇ"


def _timeframe_adjustment(result, direction: str) -> tuple[int, str]:
    if result is None:
        return 0, "çoklu zaman verisi alınamadı"
    weights = {"4h": 4.0, "1h": 3.0, "15m": 2.0, "5m": 1.0}
    aligned = opposed = total = 0.0
    readable: list[str] = []
    for timeframe, weight in weights.items():
        snapshot = getattr(result, "snapshots", {}).get(timeframe)
        if snapshot is None or not getattr(snapshot, "available", False):
            continue
        trend = str(getattr(snapshot, "trend_class", "")).casefold()
        sign = 1 if "yükseliş" in trend else -1 if "düşüş" in trend else 0
        wanted = 1 if direction == "LONG" else -1
        total += weight
        if sign == wanted:
            aligned += weight
        elif sign == -wanted:
            opposed += weight
        readable.append(f"{timeframe} {'↑' if sign > 0 else '↓' if sign < 0 else '→'}")
    if total == 0:
        return 0, "çoklu zaman verisi yetersiz"
    adjustment = round((aligned - opposed) / total * 8)
    return max(-8, min(8, adjustment)), ", ".join(readable)


def build_bist_trade_plan(df: pd.DataFrame, symbol: str, multi_timeframe_result=None) -> BistTradePlan:
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if not required.issubset(df.columns):
        raise ValueError("İşlem planı için timestamp/open/high/low/close/volume gerekir.")

    data = df.sort_values("timestamp").reset_index(drop=True).copy()
    warnings: list[str] = []
    if "is_complete" in data.columns:
        complete = data["is_complete"].map(
            lambda value: str(value).strip().casefold() in {"true", "1"}
        )
        removed = int((~complete).sum())
        data = data.loc[complete].reset_index(drop=True)
        if removed:
            warnings.append(f"{removed} tamamlanmamış mum puanlamadan çıkarıldı.")
    else:
        warnings.append("Sağlayıcı mum tamamlanma bayrağı vermedi; son mum ayrıca kontrol edilmelidir.")
    if len(data) < 60:
        raise ValueError("İşlem planı için en az 60 tamamlanmış OHLCV mumu gerekir.")

    snapshot = compute_technical_snapshot(data, symbol, "1d")
    close = snapshot.close
    atr_value = snapshot.atr
    if not pd.notna(atr_value) or atr_value <= 0:
        raise ValueError("Geçerli ATR hesaplanamadı.")
    sr = compute_support_resistance(
        data,
        close,
        snapshot.ema20,
        snapshot.ema50,
        atr_value,
        snapshot.ema100,
        snapshot.ema200,
    )
    supports = _ordered_unique((sr.support_1, sr.support_2, sr.main_support), reverse=True)
    resistances = _ordered_unique((sr.resistance_1, sr.resistance_2, sr.main_resistance))
    smart = detect_smart_money(data)
    quality_zone = select_closest_quality_zone(
        close,
        atr_value,
        smart,
        support_levels=supports,
        resistance_levels=resistances,
    )

    long_candidates = [(value, "yapısal destek", 3.0) for value in supports]
    short_candidates = [(value, "yapısal direnç", 3.0) for value in resistances]
    for value, label in ((snapshot.ema20, "EMA20"), (snapshot.ema50, "EMA50"), (snapshot.ema200, "EMA200")):
        if value is None:
            continue
        (long_candidates if value <= close else short_candidates).append((float(value), label, 2.0))
    for zone in (*smart.fvg, *smart.order_blocks):
        middle = (zone.low + zone.high) / 2
        label = f"{zone.kind} ({'yükseliş' if zone.direction == 'bullish' else 'düşüş'})"
        target = long_candidates if zone.direction == "bullish" else short_candidates
        target.append((middle, label, 2.8 if zone.kind == "OB" else 2.2))

    long_anchor, long_evidence = _best_anchor("LONG", close, atr_value, long_candidates)
    short_anchor, short_evidence = _best_anchor("SHORT", close, atr_value, short_candidates)
    long_entry_low = round(max(0.01, long_anchor - atr_value * 0.22), 2)
    long_entry_high = round(min(close, long_anchor + atr_value * 0.22), 2)
    if long_entry_high <= long_entry_low:
        long_entry_high = round(long_entry_low + atr_value * 0.18, 2)
    short_entry_low = round(max(close, short_anchor - atr_value * 0.22), 2)
    short_entry_high = round(short_anchor + atr_value * 0.22, 2)
    if short_entry_high <= short_entry_low:
        short_entry_high = round(short_entry_low + atr_value * 0.18, 2)

    long_trigger = round(long_entry_high + atr_value * 0.08, 2)
    short_trigger = round(short_entry_low - atr_value * 0.08, 2)
    long_stops = (
        round(long_entry_low - atr_value * 0.35, 2),
        round(long_entry_low - atr_value * 0.75, 2),
        round(long_entry_low - atr_value * 1.25, 2),
    )
    short_stops = (
        round(short_entry_high + atr_value * 0.35, 2),
        round(short_entry_high + atr_value * 0.75, 2),
        round(short_entry_high + atr_value * 1.25, 2),
    )
    long_targets = _five_targets(long_entry_high, long_stops[1], resistances, "LONG")
    short_targets = _five_targets(short_entry_low, short_stops[1], supports, "SHORT")
    long_r = tuple(round((target - long_entry_high) / (long_entry_high - long_stops[1]), 2) for target in long_targets)
    short_r = tuple(round((short_entry_low - target) / (short_stops[1] - short_entry_low), 2) for target in short_targets)

    long_score, long_breakdown, long_confirmations, long_risks = _score_direction(
        "LONG", snapshot, smart,
        support_reliable=sr.support_reliable,
        resistance_reliable=sr.resistance_reliable,
        entry_reference=long_entry_high,
        stop=long_stops[1],
        first_target=long_targets[0],
    )
    short_score, short_breakdown, short_confirmations, short_risks = _score_direction(
        "SHORT", snapshot, smart,
        support_reliable=sr.support_reliable,
        resistance_reliable=sr.resistance_reliable,
        entry_reference=short_entry_low,
        stop=short_stops[1],
        first_target=short_targets[0],
    )

    long_mtf, mtf_evidence = _timeframe_adjustment(multi_timeframe_result, "LONG")
    short_mtf, _ = _timeframe_adjustment(multi_timeframe_result, "SHORT")
    long_score = max(0, min(100, long_score + long_mtf))
    short_score = max(0, min(100, short_score + short_mtf))
    long_breakdown += (f"Çoklu zaman ayarı: {long_mtf:+d} • {mtf_evidence}",)
    short_breakdown += (f"Çoklu zaman ayarı: {short_mtf:+d} • {mtf_evidence}",)
    if long_mtf >= 3:
        long_confirmations += ("5dk/15dk/1s/4s yönleri LONG tarafını destekliyor",)
    elif long_mtf <= -3:
        long_risks += ("Alt zaman dilimleri LONG yönüyle çelişiyor",)
    if short_mtf >= 3:
        short_confirmations += ("5dk/15dk/1s/4s yönleri SHORT tarafını destekliyor",)
    elif short_mtf <= -3:
        short_risks += ("Alt zaman dilimleri SHORT yönüyle çelişiyor",)

    best_direction = "LONG" if long_score >= short_score else "SHORT"
    best_score, other_score = max(long_score, short_score), min(long_score, short_score)
    preferred = best_direction if best_score >= 68 and best_score - other_score >= 8 else None
    decision = (
        f"{preferred} yalnız giriş bölgesi görülüp tetik kapanışı ve hacim onayı gelirse izlenebilir."
        if preferred
        else "BEKLE • Yeterli kalite ve yön farkı oluşmadı; işlem zorlanmamalı."
    )

    return BistTradePlan(
        symbol=symbol.upper().removesuffix(".IS"),
        current_price=round(close, 2),
        atr=round(atr_value, 2),
        atr_percent=round(atr_value / close * 100, 2),
        trend="YÜKSELİŞ" if snapshot.ema20 > snapshot.ema50 else "DÜŞÜŞ" if snapshot.ema20 < snapshot.ema50 else "YATAY",
        rsi=round(snapshot.rsi, 1),
        adx=round(snapshot.adx, 1),
        relative_volume=round(snapshot.relative_volume, 2),
        support_levels=tuple(supports),
        resistance_levels=tuple(resistances),
        long=DirectionPlan(
            "LONG", long_score, _status(long_score), long_entry_low, long_entry_high, long_trigger,
            *long_stops, long_targets, long_r,
            f"{long_stops[1]:.2f} TL altında tamamlanmış mum kapanışı",
            "Destek/EMA/FVG-OB bölgesine geri çekilme, ardından bölge üstünde kapanış ve hacim teyidi",
            long_breakdown, long_confirmations + long_evidence, long_risks,
        ),
        short=DirectionPlan(
            "SHORT", short_score, _status(short_score), short_entry_low, short_entry_high, short_trigger,
            *short_stops, short_targets, short_r,
            f"{short_stops[1]:.2f} TL üstünde tamamlanmış mum kapanışı",
            "Direnç/EMA/FVG-OB bölgesine tepki, ardından bölge altında kapanış ve hacim teyidi",
            short_breakdown, short_confirmations + short_evidence, short_risks,
        ),
        preferred_direction=preferred,
        decision=decision,
        data_timestamp=data["timestamp"].iloc[-1],
        warnings=tuple(warnings) + (
            "Short senaryosu spot BIST'te her hissede uygulanamaz; açığa satış/ödünç/VİOP uygunluğu kontrol edilmelidir.",
            "Puan olasılık veya kesin AL/SAT kararı değildir; açıklanabilir teknik kurulum kalitesidir.",
        ),
        quality_zone=quality_zone,
    )
