from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, cast

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


def session_vwap(
    df: pd.DataFrame,
    *,
    timezone_name: str = "Europe/Istanbul",
) -> pd.Series:
    """Return session VWAP without carrying volume between trading days.

    Intraday timestamps are grouped by their Istanbul trading date.  With daily
    candles the result is necessarily the candle typical price because a daily
    bar does not contain the intraday distribution; callers can expose that
    limitation instead of fabricating a more precise value.
    """

    required = {"timestamp", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise InsufficientDataError(f"VWAP icin eksik kolonlar: {missing}")
    timestamps = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    try:
        session_key = timestamps.dt.tz_convert(timezone_name).dt.date
    except (TypeError, ValueError):
        session_key = timestamps.dt.date
    typical = (
        pd.to_numeric(df["high"], errors="coerce")
        + pd.to_numeric(df["low"], errors="coerce")
        + pd.to_numeric(df["close"], errors="coerce")
    ) / 3.0
    volume = pd.to_numeric(df["volume"], errors="coerce").clip(lower=0).fillna(0.0)
    cumulative_value = (typical * volume).groupby(session_key).cumsum()
    cumulative_volume = volume.groupby(session_key).cumsum().replace(0, np.nan)
    return cast(pd.Series, (cumulative_value / cumulative_volume).fillna(typical))


def _latest_swing_anchor(df: pd.DataFrame, lookback: int = 80, pivot_window: int = 3) -> int:
    """Choose the latest confirmed swing high/low, never the unfinished last bar."""

    data = df.tail(max(lookback, pivot_window * 2 + 3))
    high = pd.to_numeric(data["high"], errors="coerce")
    low = pd.to_numeric(data["low"], errors="coerce")
    window = pivot_window * 2 + 1
    swing_high = high.eq(high.rolling(window, center=True, min_periods=window).max())
    swing_low = low.eq(low.rolling(window, center=True, min_periods=window).min())
    candidates = data.index[(swing_high | swing_low).fillna(False)].tolist()
    return int(candidates[-1]) if candidates else int(data.index[0])


def anchored_vwap(
    df: pd.DataFrame,
    *,
    anchor_index: int | None = None,
    anchor_date: str | pd.Timestamp | None = None,
) -> tuple[pd.Series, int]:
    """Return anchored VWAP and the resolved anchor row index.

    The caller may provide a row index or date.  When neither is supplied the
    most recent confirmed swing high/low is selected deterministically.
    Values before the anchor stay ``NaN`` so they cannot be mistaken for data.
    """

    required = {"timestamp", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise InsufficientDataError(f"Anchored VWAP icin eksik kolonlar: {missing}")
    if df.empty:
        raise InsufficientDataError("Anchored VWAP icin mum verisi yok.")
    data = df.reset_index(drop=True)
    if anchor_date is not None:
        timestamps = pd.to_datetime(data["timestamp"], utc=True, errors="coerce")
        wanted = pd.to_datetime(anchor_date, utc=True, errors="coerce")
        matches = data.index[timestamps >= wanted].tolist()
        if not matches:
            raise InsufficientDataError("Anchored VWAP tarihi veri araliginin disinda.")
        resolved = int(matches[0])
    elif anchor_index is not None:
        resolved = max(0, min(int(anchor_index), len(data) - 1))
    else:
        resolved = _latest_swing_anchor(data)

    typical = (data["high"] + data["low"] + data["close"]) / 3.0
    volume = pd.to_numeric(data["volume"], errors="coerce").clip(lower=0).fillna(0.0)
    result = pd.Series(np.nan, index=data.index, dtype=float)
    anchored_value = (typical.iloc[resolved:] * volume.iloc[resolved:]).cumsum()
    anchored_volume = volume.iloc[resolved:].cumsum().replace(0, np.nan)
    result.iloc[resolved:] = (anchored_value / anchored_volume).fillna(typical.iloc[resolved:])
    return result, resolved


def supertrend(
    df: pd.DataFrame,
    period: int = 10,
    multiplier: float = 3.0,
) -> tuple[pd.Series, pd.Series]:
    """Compute a standard ATR Supertrend line and direction (1 / -1)."""

    if len(df) < period + 2:
        raise InsufficientDataError(f"Supertrend icin en az {period + 2} mum gerekir.")
    data = df.reset_index(drop=True)
    midpoint = (data["high"] + data["low"]) / 2.0
    atr_values = atr(data, period).bfill()
    basic_upper = midpoint + multiplier * atr_values
    basic_lower = midpoint - multiplier * atr_values
    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()
    direction = pd.Series(1, index=data.index, dtype=int)
    line = pd.Series(np.nan, index=data.index, dtype=float)

    for idx in range(1, len(data)):
        if basic_upper.iloc[idx] < final_upper.iloc[idx - 1] or data["close"].iloc[idx - 1] > final_upper.iloc[idx - 1]:
            final_upper.iloc[idx] = basic_upper.iloc[idx]
        else:
            final_upper.iloc[idx] = final_upper.iloc[idx - 1]
        if basic_lower.iloc[idx] > final_lower.iloc[idx - 1] or data["close"].iloc[idx - 1] < final_lower.iloc[idx - 1]:
            final_lower.iloc[idx] = basic_lower.iloc[idx]
        else:
            final_lower.iloc[idx] = final_lower.iloc[idx - 1]

        previous_direction = int(direction.iloc[idx - 1])
        if previous_direction < 0 and data["close"].iloc[idx] > final_upper.iloc[idx]:
            direction.iloc[idx] = 1
        elif previous_direction > 0 and data["close"].iloc[idx] < final_lower.iloc[idx]:
            direction.iloc[idx] = -1
        else:
            direction.iloc[idx] = previous_direction
        line.iloc[idx] = final_lower.iloc[idx] if direction.iloc[idx] > 0 else final_upper.iloc[idx]

    line.iloc[0] = final_lower.iloc[0]
    return line, direction


@dataclass(frozen=True)
class VolumeProfileResult:
    poc: float | None
    vah: float | None
    val: float | None
    value_area_percent: float
    mode: Literal["ohlcv_approximation", "volume_average_fallback"]
    average_volume_20: float


def volume_profile(
    df: pd.DataFrame,
    *,
    lookback: int = 120,
    bins: int = 32,
    value_area_percent: float = 0.70,
) -> VolumeProfileResult:
    """Approximate price-by-volume POC/VAH/VAL from OHLCV candles.

    This is explicitly marked as an OHLCV approximation.  If usable volume is
    absent, POC/VAH/VAL remain ``None`` and only the 20-bar volume average is
    returned as the documented fallback.
    """

    data = df.tail(max(20, lookback)).copy()
    volume = pd.to_numeric(data.get("volume"), errors="coerce").clip(lower=0).fillna(0.0)
    average_volume = float(volume.tail(20).mean()) if len(volume) else 0.0
    if data.empty or float(volume.sum()) <= 0:
        return VolumeProfileResult(None, None, None, value_area_percent, "volume_average_fallback", average_volume)
    typical = (
        pd.to_numeric(data["high"], errors="coerce")
        + pd.to_numeric(data["low"], errors="coerce")
        + pd.to_numeric(data["close"], errors="coerce")
    ) / 3.0
    valid = typical.notna() & volume.gt(0)
    typical, volume = typical[valid], volume[valid]
    if typical.empty or float(typical.max()) <= float(typical.min()):
        price = float(typical.iloc[-1]) if not typical.empty else None
        return VolumeProfileResult(price, price, price, value_area_percent, "ohlcv_approximation", average_volume)

    edges = np.linspace(float(typical.min()), float(typical.max()), max(8, int(bins)) + 1)
    bucket = np.clip(np.digitize(typical.to_numpy(), edges) - 1, 0, len(edges) - 2)
    profile = np.bincount(bucket, weights=volume.to_numpy(), minlength=len(edges) - 1)
    centres = (edges[:-1] + edges[1:]) / 2.0
    poc_index = int(np.argmax(profile))
    selected = {poc_index}
    accumulated = float(profile[poc_index])
    target = float(profile.sum()) * max(0.5, min(0.95, value_area_percent))
    left, right = poc_index - 1, poc_index + 1
    while accumulated < target and (left >= 0 or right < len(profile)):
        left_volume = float(profile[left]) if left >= 0 else -1.0
        right_volume = float(profile[right]) if right < len(profile) else -1.0
        chosen = left if left_volume >= right_volume else right
        selected.add(chosen)
        accumulated += float(profile[chosen])
        if chosen == left:
            left -= 1
        else:
            right += 1
    return VolumeProfileResult(
        poc=float(centres[poc_index]),
        vah=float(edges[max(selected) + 1]),
        val=float(edges[min(selected)]),
        value_area_percent=value_area_percent,
        mode="ohlcv_approximation",
        average_volume_20=average_volume,
    )


@dataclass(frozen=True)
class IndicatorBundle:
    symbol: str
    timeframe: str
    frame: pd.DataFrame
    volume_profile: VolumeProfileResult
    anchor_index: int
    session_vwap_mode: Literal["intraday_session", "daily_typical_price_fallback"]

    @property
    def latest(self) -> pd.Series:
        return self.frame.iloc[-1]


@dataclass(frozen=True)
class ConfluenceResult:
    direction: Literal["bullish", "bearish", "neutral"]
    confirmations: tuple[str, ...]
    conflicts: tuple[str, ...]
    score: int
    minimum_required: int

    @property
    def qualified(self) -> bool:
        return len(self.confirmations) >= self.minimum_required


def compute_indicator_bundle(
    df: pd.DataFrame,
    *,
    symbol: str = "",
    timeframe: str = "1d",
    anchor_index: int | None = None,
    anchor_date: str | pd.Timestamp | None = None,
) -> IndicatorBundle:
    """Calculate the shared ten-indicator set once for every consumer."""

    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise InsufficientDataError(f"Ortak indikator motoru icin eksik kolonlar: {missing}")
    if len(df) < MIN_BARS_FOR_FULL_ANALYSIS:
        raise InsufficientDataError(
            f"{symbol or 'sembol'}/{timeframe} icin yeterli mum yok: {len(df)} < {MIN_BARS_FOR_FULL_ANALYSIS}"
        )
    data = df.sort_values("timestamp").reset_index(drop=True).copy()
    close = pd.to_numeric(data["close"], errors="coerce")
    for period in (20, 50, 100, 200):
        data[f"ema{period}"] = ema(close, period) if len(data) >= period else np.nan
    data["vwap"] = session_vwap(data)
    data["anchored_vwap"], resolved_anchor = anchored_vwap(
        data, anchor_index=anchor_index, anchor_date=anchor_date
    )
    data["supertrend"], data["supertrend_direction"] = supertrend(data, 10, 3.0)
    data["rsi14"] = rsi(close, 14)
    data["macd"], data["macd_signal"], data["macd_histogram"] = macd(close, 12, 26, 9)
    data["adx14"] = adx(data, 14)
    data["bb_upper"], data["bb_mid"], data["bb_lower"], data["bb_width"] = bollinger_bands(close, 20, 2.0)
    data["obv"] = obv(data)
    data["relative_volume"] = relative_volume(data, 20)
    return IndicatorBundle(
        symbol=symbol.upper().removesuffix(".IS"),
        timeframe=timeframe,
        frame=data,
        volume_profile=volume_profile(data),
        anchor_index=resolved_anchor,
        session_vwap_mode=(
            "intraday_session"
            if timeframe.casefold() in {"1m", "5m", "15m", "30m", "1h", "4h"}
            else "daily_typical_price_fallback"
        ),
    )


def evaluate_indicator_confluence(
    bundle: IndicatorBundle,
    direction: Literal["bullish", "bearish"],
    *,
    minimum_required: int = 3,
) -> ConfluenceResult:
    """Require independent confirmations; a single indicator never qualifies."""

    last = bundle.latest
    previous = bundle.frame.iloc[-2]
    bullish = direction == "bullish"
    confirmations: list[str] = []
    conflicts: list[str] = []

    def add(condition: bool, positive: str, negative: str) -> None:
        (confirmations if condition else conflicts).append(positive if condition else negative)

    ema_condition = bool(last["ema50"] > last["ema100"]) if pd.notna(last["ema100"]) else bool(last["ema20"] > last["ema50"])
    add(ema_condition == bullish, "EMA trend siralamasi uyumlu", "EMA trend siralamasi ters")
    add((int(last["supertrend_direction"]) > 0) == bullish, "Supertrend yonu uyumlu", "Supertrend yonu ters")
    if bundle.session_vwap_mode == "intraday_session":
        add((float(last["close"]) > float(last["vwap"])) == bullish, "Fiyat session VWAP ile uyumlu", "Fiyat session VWAP ile uyumsuz")
    else:
        conflicts.append("Gunluk mumdan gercek session VWAP dogrulanamaz; teyit sayilmadi")
    add((float(last["macd_histogram"]) > 0) == bullish, "MACD momentumu uyumlu", "MACD momentumu ters")
    rsi_value = float(last["rsi14"])
    rsi_condition = 50 <= rsi_value < 75 if bullish else 25 < rsi_value <= 50
    add(rsi_condition, f"RSI yonu destekliyor ({rsi_value:.1f})", f"RSI yonu desteklemiyor ({rsi_value:.1f})")
    obv_condition = float(last["obv"]) > float(previous["obv"])
    add(obv_condition == bullish, "OBV fiyat yonunu teyit ediyor", "OBV teyidi yok")
    if float(last["adx14"]) < 20:
        conflicts.append(f"ADX {float(last['adx14']):.1f}: yatay/zayif trend")
    else:
        confirmations.append(f"ADX {float(last['adx14']):.1f}: trend gucu yeterli")
    score = round(len(confirmations) / max(1, len(confirmations) + len(conflicts)) * 100)
    return ConfluenceResult(
        direction=direction if len(confirmations) >= minimum_required else "neutral",
        confirmations=tuple(confirmations),
        conflicts=tuple(conflicts),
        score=score,
        minimum_required=max(3, int(minimum_required)),
    )


def evaluate_ten_indicator_confluence(
    bundle: IndicatorBundle,
    direction: Literal["bullish", "bearish"],
    *,
    minimum_required: int = 5,
) -> ConfluenceResult:
    """Evaluate every component of the shared ten-indicator engine.

    This stricter evaluator is intentionally separate from
    :func:`evaluate_indicator_confluence`: legacy alarms and the 15-minute
    radar retain their established 3+ confirmation behaviour, while commands
    that explicitly promise a ten-indicator filter can require 5--10 distinct
    confirmations.  Each of the following contributes exactly one result:
    EMA stack, session VWAP, anchored VWAP, Supertrend, RSI, MACD, ADX,
    Bollinger position, OBV trend and volume-profile POC position.
    """

    last = bundle.latest
    frame = bundle.frame
    bullish = direction == "bullish"
    confirmations: list[str] = []
    conflicts: list[str] = []

    def add(condition: bool, positive: str, negative: str) -> None:
        (confirmations if condition else conflicts).append(positive if condition else negative)

    close = float(last["close"])
    ema_values = (last["ema20"], last["ema50"], last["ema100"])
    if all(pd.notna(value) for value in ema_values):
        ema_bullish = bool(last["ema20"] > last["ema50"] > last["ema100"])
        ema_bearish = bool(last["ema20"] < last["ema50"] < last["ema100"])
        add(
            ema_bullish if bullish else ema_bearish,
            "EMA20/50/100 siralamasi uyumlu",
            "EMA20/50/100 siralamasi ters veya karisik",
        )
    else:
        conflicts.append("EMA20/50/100 icin yeterli gecmis yok")

    vwap_value = last.get("vwap")
    if pd.notna(vwap_value):
        add(
            (close > float(vwap_value)) == bullish,
            "Fiyat session VWAP ile uyumlu",
            "Fiyat session VWAP ile uyumsuz",
        )
    else:
        conflicts.append("Session VWAP hesaplanamadi")

    anchored_vwap_value = last.get("anchored_vwap")
    if pd.notna(anchored_vwap_value):
        add(
            (close > float(anchored_vwap_value)) == bullish,
            "Fiyat anchored VWAP ile uyumlu",
            "Fiyat anchored VWAP ile uyumsuz",
        )
    else:
        conflicts.append("Anchored VWAP hesaplanamadi")

    add(
        (int(last["supertrend_direction"]) > 0) == bullish,
        "Supertrend yonu uyumlu",
        "Supertrend yonu ters",
    )

    rsi_value = float(last["rsi14"])
    rsi_condition = 50 <= rsi_value < 75 if bullish else 25 < rsi_value <= 50
    add(
        rsi_condition,
        f"RSI yonu destekliyor ({rsi_value:.1f})",
        f"RSI yonu desteklemiyor ({rsi_value:.1f})",
    )

    macd_value = last.get("macd_histogram")
    if pd.notna(macd_value):
        add(
            (float(macd_value) > 0) == bullish,
            "MACD momentumu uyumlu",
            "MACD momentumu ters",
        )
    else:
        conflicts.append("MACD hesaplanamadi")

    adx_value = last.get("adx14")
    if pd.notna(adx_value) and float(adx_value) >= 20:
        confirmations.append(f"ADX {float(adx_value):.1f}: trend gucu yeterli")
    else:
        displayed = float(adx_value) if pd.notna(adx_value) else 0.0
        conflicts.append(f"ADX {displayed:.1f}: yatay/zayif trend")

    bb_mid, bb_upper, bb_lower = last.get("bb_mid"), last.get("bb_upper"), last.get("bb_lower")
    if all(pd.notna(value) for value in (bb_mid, bb_upper, bb_lower)):
        # A bandin cok disina tasan fiyat momentum gosterebilir, ancak yeni
        # giris icin kovalamaca riski tasir; bu yuzden teyit sayilmaz.
        bb_bullish = float(bb_mid) <= close <= float(bb_upper) * 1.01
        bb_bearish = float(bb_lower) * 0.99 <= close <= float(bb_mid)
        add(
            bb_bullish if bullish else bb_bearish,
            "Bollinger konumu yonu destekliyor",
            "Bollinger konumu yonu desteklemiyor/kovalama riski var",
        )
    else:
        conflicts.append("Bollinger bantlari hesaplanamadi")

    obv_lookback = frame["obv"].iloc[max(0, len(frame) - 6)]
    obv_bullish = bool(float(last["obv"]) > float(obv_lookback))
    add(
        obv_bullish == bullish,
        "OBV hacim akisi yonu teyit ediyor",
        "OBV hacim akisi yonu teyit etmiyor",
    )

    poc = bundle.volume_profile.poc
    if poc is not None:
        add(
            (close >= float(poc)) == bullish,
            "Fiyat volume profile POC tarafinda uyumlu",
            "Fiyat volume profile POC tarafinda ters",
        )
    else:
        conflicts.append("Volume profile POC verisi yok")

    score = round(len(confirmations) / 10 * 100)
    return ConfluenceResult(
        direction=direction if len(confirmations) >= max(3, min(10, int(minimum_required))) else "neutral",
        confirmations=tuple(confirmations),
        conflicts=tuple(conflicts),
        score=score,
        minimum_required=max(3, min(10, int(minimum_required))),
    )


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

    bundle = compute_indicator_bundle(df, symbol=symbol, timeframe=timeframe)
    df = bundle.frame
    close = df["close"]

    ema20_s = df["ema20"]
    ema50_s = df["ema50"]
    ema100_s = df["ema100"]
    ema200_s = df["ema200"]
    sma20_s = sma(close, 20)
    sma50_s = sma(close, 50)
    adx_s = df["adx14"]
    rsi_s = df["rsi14"]
    macd_line_s, macd_signal_s, macd_hist_s = df["macd"], df["macd_signal"], df["macd_histogram"]
    atr_s = atr(df, 14)
    rel_vol_s = df["relative_volume"]
    obv_s = df["obv"]
    mfi_s = money_flow_index(df, 14)
    bb_width_s = df["bb_width"]
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
        extras={
            "vwap": float(df["vwap"].iloc[last]),
            "anchored_vwap": float(df["anchored_vwap"].iloc[last]),
            "anchored_vwap_index": bundle.anchor_index,
            "supertrend": float(df["supertrend"].iloc[last]),
            "supertrend_direction": int(df["supertrend_direction"].iloc[last]),
            "bollinger_upper": float(df["bb_upper"].iloc[last]),
            "bollinger_mid": float(df["bb_mid"].iloc[last]),
            "bollinger_lower": float(df["bb_lower"].iloc[last]),
            "volume_profile_mode": bundle.volume_profile.mode,
            "volume_profile_poc": bundle.volume_profile.poc,
            "volume_profile_vah": bundle.volume_profile.vah,
            "volume_profile_val": bundle.volume_profile.val,
            "average_volume_20": bundle.volume_profile.average_volume_20,
            "session_vwap_mode": bundle.session_vwap_mode,
        },
    )
