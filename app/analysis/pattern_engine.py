from __future__ import annotations

"""Conservative classical-chart-pattern detection.

The module intentionally reports *candidates* separately from confirmed
breakouts.  A geometric resemblance is not a trading signal: a pattern gets
``confirmed=True`` only when the latest completed close is beyond its
neckline/trendline.  This keeps the screener from presenting unfinished
triangles, wedges or double tops as facts.
"""

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd


PatternDirection = Literal["bullish", "bearish", "neutral"]
PatternKind = Literal["reversal", "continuation", "bilateral"]


@dataclass(frozen=True)
class ChartPattern:
    """One recent pattern candidate built from completed OHLC candles."""

    name: str
    kind: PatternKind
    direction: PatternDirection
    confirmed: bool
    confidence: int
    breakout_level: float
    target: float | None
    detail: str


def _finite(values: pd.Series) -> np.ndarray:
    return np.asarray(pd.to_numeric(values, errors="coerce"), dtype=float)


def _pivots(values: np.ndarray, *, window: int = 3, mode: Literal["high", "low"]) -> list[tuple[int, float]]:
    """Return local extrema without using the incomplete last ``window`` bars."""

    output: list[tuple[int, float]] = []
    if len(values) < window * 2 + 3:
        return output
    for index in range(window, len(values) - window):
        area = values[index - window : index + window + 1]
        value = values[index]
        if not np.isfinite(value) or not np.isfinite(area).all():
            continue
        is_extreme = value == (np.max(area) if mode == "high" else np.min(area))
        if is_extreme:
            output.append((index, float(value)))
    return output


def _linear(points: list[tuple[int, float]]) -> tuple[float, float] | None:
    if len(points) < 2:
        return None
    xs = np.asarray([point[0] for point in points], dtype=float)
    ys = np.asarray([point[1] for point in points], dtype=float)
    if len(np.unique(xs)) < 2:
        return None
    slope, intercept = np.polyfit(xs, ys, 1)
    return float(slope), float(intercept)


def _line_at(model: tuple[float, float], x: int) -> float:
    return model[0] * x + model[1]


def _similar(left: float, right: float, tolerance: float = 0.028) -> bool:
    reference = max((abs(left) + abs(right)) / 2.0, 1e-9)
    return abs(left - right) / reference <= tolerance


def _append_unique(result: list[ChartPattern], pattern: ChartPattern) -> None:
    """Keep the most useful one per name/direction and prevent duplicate text."""

    for index, prior in enumerate(result):
        if prior.name == pattern.name and prior.direction == pattern.direction:
            if (pattern.confirmed, pattern.confidence) > (prior.confirmed, prior.confidence):
                result[index] = pattern
            return
    result.append(pattern)


def _double_patterns(
    highs: list[tuple[int, float]],
    lows: list[tuple[int, float]],
    close: float,
    result: list[ChartPattern],
) -> None:
    if len(lows) >= 2:
        first, second = lows[-2], lows[-1]
        between_highs = [value for index, value in highs if first[0] < index < second[0]]
        if second[0] - first[0] >= 4 and between_highs and _similar(first[1], second[1]):
            neckline = max(between_highs)
            height = neckline - min(first[1], second[1])
            confirmed = close > neckline
            _append_unique(
                result,
                ChartPattern(
                    "İkili Dip",
                    "reversal",
                    "bullish" if confirmed else "neutral",
                    confirmed,
                    82 if confirmed else 58,
                    neckline,
                    neckline + height if height > 0 else None,
                    f"Boyun çizgisi {_money(neckline)}; {'üstü kapanış teyidi var' if confirmed else 'üstü kapanış bekleniyor'}.",
                ),
            )
    if len(highs) >= 2:
        first, second = highs[-2], highs[-1]
        between_lows = [value for index, value in lows if first[0] < index < second[0]]
        if second[0] - first[0] >= 4 and between_lows and _similar(first[1], second[1]):
            neckline = min(between_lows)
            height = max(first[1], second[1]) - neckline
            confirmed = close < neckline
            _append_unique(
                result,
                ChartPattern(
                    "İkili Tepe",
                    "reversal",
                    "bearish" if confirmed else "neutral",
                    confirmed,
                    82 if confirmed else 58,
                    neckline,
                    neckline - height if height > 0 else None,
                    f"Boyun çizgisi {_money(neckline)}; {'altı kapanış teyidi var' if confirmed else 'altı kapanış bekleniyor'}.",
                ),
            )


def _head_shoulders_patterns(
    highs: list[tuple[int, float]],
    lows: list[tuple[int, float]],
    close: float,
    result: list[ChartPattern],
) -> None:
    if len(highs) >= 3:
        left, head, right = highs[-3:]
        valleys = [value for index, value in lows if left[0] < index < right[0]]
        shoulders_ok = _similar(left[1], right[1], tolerance=0.05)
        if shoulders_ok and head[1] > max(left[1], right[1]) * 1.012 and len(valleys) >= 2:
            neckline = float(np.mean(valleys[-2:]))
            confirmed = close < neckline
            _append_unique(
                result,
                ChartPattern(
                    "Omuz Baş Omuz",
                    "reversal",
                    "bearish" if confirmed else "neutral",
                    confirmed,
                    85 if confirmed else 60,
                    neckline,
                    neckline - (head[1] - neckline),
                    f"Boyun çizgisi {_money(neckline)}; {'altı kapanış teyidi var' if confirmed else 'kırılım bekleniyor'}.",
                ),
            )
    if len(lows) >= 3:
        left, head, right = lows[-3:]
        peaks = [value for index, value in highs if left[0] < index < right[0]]
        shoulders_ok = _similar(left[1], right[1], tolerance=0.05)
        if shoulders_ok and head[1] < min(left[1], right[1]) * 0.988 and len(peaks) >= 2:
            neckline = float(np.mean(peaks[-2:]))
            confirmed = close > neckline
            _append_unique(
                result,
                ChartPattern(
                    "Ters Omuz Baş Omuz",
                    "reversal",
                    "bullish" if confirmed else "neutral",
                    confirmed,
                    85 if confirmed else 60,
                    neckline,
                    neckline + (neckline - head[1]),
                    f"Boyun çizgisi {_money(neckline)}; {'üstü kapanış teyidi var' if confirmed else 'kırılım bekleniyor'}.",
                ),
            )


def _triangle_and_wedge_patterns(
    highs: list[tuple[int, float]],
    lows: list[tuple[int, float]],
    close: float,
    last_index: int,
    result: list[ChartPattern],
) -> None:
    recent_highs, recent_lows = highs[-3:], lows[-3:]
    high_model, low_model = _linear(recent_highs), _linear(recent_lows)
    if high_model is None or low_model is None:
        return
    upper, lower = _line_at(high_model, last_index), _line_at(low_model, last_index)
    width_start = _line_at(high_model, min(recent_highs[0][0], recent_lows[0][0])) - _line_at(
        low_model, min(recent_highs[0][0], recent_lows[0][0])
    )
    width_now = upper - lower
    if width_start <= 0 or width_now <= 0 or width_now > width_start * 0.9:
        return

    scale = max(abs(upper), abs(lower), 1e-9)
    high_flat = abs(high_model[0]) / scale < 0.0009
    low_flat = abs(low_model[0]) / scale < 0.0009
    if high_model[0] < 0 < low_model[0]:
        name, kind = "Simetrik Üçgen", "bilateral"
    elif high_flat and low_model[0] > 0:
        name, kind = "Yükselen Üçgen", "continuation"
    elif low_flat and high_model[0] < 0:
        name, kind = "Alçalan Üçgen", "continuation"
    elif high_model[0] > 0 and low_model[0] > 0:
        name, kind = "Yükselen Takoz", "reversal"
    elif high_model[0] < 0 and low_model[0] < 0:
        name, kind = "Alçalan Takoz", "reversal"
    else:
        return

    if close > upper:
        direction: PatternDirection = "bullish"
        level = upper
        target = upper + width_start
    elif close < lower:
        direction = "bearish"
        level = lower
        target = lower - width_start
    else:
        direction = "neutral"
        level = upper if name not in {"Alçalan Üçgen", "Yükselen Takoz"} else lower
        target = None
    _append_unique(
        result,
        ChartPattern(
            name,
            kind,
            direction,
            direction != "neutral",
            80 if direction != "neutral" else 55,
            level,
            target,
            (
                f"{'Kırılım teyidi' if direction != 'neutral' else 'Sıkışma sürüyor'}; "
                f"izlenen çizgi {_money(level)}."
            ),
        ),
    )


def _rectangle_and_flag_patterns(
    high_values: np.ndarray,
    low_values: np.ndarray,
    close_values: np.ndarray,
    close: float,
    result: list[ChartPattern],
) -> None:
    if len(close_values) < 28:
        return
    consolidation_high = float(np.nanmax(high_values[-16:-1]))
    consolidation_low = float(np.nanmin(low_values[-16:-1]))
    height = consolidation_high - consolidation_low
    mid = (consolidation_high + consolidation_low) / 2
    if height <= 0 or mid <= 0:
        return
    compact = height / mid <= 0.15
    if compact:
        if close > consolidation_high:
            direction: PatternDirection = "bullish"
            level, target = consolidation_high, consolidation_high + height
        elif close < consolidation_low:
            direction = "bearish"
            level, target = consolidation_low, consolidation_low - height
        else:
            direction = "neutral"
            level, target = consolidation_high, None
        _append_unique(
            result,
            ChartPattern(
                "Dikdörtgen",
                "continuation",
                direction,
                direction != "neutral",
                76 if direction != "neutral" else 52,
                level,
                target,
                f"Band {_money(consolidation_low)}–{_money(consolidation_high)}; "
                f"{'kapanışla kırıldı' if direction != 'neutral' else 'kırılım bekleniyor'}.",
            ),
        )

    impulse_base = float(close_values[-28])
    impulse_end = float(close_values[-17])
    if impulse_base <= 0:
        return
    impulse_change = (impulse_end / impulse_base - 1) * 100
    channel = close_values[-16:-1]
    model = _linear([(index, float(value)) for index, value in enumerate(channel)])
    if model is None:
        return
    trend = model[0] / max(abs(float(np.nanmean(channel))), 1e-9)
    if impulse_change >= 5 and trend < 0 and close > consolidation_high:
        _append_unique(
            result,
            ChartPattern(
                "Boğa Bayrağı",
                "continuation",
                "bullish",
                True,
                78,
                consolidation_high,
                consolidation_high + max(height, impulse_end - impulse_base),
                f"Yükseliş sonrası geri çekilme {_money(consolidation_high)} üstünde kırıldı.",
            ),
        )
    elif impulse_change <= -5 and trend > 0 and close < consolidation_low:
        _append_unique(
            result,
            ChartPattern(
                "Ayı Bayrağı",
                "continuation",
                "bearish",
                True,
                78,
                consolidation_low,
                consolidation_low - max(height, impulse_base - impulse_end),
                f"Düşüş sonrası tepki {_money(consolidation_low)} altında kırıldı.",
            ),
        )


def _money(value: float) -> str:
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def detect_chart_patterns(frame: pd.DataFrame, *, lookback: int = 90) -> tuple[ChartPattern, ...]:
    """Detect the patterns shown in the reference images on recent OHLC data.

    The detector covers double top/bottom, head-and-shoulders variants,
    rectangles, triangles, wedges and bull/bear flags.  It is deliberately
    conservative and returns at most three high-confidence recent patterns.
    A caller should add a confirmation only for ``confirmed`` records whose
    ``direction`` matches the intended trade direction.
    """

    required = {"high", "low", "close"}
    if not required.issubset(frame.columns) or len(frame) < 40:
        return ()
    data = frame.tail(max(40, lookback)).reset_index(drop=True)
    highs, lows, closes = _finite(data["high"]), _finite(data["low"]), _finite(data["close"])
    if not (np.isfinite(highs).all() and np.isfinite(lows).all() and np.isfinite(closes).all()):
        return ()
    close = float(closes[-1])
    pivot_highs = _pivots(highs, mode="high")
    pivot_lows = _pivots(lows, mode="low")
    result: list[ChartPattern] = []
    _double_patterns(pivot_highs, pivot_lows, close, result)
    _head_shoulders_patterns(pivot_highs, pivot_lows, close, result)
    _triangle_and_wedge_patterns(pivot_highs, pivot_lows, close, len(data) - 1, result)
    _rectangle_and_flag_patterns(highs, lows, closes, close, result)
    result.sort(key=lambda item: (item.confirmed, item.confidence, item.name), reverse=True)
    return tuple(result[:3])
