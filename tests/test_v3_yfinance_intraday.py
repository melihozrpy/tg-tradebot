from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.data.base_provider import DataUnavailableError
from app.data.yfinance_provider import YFinanceMarketDataProvider


def _fake_history_df(periods: int = 200, base_price: float = 50.0, freq: str = "15min") -> pd.DataFrame:
    rng = np.random.default_rng(11)
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=periods, freq=freq)
    returns = rng.normal(0.0002, 0.01, size=periods)
    closes = base_price * np.cumprod(1 + returns)
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    highs = closes * (1 + np.abs(rng.normal(0, 0.004, size=periods)))
    lows = closes * (1 - np.abs(rng.normal(0, 0.004, size=periods)))
    volumes = np.abs(rng.normal(500_000, 100_000, size=periods))
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.DatetimeIndex(dates, name="Datetime"),
    )


def _fake_daily_df(periods: int = 20, base_price: float = 50.0) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    dates = pd.bdate_range(end=datetime.now(timezone.utc), periods=periods)
    returns = rng.normal(0.0003, 0.012, size=periods)
    closes = base_price * np.cumprod(1 + returns)
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    highs = closes * 1.01
    lows = closes * 0.99
    volumes = np.abs(rng.normal(1_000_000, 200_000, size=periods))
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": volumes},
        index=pd.DatetimeIndex(dates, name="Date"),
    )


# ---------------------------------------------------------------------------
# get_intraday_ohlcv / get_daily_ohlcv
# ---------------------------------------------------------------------------


def test_get_intraday_ohlcv_returns_normalized_frame(monkeypatch):
    provider = YFinanceMarketDataProvider(max_retries=1, request_delay_seconds=0)

    def fake_fetch(self, yf_symbol, interval, period):
        assert interval == "15m"
        return _fake_history_df(150)

    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw_intraday", fake_fetch)
    df = provider.get_intraday_ohlcv("SVGYO", interval="15m")
    assert not df.empty
    assert df["timestamp"].is_monotonic_increasing


def test_get_intraday_ohlcv_rejects_non_intraday_interval():
    provider = YFinanceMarketDataProvider(max_retries=1, request_delay_seconds=0)
    with pytest.raises(DataUnavailableError):
        provider.get_intraday_ohlcv("SVGYO", interval="1d")


def test_get_intraday_ohlcv_clamps_period_to_yfinance_limit(monkeypatch):
    provider = YFinanceMarketDataProvider(max_retries=1, request_delay_seconds=0)
    captured = {}

    def fake_fetch(self, yf_symbol, interval, period):
        captured["period"] = period
        return _fake_history_df(150)

    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw_intraday", fake_fetch)
    # 5m icin yfinance siniri 60 gun; 400 gun istense bile 60d'ye kisaltilmali.
    provider.get_intraday_ohlcv("SVGYO", interval="5m", period="400d")
    assert captured["period"] == "60d"


def test_get_daily_ohlcv_uses_period_string(monkeypatch):
    provider = YFinanceMarketDataProvider(max_retries=1, request_delay_seconds=0)

    def fake_fetch(self, yf_symbol, start, end):
        return _fake_daily_df(260)

    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw", fake_fetch)
    df = provider.get_daily_ohlcv("SVGYO", period="1y")
    assert not df.empty


# ---------------------------------------------------------------------------
# Haftalik (1wk)
# ---------------------------------------------------------------------------


def test_weekly_timeframe_uses_dedicated_fetcher(monkeypatch):
    provider = YFinanceMarketDataProvider(max_retries=1, request_delay_seconds=0)
    called = {"n": 0}

    def fake_weekly(self, yf_symbol, start, end):
        called["n"] += 1
        return _fake_daily_df(60, base_price=80.0)

    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw_weekly", fake_weekly)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)
    df = provider.get_ohlcv("SVGYO", "1wk", start, end)
    assert called["n"] == 1
    assert not df.empty


# ---------------------------------------------------------------------------
# get_multi_timeframe_data
# ---------------------------------------------------------------------------


def test_multi_timeframe_data_partial_failure_does_not_break_others(monkeypatch):
    provider = YFinanceMarketDataProvider(max_retries=1, request_delay_seconds=0)

    def fake_daily(self, yf_symbol, start, end):
        return _fake_daily_df(260)

    def fake_weekly(self, yf_symbol, start, end):
        return _fake_daily_df(60, base_price=80.0)

    def fake_intraday(self, yf_symbol, interval, period):
        if interval == "5m":
            raise TimeoutError("gecici hata")
        return _fake_history_df(150, freq="15min" if interval == "15m" else "1h")

    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw", fake_daily)
    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw_weekly", fake_weekly)
    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw_intraday", fake_intraday)

    result = provider.get_multi_timeframe_data("SVGYO")
    assert set(result.keys()) == {"5m", "15m", "1h", "1d", "1wk"}
    assert result["5m"]["available"] is False
    assert result["5m"]["data"] is None
    assert result["15m"]["available"] is True
    assert result["1d"]["available"] is True
    assert result["1wk"]["available"] is True


# ---------------------------------------------------------------------------
# get_latest_intraday_snapshot
# ---------------------------------------------------------------------------


def test_latest_intraday_snapshot_computes_change_percent(monkeypatch):
    provider = YFinanceMarketDataProvider(max_retries=1, request_delay_seconds=0)

    def fake_intraday(self, yf_symbol, interval, period):
        return _fake_history_df(100, base_price=50.0)

    def fake_daily(self, yf_symbol, start, end):
        return _fake_daily_df(20, base_price=48.0)

    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw_intraday", fake_intraday)
    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw", fake_daily)

    snapshot = provider.get_latest_intraday_snapshot("SVGYO")
    assert snapshot["available"] is True
    assert snapshot["last_price"] is not None
    assert "daily_change_percent" in snapshot


def test_latest_intraday_snapshot_reports_unavailable_when_no_intraday_data(monkeypatch):
    provider = YFinanceMarketDataProvider(max_retries=1, request_delay_seconds=0)

    def fake_intraday_empty(self, yf_symbol, interval, period):
        return pd.DataFrame()

    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw_intraday", fake_intraday_empty)
    snapshot = provider.get_latest_intraday_snapshot("SVGYO")
    assert snapshot["available"] is False
    assert "Gün içi veri alınamadı" in snapshot["detail"]


# ---------------------------------------------------------------------------
# validate_bar_completion
# ---------------------------------------------------------------------------


def test_validate_bar_completion_detects_incomplete_bar():
    provider = YFinanceMarketDataProvider(max_retries=1, request_delay_seconds=0)
    # Son bar 1 dakika once basladi; 15 dakikalik bar henuz tamamlanmamis olmali.
    now = datetime.now(timezone.utc)
    df = pd.DataFrame({
        "timestamp": [now - timedelta(minutes=1)],
        "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2], "volume": [1000.0],
    })
    result = provider.validate_bar_completion(df, "15m")
    assert result["is_complete"] is False


def test_validate_bar_completion_detects_complete_bar():
    provider = YFinanceMarketDataProvider(max_retries=1, request_delay_seconds=0)
    now = datetime.now(timezone.utc)
    df = pd.DataFrame({
        "timestamp": [now - timedelta(hours=2)],
        "open": [10.0], "high": [10.5], "low": [9.8], "close": [10.2], "volume": [1000.0],
    })
    result = provider.validate_bar_completion(df, "15m")
    assert result["is_complete"] is True


def test_validate_bar_completion_empty_df():
    provider = YFinanceMarketDataProvider(max_retries=1, request_delay_seconds=0)
    result = provider.validate_bar_completion(pd.DataFrame(), "15m")
    assert result["is_complete"] is False


# ---------------------------------------------------------------------------
# health_check / get_data_freshness (timeframe-aware)
# ---------------------------------------------------------------------------


def test_health_check_reports_ok_with_mocked_daily(monkeypatch):
    provider = YFinanceMarketDataProvider(max_retries=1, request_delay_seconds=0)

    def fake_daily(self, yf_symbol, start, end):
        return _fake_daily_df(260)

    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw", fake_daily)
    health = provider.health_check()
    assert health["status"] == "ok"


def test_data_freshness_is_timeframe_aware(monkeypatch):
    provider = YFinanceMarketDataProvider(max_retries=1, request_delay_seconds=0)

    def fake_intraday(self, yf_symbol, interval, period):
        return _fake_history_df(50)

    monkeypatch.setattr(YFinanceMarketDataProvider, "_fetch_history_raw_intraday", fake_intraday)
    freshness = provider.get_data_freshness("SVGYO", "15m")
    assert freshness.timeframe == "15m"
    assert freshness.max_allowed_lag_minutes == 35
