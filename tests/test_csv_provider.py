from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.data.base_provider import DataUnavailableError
from app.data.csv_provider import CsvMarketDataProvider


def test_csv_provider_loads_sample_data():
    provider = CsvMarketDataProvider(csv_data_dir="data_csv")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    df = provider.get_ohlcv("THYAO", "1d", start, end)
    assert len(df) > 50
    assert {"open", "high", "low", "close", "volume"}.issubset(df.columns)


def test_csv_provider_missing_symbol_raises():
    provider = CsvMarketDataProvider(csv_data_dir="data_csv")
    with pytest.raises(DataUnavailableError):
        provider.get_quote("NOSUCHSYMBOL")


def test_csv_provider_rejects_intraday_timeframe():
    provider = CsvMarketDataProvider(csv_data_dir="data_csv")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=10)
    with pytest.raises(DataUnavailableError):
        provider.get_ohlcv("THYAO", "15m", start, end)
