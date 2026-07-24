from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class PriceZone:
    kind: str
    low: float
    high: float
    index: int
    direction: str


@dataclass(frozen=True)
class StructureEvent:
    kind: str
    price: float
    index: int
    direction: str


@dataclass(frozen=True)
class SmartMoneyResult:
    fvg: tuple[PriceZone, ...]
    order_blocks: tuple[PriceZone, ...]
    structure: tuple[StructureEvent, ...]


def detect_smart_money(df: pd.DataFrame, swing_window: int = 3) -> SmartMoneyResult:
    data = df.sort_values("timestamp").reset_index(drop=True)
    fvg: list[PriceZone] = []
    blocks: list[PriceZone] = []
    events: list[StructureEvent] = []
    for i in range(2, len(data)):
        if float(data.loc[i, "low"]) > float(data.loc[i - 2, "high"]):
            fvg.append(PriceZone("FVG", float(data.loc[i - 2, "high"]), float(data.loc[i, "low"]), i, "bullish"))
        elif float(data.loc[i, "high"]) < float(data.loc[i - 2, "low"]):
            fvg.append(PriceZone("FVG", float(data.loc[i, "high"]), float(data.loc[i - 2, "low"]), i, "bearish"))
    last_high = last_low = None
    trend = None
    for i in range(swing_window, len(data) - swing_window):
        high = float(data.loc[i, "high"]); low = float(data.loc[i, "low"])
        local = data.iloc[i - swing_window:i + swing_window + 1]
        if high >= float(local["high"].max()):
            if last_high is not None and high > last_high:
                kind = "MSS" if trend == "bearish" else "BOS"
                events.append(StructureEvent(kind, high, i, "bullish")); trend = "bullish"
                candle = data.iloc[max(0, i - 1)]
                blocks.append(PriceZone("OB", float(candle["low"]), float(candle["high"]), i - 1, "bullish"))
            last_high = high
        if low <= float(local["low"].min()):
            if last_low is not None and low < last_low:
                kind = "MSS" if trend == "bullish" else "BOS"
                events.append(StructureEvent(kind, low, i, "bearish")); trend = "bearish"
                candle = data.iloc[max(0, i - 1)]
                blocks.append(PriceZone("OB", float(candle["low"]), float(candle["high"]), i - 1, "bearish"))
            last_low = low
    # Telegram ekranında okunabilirlik için yalnızca en güncel yapılar çizilir.
    return SmartMoneyResult(tuple(fvg[-3:]), tuple(blocks[-3:]), tuple(events[-5:]))
