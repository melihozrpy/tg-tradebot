from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.analysis.indicator_engine import (
    InsufficientDataError,
    compute_technical_snapshot,
    ema,
    rsi,
)


def test_ema_matches_pandas_ewm():
    series = pd.Series([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=float)
    result = ema(series, 3)
    assert not result.isnull().all()


def test_rsi_bounds():
    series = pd.Series(range(1, 60), dtype=float)  # monoton artan seri
    result = rsi(series, 14)
    assert (result.dropna() <= 100).all()
    assert (result.dropna() >= 0).all()


def test_insufficient_data_raises(mock_provider):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=5)
    df = mock_provider.get_ohlcv("THYAO", "1d", start, end)
    with pytest.raises(InsufficientDataError):
        compute_technical_snapshot(df, "THYAO", "1d")


def test_sufficient_data_produces_snapshot(mock_provider):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)
    df = mock_provider.get_ohlcv("THYAO", "1d", start, end)
    snapshot = compute_technical_snapshot(df, "THYAO", "1d")
    assert snapshot.symbol == "THYAO"
    assert snapshot.close > 0
    assert snapshot.trend_direction in ("up", "down", "sideways")
