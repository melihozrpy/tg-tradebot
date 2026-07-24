from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from app.services.multi_timeframe_explanation_service import (
    build_multi_timeframe_explanation, build_multi_timeframe_package,
    format_multi_timeframe_explanation,
)


class FakeProvider:
    def get_ohlcv(self, symbol, interval, start, end):
        if interval == "1h":
            stamps = []
            day = datetime(2026, 3, 2, 7, tzinfo=timezone.utc)
            for offset in range(60):
                current = day + timedelta(days=offset)
                if current.weekday() < 5:
                    stamps.extend(current + timedelta(hours=hour) for hour in range(8))
            index = pd.DatetimeIndex(stamps)
        else:
            index = pd.date_range("2026-03-01", periods=240, freq="5min" if interval == "5m" else "15min", tz="UTC")
        close = np.linspace(100, 125, len(index)) + np.sin(np.arange(len(index)) / 4)
        return pd.DataFrame({"timestamp": index, "open": close - .2, "high": close + .8,
                             "low": close - .8, "close": close, "volume": 1000})


def test_multi_timeframe_summary_explains_all_requested_periods():
    now = datetime(2026, 5, 15, 20, tzinfo=timezone.utc)
    result = build_multi_timeframe_explanation(FakeProvider(), "THYAO", now=now)
    text = format_multi_timeframe_explanation("THYAO", result)
    assert not result[1]
    for label in ("5 dk", "15 dk", "1 saat", "4 saat"):
        assert label in text
    for concept in ("MACD", "ATR", "FVG", "Order Block", "BOS", "MSS"):
        assert concept in text


def test_montana_brand_and_buy_sell_chart_labels_are_present():
    handlers = open("app/telegram/handlers_v3.py", encoding="utf-8").read()
    chart = open("app/services/chart_service.py", encoding="utf-8").read()
    assert "MONTANA MELİH HİSSE BOT" in handlers
    assert 'f"AL  {buy_price:.2f} TL"' in chart
    assert 'f"SAT  {sell_price:.2f} TL"' in chart


def test_four_panel_multi_timeframe_chart_is_generated():
    from app.services.chart_service import delete_chart_file, generate_multi_timeframe_chart
    result, frames = build_multi_timeframe_package(
        FakeProvider(), "THYAO", now=datetime(2026, 5, 15, 20, tzinfo=timezone.utc),
    )
    path = generate_multi_timeframe_chart(frames, "THYAO")
    try:
        assert len(result[0]) == 4
        assert __import__("os").path.getsize(path) > 20_000
    finally:
        delete_chart_file(path)
