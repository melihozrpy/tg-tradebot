from __future__ import annotations

import pandas as pd
import pytest

from app.data.base_provider import DataUnavailableError
from app.data.mock_provider import MockMarketDataProvider
from app.services.intraday_service import (
    INTRADAY_PREVIEW_STATE,
    IntradayAnalysisUnavailableError,
    run_intraday_preview,
)


def test_intraday_preview_returns_preview_state(mock_provider):
    result = run_intraday_preview(mock_provider, "SVGYO")
    assert result.state == INTRADAY_PREVIEW_STATE
    assert result.last_price is not None
    assert result.classification in (
        "Gün içi alım adayı",
        "Tetik bekleniyor",
        "Gün içi risk arttı",
        "Nötr",
        "Veri yetersiz",
    )


def test_intraday_preview_includes_warning_about_delay(mock_provider):
    result = run_intraday_preview(mock_provider, "THYAO")
    assert any("gecikmeli" in w for w in result.warnings)


def test_intraday_preview_raises_when_no_data(monkeypatch, mock_provider):
    def fake_get_ohlcv(self, symbol, timeframe, start, end):
        raise DataUnavailableError("veri yok")

    monkeypatch.setattr(MockMarketDataProvider, "get_ohlcv", fake_get_ohlcv)
    with pytest.raises(IntradayAnalysisUnavailableError):
        run_intraday_preview(mock_provider, "SVGYO")


def test_intraday_preview_raises_on_bad_quality_data(monkeypatch, mock_provider):
    def fake_bad_ohlcv(self, symbol, timeframe, start, end):
        # High < Low gibi bozuk veri -> kalite kontrolunden gecmemeli.
        return pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=30, freq="15min", tz="UTC"),
            "open": [10.0] * 30,
            "high": [5.0] * 30,  # bilerek high < low
            "low": [9.0] * 30,
            "close": [10.0] * 30,
            "volume": [1000.0] * 30,
        })

    monkeypatch.setattr(MockMarketDataProvider, "get_ohlcv", fake_bad_ohlcv)
    with pytest.raises(IntradayAnalysisUnavailableError):
        run_intraday_preview(mock_provider, "SVGYO")


def test_intraday_preview_never_saved_as_confirmed_signal(mock_provider):
    """Gun ici on analiz her zaman PREVIEW olmali, asla CONFIRMED/kesinlesmis degil."""
    result = run_intraday_preview(mock_provider, "ASELS")
    assert result.state == "PREVIEW"
    assert result.state != "CONFIRMED"
