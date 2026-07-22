from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.data.base_provider import DataUnavailableError
from app.data.yfinance_provider import YFinanceMarketDataProvider, normalize_bist_symbol


def _fake_history_df(periods: int = 260, base_price: float = 100.0) -> pd.DataFrame:
    """yfinance.Ticker.history()'nin dondurdugu forma benzer sahte bir DataFrame uretir.

    Bu yalnizca TEST amaclidir: gercek ag erisimi olmadan saglayicinin
    normalizasyon/hata yonetimi mantigini dogrulamak icin kullanilir.
    Provider'in kendisi hicbir zaman gercek veri yerine bunu kullanmaz.
    """
    import numpy as np

    rng = np.random.default_rng(7)
    dates = pd.bdate_range(end=datetime.now(timezone.utc), periods=periods)
    returns = rng.normal(0.0003, 0.015, size=periods)
    closes = base_price * np.cumprod(1 + returns)
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    highs = closes * (1 + np.abs(rng.normal(0, 0.005, size=periods)))
    lows = closes * (1 - np.abs(rng.normal(0, 0.005, size=periods)))
    volumes = np.abs(rng.normal(1_000_000, 200_000, size=periods))

    df = pd.DataFrame(
        {
            "Open": opens,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": volumes,
        },
        index=pd.DatetimeIndex(dates, name="Date"),
    )
    return df


# ---------------------------------------------------------------------------
# .IS sembol donusumu testleri
# ---------------------------------------------------------------------------


def test_normalize_adds_is_suffix():
    assert normalize_bist_symbol("SVGYO") == "SVGYO.IS"


def test_normalize_does_not_duplicate_is_suffix():
    assert normalize_bist_symbol("SVGYO.IS") == "SVGYO.IS"


def test_normalize_lowercase_input():
    assert normalize_bist_symbol("svgyo") == "SVGYO.IS"


def test_normalize_index_symbol_special_case():
    assert normalize_bist_symbol("XU100") == "^XU100"


# ---------------------------------------------------------------------------
# Veri cekme / hata yonetimi testleri
# ---------------------------------------------------------------------------


def test_yfinance_provider_returns_normalized_ohlcv(monkeypatch):
    provider = YFinanceMarketDataProvider(max_retries=1)

    def fake_fetch(self, yf_symbol, start, end):
        assert yf_symbol == "SVGYO.IS"
        return _fake_history_df(260)

    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw", fake_fetch)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    df = provider.get_ohlcv("SVGYO", "1d", start, end)

    assert len(df) >= 250
    assert {"timestamp", "open", "high", "low", "close", "volume"}.issubset(df.columns)


def test_yfinance_provider_never_falls_back_to_mock_on_empty_result(monkeypatch):
    provider = YFinanceMarketDataProvider(max_retries=1)

    def fake_fetch_empty(self, yf_symbol, start, end):
        return pd.DataFrame()

    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw", fake_fetch_empty)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    with pytest.raises(DataUnavailableError):
        provider.get_ohlcv("NOSUCHSYM", "1d", start, end)


def test_yfinance_provider_retries_on_transient_error_then_succeeds(monkeypatch):
    provider = YFinanceMarketDataProvider(max_retries=3, backoff_base_seconds=0.01)
    call_count = {"n": 0}

    def flaky_fetch(self, yf_symbol, start, end):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise TimeoutError("gecici ag hatasi")
        return _fake_history_df(260)

    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw", flaky_fetch)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    df = provider.get_ohlcv("SVGYO", "1d", start, end)
    assert call_count["n"] == 3
    assert len(df) > 0


def test_yfinance_provider_raises_after_exhausting_retries(monkeypatch):
    provider = YFinanceMarketDataProvider(max_retries=2, backoff_base_seconds=0.01)

    def always_fails(self, yf_symbol, start, end):
        raise TimeoutError("surekli ag hatasi")

    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw", always_fails)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    with pytest.raises(DataUnavailableError):
        provider.get_ohlcv("SVGYO", "1d", start, end)


def test_yfinance_provider_accepts_intraday_timeframe_with_mocked_fetch(monkeypatch):
    """V3.1: 15m artik desteklenen bir zaman dilimi (bolum 2); gercek ag
    cagrisi yapilmadan (monkeypatch ile) dogru sekilde calismali."""
    provider = YFinanceMarketDataProvider(max_retries=1, request_delay_seconds=0)

    def fake_intraday_fetch(self, yf_symbol, interval, period):
        assert yf_symbol == "SVGYO.IS"
        assert interval == "15m"
        return _fake_history_df(200)

    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw_intraday", fake_intraday_fetch)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=10)
    df = provider.get_ohlcv("SVGYO", "15m", start, end)
    assert not df.empty
    assert {"timestamp", "open", "high", "low", "close", "volume"}.issubset(df.columns)


def test_yfinance_provider_rejects_unsupported_timeframe():
    """Gercekten desteklenmeyen bir zaman dilimi (orn. '1m') reddedilmeli."""
    provider = YFinanceMarketDataProvider(max_retries=1, request_delay_seconds=0)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=10)
    with pytest.raises(DataUnavailableError):
        provider.get_ohlcv("SVGYO", "1m", start, end)


def test_yfinance_provider_health_check_reports_down_on_failure(monkeypatch):
    provider = YFinanceMarketDataProvider(max_retries=1)

    def always_fails(self, yf_symbol, start, end):
        raise TimeoutError("baglanti yok")

    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw", always_fails)
    health = provider.health_check()
    assert health["status"] in ("degraded", "down")
