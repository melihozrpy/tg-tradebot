from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from app.analysis.indicator_engine import (
    InsufficientDataError,
    adx,
    atr,
    ema,
    macd,
    relative_volume,
    rsi,
)
from app.data.base_provider import BaseMarketDataProvider, DataUnavailableError

# Eski çağrılar için beşli varsayılan korunur. Aşama 5e entegrasyonu bu
# genişletilmiş kümeyi açıkça kullanır.
TIMEFRAMES = ("1wk", "1d", "1h", "15m", "5m")
STAGE5E_TIMEFRAMES = ("1wk", "1d", "4h", "1h", "15m", "5m")

TIMEFRAME_ROLES = {
    "1wk": "Ana buyuk trend",
    "1d": "Ana islem yonu",
    "4h": "Orta-uzun vadeli yapi",
    "1h": "Orta vadeli yapi",
    "15m": "Giris zamanlamasi",
    "5m": "Hassas giris ve on alarm",
}

TREND_STRONG_UP = "Güçlü yükseliş"
TREND_VERY_STRONG_UP = "Çok güçlü yükseliş"
TREND_UP = "Yükseliş"
TREND_SIDEWAYS = "Yatay"
TREND_NEUTRAL = "Nötr"
TREND_DOWN = "Düşüş"
TREND_STRONG_DOWN = "Güçlü düşüş"
TREND_VERY_STRONG_DOWN = "Çok güçlü düşüş"
TREND_INSUFFICIENT = "Veri yetersiz"

# Bir zaman diliminde tam analiz icin minimum bar sayisi (kisa zaman dilimlerinde
# daha az bar ile de anlamli EMA/RSI hesaplanabilir; gunluk/haftalikta daha fazla
# gecmis aranir).
MIN_BARS_FOR_TIMEFRAME = {
    "5m": 60,
    "15m": 60,
    "1h": 60,
    "4h": 50,
    "1d": 60,
    "1wk": 30,
}


@dataclass
class TimeframeSnapshot:
    timeframe: str
    available: bool
    role: str
    trend_class: str = TREND_INSUFFICIENT
    momentum_direction: str = "notr"
    close: Optional[float] = None
    ema20: Optional[float] = None
    ema50: Optional[float] = None
    ema100: Optional[float] = None
    ema200: Optional[float] = None
    rsi: Optional[float] = None
    macd_histogram: Optional[float] = None
    adx: Optional[float] = None
    atr: Optional[float] = None
    relative_volume: Optional[float] = None
    higher_high: bool = False
    higher_low: bool = False
    lower_high: bool = False
    lower_low: bool = False
    last_breakout: Optional[str] = None
    support: Optional[float] = None
    resistance: Optional[float] = None
    last_bar_timestamp: Optional[datetime] = None
    data_timestamp_note: str = ""
    error: Optional[str] = None
    trend_strength: float = 0.0
    ema_structure: str = "Veri yetersiz"
    volume_status: str = "Veri yetersiz"
    support_distance_percent: Optional[float] = None
    resistance_distance_percent: Optional[float] = None
    last_candle_state: str = "Veri yetersiz"
    data_quality: str = "Veri yetersiz"


@dataclass
class MultiTimeframeResult:
    symbol: str
    snapshots: dict = field(default_factory=dict)  # timeframe -> TimeframeSnapshot
    confluence_score: float = 0.0
    primary_direction: str = TREND_INSUFFICIENT
    short_term_direction: str = TREND_INSUFFICIENT
    conflict: bool = False
    counter_trend_warning: bool = False
    scenario_note: str = ""
    large_timeframe_direction: str = TREND_INSUFFICIENT
    small_timeframe_direction: str = TREND_INSUFFICIENT
    short_term_reaction: bool = False
    trend_reversal: bool = False
    breakout_confirmed: bool = False
    counter_trend_risk: bool = False
    data_quality: str = "Veri yetersiz"
    data_timestamp: Optional[datetime] = None


def _classify_trend(
    close: float, ema20: float, ema50: float, adx_val: float, *, extended: bool = False
) -> str:
    if ema20 > ema50 and close > ema20:
        if extended and adx_val >= 40:
            return TREND_VERY_STRONG_UP
        return TREND_STRONG_UP if adx_val >= 25 else TREND_UP
    if ema20 < ema50 and close < ema20:
        if extended and adx_val >= 40:
            return TREND_VERY_STRONG_DOWN
        return TREND_STRONG_DOWN if adx_val >= 25 else TREND_DOWN
    return TREND_SIDEWAYS


def _detect_hh_hl_lh_ll(df: pd.DataFrame, lookback: int = 20) -> tuple[bool, bool, bool, bool]:
    """Basit swing-bazli higher-high/higher-low/lower-high/lower-low tespiti.

    Son `lookback` bari iki yariya bolup, ikinci yarinin tepe/dibini ilk
    yariyla kiyaslar. Look-ahead bias yoktur (yalnizca gecmis veri kullanilir).
    """
    if len(df) < lookback:
        return False, False, False, False
    window = df.tail(lookback)
    half = lookback // 2
    first_half, second_half = window.iloc[:half], window.iloc[half:]
    if first_half.empty or second_half.empty:
        return False, False, False, False

    higher_high = float(second_half["high"].max()) > float(first_half["high"].max())
    higher_low = float(second_half["low"].min()) > float(first_half["low"].min())
    lower_high = float(second_half["high"].max()) < float(first_half["high"].max())
    lower_low = float(second_half["low"].min()) < float(first_half["low"].min())
    return higher_high, higher_low, lower_high, lower_low


def _detect_breakout(df: pd.DataFrame, lookback: int = 20) -> Optional[str]:
    if len(df) < lookback + 2:
        return None
    prior = df.iloc[-(lookback + 1):-1]
    last_close = float(df.iloc[-1]["close"])
    prior_high = float(prior["high"].max())
    prior_low = float(prior["low"].min())
    if last_close > prior_high:
        return "direnc_kirildi"
    if last_close < prior_low:
        return "destek_kirildi"
    return None


def compute_timeframe_snapshot(
    df: Optional[pd.DataFrame], timeframe: str, error: Optional[str] = None, *, extended: bool = False
) -> TimeframeSnapshot:
    """Tek bir zaman dilimi icin teknik gorunumu hesaplar.

    Veri yoksa veya yetersizse UYDURMA YAPILMAZ; available=False ve
    trend_class='Veri yetersiz' doner.
    """
    role = TIMEFRAME_ROLES.get(timeframe, timeframe)
    min_bars = MIN_BARS_FOR_TIMEFRAME.get(timeframe, 60)

    if error is not None or df is None or df.empty or len(df) < min_bars:
        detail = error or f"Yetersiz veri: {0 if df is None else len(df)} < {min_bars}"
        return TimeframeSnapshot(timeframe=timeframe, available=False, role=role, error=detail)

    df = df.sort_values("timestamp").reset_index(drop=True)
    close = df["close"]

    ema20 = float(ema(close, 20).iloc[-1])
    ema50 = float(ema(close, 50).iloc[-1]) if len(df) >= 50 else ema20
    ema100_s = ema(close, 100)
    ema200_s = ema(close, 200)
    ema100 = float(ema100_s.iloc[-1]) if len(df) >= 100 else None
    ema200 = float(ema200_s.iloc[-1]) if len(df) >= 200 else None

    rsi_val = float(rsi(close, 14).iloc[-1])
    macd_line, macd_signal, macd_hist = macd(close)
    adx_val = float(adx(df, 14).iloc[-1])
    atr_val = float(atr(df, 14).iloc[-1])
    rel_vol_series = relative_volume(df, 20)
    rel_vol = float(rel_vol_series.iloc[-1]) if not pd.isna(rel_vol_series.iloc[-1]) else None

    trend_class = _classify_trend(float(close.iloc[-1]), ema20, ema50, adx_val, extended=extended)
    momentum_direction = "yukari" if float(macd_hist.iloc[-1]) > 0 else "asagi"
    ema_gap_percent = abs(ema20 - ema50) / max(abs(float(close.iloc[-1])), 0.000001) * 100
    trend_strength = min(100.0, max(0.0, adx_val * 1.55 + min(22.0, ema_gap_percent * 3.0)))
    if close.iloc[-1] > ema20 > ema50:
        ema_structure = "Pozitif sıralı"
    elif close.iloc[-1] < ema20 < ema50:
        ema_structure = "Negatif sıralı"
    else:
        ema_structure = "Karışık"

    higher_high, higher_low, lower_high, lower_low = _detect_hh_hl_lh_ll(df)
    breakout = _detect_breakout(df)

    last_20 = df.tail(20)
    support = float(last_20["low"].min()) if not last_20.empty else None
    resistance = float(last_20["high"].max()) if not last_20.empty else None
    last_close = float(close.iloc[-1])
    support_distance = ((last_close - support) / support * 100) if support and support > 0 else None
    resistance_distance = ((resistance - last_close) / last_close * 100) if resistance and last_close > 0 else None
    if rel_vol is None:
        volume_status = "Veri yetersiz"
    elif rel_vol >= 1.5:
        volume_status = "Yüksek"
    elif rel_vol <= 0.7:
        volume_status = "Düşük"
    else:
        volume_status = "Normal"
    candle = df.iloc[-1]
    candle_range = max(float(candle["high"]) - float(candle["low"]), 0.000001)
    candle_body = abs(float(candle["close"]) - float(candle["open"]))
    if candle_body / candle_range <= 0.1:
        last_candle_state = "Kararsız/doji"
    elif float(candle["close"]) > float(candle["open"]):
        last_candle_state = "Pozitif"
    else:
        last_candle_state = "Negatif"

    last_ts = df["timestamp"].iloc[-1]
    last_ts_py = last_ts.to_pydatetime() if hasattr(last_ts, "to_pydatetime") else last_ts

    return TimeframeSnapshot(
        timeframe=timeframe,
        available=True,
        role=role,
        trend_class=trend_class,
        momentum_direction=momentum_direction,
        close=round(float(close.iloc[-1]), 2),
        ema20=round(ema20, 2),
        ema50=round(ema50, 2),
        ema100=round(ema100, 2) if ema100 is not None else None,
        ema200=round(ema200, 2) if ema200 is not None else None,
        rsi=round(rsi_val, 1),
        macd_histogram=round(float(macd_hist.iloc[-1]), 4),
        adx=round(adx_val, 1),
        atr=round(atr_val, 4),
        relative_volume=round(rel_vol, 2) if rel_vol is not None else None,
        higher_high=higher_high,
        higher_low=higher_low,
        lower_high=lower_high,
        lower_low=lower_low,
        last_breakout=breakout,
        support=round(support, 2) if support is not None else None,
        resistance=round(resistance, 2) if resistance is not None else None,
        last_bar_timestamp=last_ts_py,
        data_timestamp_note="Tamamlanmış mum",
        trend_strength=round(trend_strength, 1),
        ema_structure=ema_structure,
        volume_status=volume_status,
        support_distance_percent=round(support_distance, 2) if support_distance is not None else None,
        resistance_distance_percent=round(resistance_distance, 2) if resistance_distance is not None else None,
        last_candle_state=last_candle_state,
        data_quality="Yeterli",
    )


_TREND_DIRECTION_SIGN = {
    TREND_VERY_STRONG_UP: 3,
    TREND_STRONG_UP: 2,
    TREND_UP: 1,
    TREND_SIDEWAYS: 0,
    TREND_NEUTRAL: 0,
    TREND_DOWN: -1,
    TREND_STRONG_DOWN: -2,
    TREND_VERY_STRONG_DOWN: -3,
    TREND_INSUFFICIENT: 0,
}

# Agirliklar: buyuk zaman dilimleri (haftalik/gunluk) uyum skorunda daha agir basar.
_TIMEFRAME_WEIGHTS = {"1wk": 30, "1d": 25, "4h": 20, "1h": 15, "15m": 7, "5m": 3}


def _compute_confluence_score(snapshots: dict, weights: Optional[dict[str, int]] = None) -> float:
    """0-100 arasi uyum skoru: zaman dilimleri ayni yonde ise skor yuksek,
    celisiyorsa dusuk olur. Veri olmayan zaman dilimi skora dahil edilmez
    (uydurulmaz), toplam agirlik yeniden normalize edilir.
    """
    available = [(tf, snap) for tf, snap in snapshots.items() if snap.available]
    if not available:
        return 0.0

    active_weights = weights or _TIMEFRAME_WEIGHTS
    total_weight = sum(active_weights.get(tf, 10) for tf, _ in available)
    if total_weight == 0:
        return 0.0

    signs = [_TREND_DIRECTION_SIGN.get(snap.trend_class, 0) for _, snap in available]
    weight_values = [active_weights.get(tf, 10) for tf, _ in available]

    weighted_sign_sum = sum(s * w for s, w in zip(signs, weight_values))
    max_possible = sum(3 * w for w in weight_values)

    if max_possible == 0:
        return 50.0

    # -1..+1 araligina normalize et, sonra 0-100'e tasi (0 = tam celiski, 100 = tam uyum)
    normalized = weighted_sign_sum / max_possible  # -1..1
    direction_score = abs(normalized) * 100
    dominant_sign = 1 if weighted_sign_sum > 0 else (-1 if weighted_sign_sum < 0 else 0)
    agreement_weight = sum(
        weight for sign, weight in zip(signs, weight_values)
        if dominant_sign and (1 if sign > 0 else (-1 if sign < 0 else 0)) == dominant_sign
    )
    agreement_score = (agreement_weight / total_weight * 100) if dominant_sign else 50.0
    strength_score = sum(
        float(getattr(snap, "trend_strength", 0.0)) * active_weights.get(tf, 10)
        for tf, snap in available
    ) / total_weight
    score = agreement_score * 0.55 + direction_score * 0.20 + strength_score * 0.25
    if any(sign > 0 for sign in signs) and any(sign < 0 for sign in signs):
        score *= 0.85
    return round(min(max(score, 0.0), 100.0), 1)


def resample_completed_4h(
    hourly_df: pd.DataFrame,
    *,
    now: Optional[datetime] = None,
    timezone_name: str = "Europe/Istanbul",
) -> pd.DataFrame:
    """Tamamlanmış 1 saatlik BIST mumlarından deterministik 4 saat üretir."""
    columns = ["timestamp", "open", "high", "low", "close", "volume"]
    if hourly_df is None or hourly_df.empty:
        return pd.DataFrame(columns=columns)
    from app.analysis.data_quality import DataQualityEngine

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    completed = DataQualityEngine().completed_candles(hourly_df, "1h", now=now)
    if completed is None or completed.empty:
        return pd.DataFrame(columns=columns)
    local_tz = ZoneInfo(timezone_name)
    working = completed[columns].copy().sort_values("timestamp")
    working["_local"] = pd.to_datetime(working["timestamp"], utc=True).dt.tz_convert(local_tz)
    # BIST sürekli işlem saatleri: 10:00-18:00. İki deterministik blok:
    # [10:00,14:00) ve [14:00,18:00).
    working = working[(working["_local"].dt.hour >= 10) & (working["_local"].dt.hour < 18)].copy()
    if working.empty:
        return pd.DataFrame(columns=columns)
    day_start = working["_local"].dt.normalize()
    block_offset = ((working["_local"].dt.hour - 10) // 4) * 4 + 10
    working["_bucket"] = day_start + pd.to_timedelta(block_offset, unit="h")
    now_local = pd.Timestamp(now).tz_convert(local_tz)
    rows: list[dict] = []
    for bucket, group in working.groupby("_bucket", sort=True):
        group = group.sort_values("_local")
        if len(group) < 4 or bucket + pd.Timedelta(hours=4) > now_local:
            continue
        rows.append(
            {
                "timestamp": bucket.tz_convert("UTC"),
                "open": float(group.iloc[0]["open"]),
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "close": float(group.iloc[-1]["close"]),
                "volume": float(group["volume"].sum()),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def _aggregate_direction(snapshots: dict, members: tuple[str, ...], weights: dict[str, int]) -> tuple[int, float]:
    weighted = 0.0
    total = 0.0
    strength_total = 0.0
    for tf in members:
        snap = snapshots.get(tf)
        if snap is None or not snap.available:
            continue
        weight = max(0, weights.get(tf, 0))
        sign = _TREND_DIRECTION_SIGN.get(snap.trend_class, 0)
        weighted += (1 if sign > 0 else (-1 if sign < 0 else 0)) * weight
        strength_total += snap.trend_strength * weight
        total += weight
    if total <= 0:
        return 0, 0.0
    return (1 if weighted > 0 else (-1 if weighted < 0 else 0)), strength_total / total


def _direction_label(sign: int, strength: float) -> str:
    if sign == 0:
        return TREND_NEUTRAL if strength else TREND_INSUFFICIENT
    if sign > 0:
        if strength >= 82:
            return TREND_VERY_STRONG_UP
        return TREND_STRONG_UP if strength >= 60 else TREND_UP
    if strength >= 82:
        return TREND_VERY_STRONG_DOWN
    return TREND_STRONG_DOWN if strength >= 60 else TREND_DOWN


def analyze_multi_timeframe(
    provider: BaseMarketDataProvider,
    symbol: str,
    timeframes: tuple = TIMEFRAMES,
    *,
    weights: Optional[dict[str, int]] = None,
    now: Optional[datetime] = None,
    timezone_name: str = "Europe/Istanbul",
) -> MultiTimeframeResult:
    """Ağırlıklı çoklu zaman analizi; her zaman dilimi hata yalıtımlıdır."""
    from app.analysis.data_quality import DataQualityEngine

    snapshots: dict[str, TimeframeSnapshot] = {}
    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    active_weights = weights or _TIMEFRAME_WEIGHTS
    fetched: dict[str, pd.DataFrame] = {}
    ranges = {"1wk": 365 * 5, "1d": 700, "4h": 180, "1h": 120, "15m": 20, "5m": 8}

    for tf in timeframes:
        try:
            if tf == "4h":
                hourly = fetched.get("1h")
                if hourly is None:
                    hourly = provider.get_ohlcv(symbol, "1h", end - timedelta(days=ranges["4h"]), end)
                    fetched["1h"] = hourly
                df = resample_completed_4h(hourly, now=end, timezone_name=timezone_name)
            else:
                df = fetched.get(tf)
                if df is None:
                    df = provider.get_ohlcv(symbol, tf, end - timedelta(days=ranges.get(tf, 500)), end)
                    fetched[tf] = df
                df = DataQualityEngine().completed_candles(df, tf, now=end)
            snapshots[tf] = compute_timeframe_snapshot(
                df, tf, extended=("4h" in timeframes or len(timeframes) >= 6)
            )
        except (DataUnavailableError, InsufficientDataError) as exc:
            snapshots[tf] = compute_timeframe_snapshot(None, tf, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - bir TF diğerlerini durdurmaz
            snapshots[tf] = compute_timeframe_snapshot(None, tf, error=f"Veri işlenemedi: {exc}")

    confluence_score = _compute_confluence_score(snapshots, active_weights)
    big_sign, big_strength = _aggregate_direction(snapshots, ("1wk", "1d", "4h"), active_weights)
    small_sign, small_strength = _aggregate_direction(snapshots, ("15m", "5m"), active_weights)
    primary_direction = _direction_label(big_sign, big_strength)
    short_term_direction = _direction_label(small_sign, small_strength)

    nonzero_signs = {
        1 if _TREND_DIRECTION_SIGN.get(s.trend_class, 0) > 0 else -1
        for s in snapshots.values() if s.available and _TREND_DIRECTION_SIGN.get(s.trend_class, 0) != 0
    }
    conflict = len(nonzero_signs) > 1
    breakout_confirmed = any(
        s.available and s.last_breakout and (s.relative_volume or 0) >= 1.2
        for s in snapshots.values()
    )
    short_term_reaction = big_sign != 0 and small_sign == -big_sign
    four_h = snapshots.get("4h")
    four_h_sign = 0 if not four_h or not four_h.available else (
        1 if _TREND_DIRECTION_SIGN.get(four_h.trend_class, 0) > 0 else -1
    )
    trend_reversal = bool(short_term_reaction and four_h_sign == small_sign and breakout_confirmed)
    counter_trend_warning = bool(short_term_reaction and not trend_reversal)

    if trend_reversal:
        scenario_note = "Kısa ve 4 saatlik yapı ana yöne karşı hacimli kırılım gösteriyor; dönüşüm adayı henüz büyük zaman diliminde doğrulanmış değildir."
    elif counter_trend_warning:
        scenario_note = "Küçük zaman dilimindeki hareket kısa vadeli tepki niteliğinde; ana trendle ters işlem riski yüksek."
    elif conflict:
        scenario_note = "Zaman dilimleri arasında çelişki var; kapanış ve hacim teyidi beklenmeli."
    elif confluence_score >= 70:
        scenario_note = "Büyük ve küçük zaman dilimleri aynı yönde."
    else:
        scenario_note = "Zaman dilimleri kısmi uyum gösteriyor."

    available = [s for s in snapshots.values() if s.available]
    timestamps = [s.last_bar_timestamp for s in available if s.last_bar_timestamp is not None]
    return MultiTimeframeResult(
        symbol=symbol,
        snapshots=snapshots,
        confluence_score=confluence_score,
        primary_direction=primary_direction,
        short_term_direction=short_term_direction,
        conflict=conflict,
        counter_trend_warning=counter_trend_warning,
        scenario_note=scenario_note,
        large_timeframe_direction=primary_direction,
        small_timeframe_direction=short_term_direction,
        short_term_reaction=short_term_reaction,
        trend_reversal=trend_reversal,
        breakout_confirmed=breakout_confirmed,
        counter_trend_risk=counter_trend_warning,
        data_quality=f"{len(available)}/{len(timeframes)} zaman dilimi yeterli" if timeframes else "Veri yetersiz",
        data_timestamp=max(timestamps) if timestamps else None,
    )
