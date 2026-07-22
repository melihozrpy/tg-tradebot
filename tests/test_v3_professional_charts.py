from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from app.analysis.timeframe_levels_engine import compute_timeframe_levels
from app.services.chart_service import (
    delete_chart_file,
    generate_intraday_chart,
    generate_professional_daily_chart,
)


def test_professional_daily_chart_creates_file(mock_provider):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)
    df = mock_provider.get_ohlcv("THYAO", "1d", start, end)
    close = df["close"].iloc[-1]
    levels = compute_timeframe_levels(df, close)

    path = generate_professional_daily_chart(
        df, "THYAO",
        info_box={"Son fiyat": close, "Nihai karar": "ALIM ADAYI"},
        timeframe_levels=levels,
        entry_zone=(close * 0.98, close * 1.0),
        entry_trigger=close * 1.01,
        stop_price=close * 0.93,
        targets=[close * 1.05, close * 1.1, close * 1.15],
    )
    try:
        assert os.path.exists(path)
        assert path.endswith(".png")
    finally:
        delete_chart_file(path)


def test_professional_chart_survives_missing_optional_context(mock_provider):
    """Ek katmanlar (timeframe_levels/confluence/senaryo/anomali) verilmezse
    de grafik uretimi cokmemeli."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=60)
    df = mock_provider.get_ohlcv("THYAO", "1d", start, end)
    path = generate_professional_daily_chart(df, "THYAO")
    try:
        assert os.path.exists(path)
    finally:
        delete_chart_file(path)


def test_intraday_chart_creates_file(mock_provider):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=5)
    df = mock_provider.get_ohlcv("THYAO", "15m", start, end)
    path = generate_intraday_chart(df, "THYAO", daily_support=10.0, daily_resistance=12.0)
    try:
        assert os.path.exists(path)
        assert path.endswith(".png")
    finally:
        delete_chart_file(path)
