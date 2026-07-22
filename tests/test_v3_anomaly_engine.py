from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analysis.anomaly_engine import (
    ANOMALY_GAP_UP,
    ANOMALY_VOLATILITY_SPIKE,
    ANOMALY_VOLUME_SPIKE,
    detect_anomalies,
)
from app.data.base_provider import DataUnavailableError
from app.data.mock_provider import MockMarketDataProvider
from app.services.anomaly_service import (
    AnomalyDetectionUnavailableError,
    list_recent_anomalies,
    run_symbol_anomaly_scan,
)


def _synthetic_df_with_spike(n: int = 40) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=n, freq="B", tz="UTC")
    close = np.linspace(100, 110, n)
    df = pd.DataFrame({
        "timestamp": dates,
        "open": close - 0.2,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": [100000.0] * n,
    })
    df.loc[n - 1, "volume"] = 500000.0
    df.loc[n - 1, "open"] = df.loc[n - 2, "close"] * 1.05
    df.loc[n - 1, "high"] = df.loc[n - 1, "open"] + 1
    df.loc[n - 1, "low"] = df.loc[n - 1, "open"] - 0.5
    df.loc[n - 1, "close"] = df.loc[n - 1, "open"] + 0.5
    return df


def _flat_df(n: int = 40) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=n, freq="B", tz="UTC")
    close = np.full(n, 50.0)
    return pd.DataFrame({
        "timestamp": dates, "open": close, "high": close + 0.1, "low": close - 0.1,
        "close": close, "volume": [10000.0] * n,
    })


def test_detect_anomalies_requires_minimum_bars():
    result = detect_anomalies(pd.DataFrame(), "TEST")
    assert result.available is False
    assert result.events == []


def test_detect_anomalies_finds_volume_and_gap_and_volatility():
    df = _synthetic_df_with_spike()
    result = detect_anomalies(df, "TEST")
    assert result.available is True
    types = {e.anomaly_type for e in result.events}
    assert ANOMALY_VOLUME_SPIKE in types
    assert ANOMALY_GAP_UP in types
    assert ANOMALY_VOLATILITY_SPIKE in types


def test_detect_anomalies_quiet_market_has_no_events():
    df = _flat_df()
    result = detect_anomalies(df, "TEST")
    assert result.available is True
    assert result.events == []
    assert "tespit edilmedi" in result.note


def test_run_symbol_anomaly_scan_persists_and_deduplicates(db_session, monkeypatch):
    provider = MockMarketDataProvider()

    def fake_get_ohlcv(self, symbol, timeframe, start, end):
        return _synthetic_df_with_spike()

    monkeypatch.setattr(MockMarketDataProvider, "get_ohlcv", fake_get_ohlcv)

    outcome1 = run_symbol_anomaly_scan(db_session, provider, "THYAO")
    assert outcome1.result.available is True
    assert len(outcome1.new_anomalies) >= 1

    # Ayni anomaliler kisa surede tekrar taranirsa TEKRAR kaydedilmemeli.
    outcome2 = run_symbol_anomaly_scan(db_session, provider, "THYAO")
    assert len(outcome2.new_anomalies) == 0

    recent = list_recent_anomalies(db_session, symbols=["THYAO"])
    assert len(recent) >= 1


def test_run_symbol_anomaly_scan_raises_when_no_data(db_session, monkeypatch):
    provider = MockMarketDataProvider()

    def fake_get_ohlcv(self, symbol, timeframe, start, end):
        raise DataUnavailableError("veri yok")

    monkeypatch.setattr(MockMarketDataProvider, "get_ohlcv", fake_get_ohlcv)

    with pytest.raises(AnomalyDetectionUnavailableError):
        run_symbol_anomaly_scan(db_session, provider, "THYAO")
