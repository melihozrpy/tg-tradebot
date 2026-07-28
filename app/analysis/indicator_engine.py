from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, cast

import numpy as np
import pandas as pd

MIN_BARS_FOR_FULL_ANALYSIS = 60


class InsufficientDataError(Exception):
    """Bir indikator icin yeterli mum verisi olmadiginda firlatilir."""


def ema(series: pd.Series, period: int) -> pd.Series:
    return cast(pd.Series, series.ewm(span=period, adjust=False, min_periods=period).mean())


def sma(series: pd.Series, period: int) -> pd.Series:
    return cast(pd.Series, series.rolling(window=period, min_periods=period).mean())


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    return result.fillna(50)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return cast(pd.Series, tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean())


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_val = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean() / atr_val.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(
        alpha=1 / period, min_periods=period, adjust=False
    ).mean() / atr_val.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return cast(pd.Series, dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean().fillna(0))


def obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["close"].diff().fillna(0))
    return cast(pd.Series, (direction * df["volume"]).cumsum())


def money_flow_index(df: pd.DataFrame, period: int = 14) -> pd.Series:
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    raw_money_flow = typical_price * df["volume"]
    direction = np.sign(typical_price.diff().fillna(0))
    positive_flow = pd.Series(np.where(direction > 0, raw_money_flow, 0.0), index=df.index)
    negative_flow = pd.Series(np.where(direction < 0, raw_money_flow, 0.0), index=df.index)
    positive_sum = positive_flow.rolling(period, min_periods=period).sum()
    negative_sum = negative_flow.rolling(period, min_periods=period).sum()
    money_ratio = positive_sum / negative_sum.replace(0, np.nan)
    return (100 - (100 / (1 + money_ratio))).fillna(50)


def bollinger_bands(series: pd.Series, period: int = 20, num_std: float = 2.0):
    mid = sma(series, period)
    std = series.rolling(period, min_periods=period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    width = (upper - lower) / mid.replace(0, np.nan)
    return upper, mid, lower, width


def relative_volume(df: pd.DataFrame, period: int = 20) -> pd.Series:
    avg_vol = df["volume"].rolling(period, min_periods=period).mean()
    return df["volume"] / avg_vol.replace(0, np.nan)


def pivot_support_resistance(df: pd.DataFrame, lookback: int = 20) -> tuple[Optional[float], Optional[float]]:
    if len(df) < lookback:
        return None, None
    window = df.tail(lookback)
    return float(window["low"].min()), float(window["high"].max())


@dataclass
class TechnicalSnapshot:
    symbol: str
    timeframe: str
    last_timestamp: pd.Timestamp
    close: float
    ema20: float
    ema50: float
    ema100: Optional[float]
    ema200: Optional[float]
    sma20: float
    sma50: float
    adx: float
    rsi: float
    macd_line: float
    macd_signal: float
    macd_histogram: float
    atr: float
    relative_volume: float
    obv_trend_up: bool
    mfi: float
    bb_width: float
    support: Optional[float]
    resistance: Optional[float]
    trend_direction: str  # up | down | sideways
    volume_confirmed: bool
    bars_used: int
    extras: dict = field(default_factory=dict)


def compute_technical_snapshot(df: pd.DataFrame, symbol: str, timeframe: str) -> TechnicalSnapshot:
    """Verilen OHLCV DataFrame'inden tam bir teknik gorunum uretir.

    Yetersiz veri durumunda InsufficientDataError firlatir; boylece
    cagiran taraf (signal engine) fail-closed davranip sinyal uretmez.
    """
    required_cols = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required_cols - set(df.columns)
    if missing:
        raise InsufficientDataError(f"Eksik kolonlar: {missing}")

    if len(df) < MIN_BARS_FOR_FULL_ANALYSIS:
        raise InsufficientDataError(
            f"{symbol}/{timeframe} icin yeterli mum yok: {len(df)} < {MIN_BARS_FOR_FULL_ANALYSIS}"
        )

    df = df.sort_values("timestamp").reset_index(drop=True)
    close = df["close"]

    ema20_s = ema(close, 20)
    ema50_s = ema(close, 50)
    ema100_s = ema(close, 100) if len(df) >= 100 else pd.Series([np.nan] * len(df))
    ema200_s = ema(close, 200) if len(df) >= 200 else pd.Series([np.nan] * len(df))
    sma20_s = sma(close, 20)
    sma50_s = sma(close, 50)
    adx_s = adx(df, 14)
    rsi_s = rsi(close, 14)
    macd_line_s, macd_signal_s, macd_hist_s = macd(close)
    atr_s = atr(df, 14)
    rel_vol_s = relative_volume(df, 20)
    obv_s = obv(df)
    mfi_s = money_flow_index(df, 14)
    _, _, _, bb_width_s = bollinger_bands(close, 20, 2.0)
    support, resistance = pivot_support_resistance(df, lookback=20)

    last = -1
    ema20_last = float(ema20_s.iloc[last])
    ema50_last = float(ema50_s.iloc[last])

    if ema20_last > ema50_last and close.iloc[last] > ema20_last:
        trend_direction = "up"
    elif ema20_last < ema50_last and close.iloc[last] < ema20_last:
        trend_direction = "down"
    else:
        trend_direction = "sideways"

    obv_trend_up = bool(obv_s.iloc[last] > obv_s.tail(10).mean())
    rel_vol_last = float(rel_vol_s.iloc[last]) if not np.isnan(rel_vol_s.iloc[last]) else 0.0
    volume_confirmed = rel_vol_last >= 1.4

    return TechnicalSnapshot(
        symbol=symbol,
        timeframe=timeframe,
        last_timestamp=df["timestamp"].iloc[last],
        close=float(close.iloc[last]),
        ema20=ema20_last,
        ema50=ema50_last,
        ema100=float(ema100_s.iloc[last]) if not np.isnan(ema100_s.iloc[last]) else None,
        ema200=float(ema200_s.iloc[last]) if not np.isnan(ema200_s.iloc[last]) else None,
        sma20=float(sma20_s.iloc[last]),
        sma50=float(sma50_s.iloc[last]),
        adx=float(adx_s.iloc[last]),
        rsi=float(rsi_s.iloc[last]),
        macd_line=float(macd_line_s.iloc[last]),
        macd_signal=float(macd_signal_s.iloc[last]),
        macd_histogram=float(macd_hist_s.iloc[last]),
        atr=float(atr_s.iloc[last]),
        relative_volume=rel_vol_last,
        obv_trend_up=obv_trend_up,
        mfi=float(mfi_s.iloc[last]),
        bb_width=float(bb_width_s.iloc[last]) if not np.isnan(bb_width_s.iloc[last]) else 0.0,
        support=support,
        resistance=resistance,
        trend_direction=trend_direction,
        volume_confirmed=volume_confirmed,
        bars_used=len(df),
    )
