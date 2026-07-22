from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.data.base_provider import BaseMarketDataProvider, DataFreshness, DataUnavailableError
from app.data.reliable_provider import (
    CircuitBreaker,
    CircuitState,
    FileDataCache,
    ReliableMarketDataProvider,
    RetryPolicy,
)
from app.data.yahoo_chart_provider import YahooChartMarketDataProvider


def _df(periods=80):
    dates = pd.bdate_range(end=datetime.now(timezone.utc) - timedelta(days=1), periods=periods)
    close = np.linspace(10, 12, periods)
    return pd.DataFrame(
        {"timestamp": dates, "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000.0}
    )


class _Provider(BaseMarketDataProvider):
    def __init__(self, name, data=None, error=None):
        self.name = name
        self.data = data
        self.error = error
        self.calls = 0

    def get_ohlcv(self, symbol, timeframe, start, end):
        self.calls += 1
        if self.error:
            raise self.error
        return self.data.copy()

    def get_quote(self, symbol):
        raise NotImplementedError

    def get_index_data(self, index_symbol, timeframe):
        return self.data.copy()

    def is_market_open(self):
        return False

    def get_data_freshness(self, symbol, timeframe):
        return DataFreshness(symbol, timeframe, None, False, 0, self.name)

    def health_check(self):
        return {"provider": self.name, "status": "ok" if not self.error else "down"}


def test_retry_exponential_backoff_then_success():
    calls = {"n": 0}
    sleeps = []

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("temporary timeout")
        return _df()

    result = RetryPolicy(max_attempts=3, base_delay_seconds=0.25).execute(flaky, sleep=sleeps.append)
    assert len(result) == 80
    assert sleeps == [0.25, 0.5]


def test_retry_does_not_repeat_permanent_error():
    calls = {"n": 0}

    def invalid():
        calls["n"] += 1
        raise ValueError("unsupported symbol")

    with pytest.raises(DataUnavailableError):
        RetryPolicy(max_attempts=5).execute(invalid, sleep=lambda _: None)
    assert calls["n"] == 1


def test_rate_limit_gets_longer_backoff():
    sleeps = []
    calls = {"n": 0}

    def rate_limited():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("HTTP 429 rate limit")
        return _df()

    RetryPolicy(max_attempts=2, base_delay_seconds=1).execute(rate_limited, sleep=sleeps.append)
    assert sleeps == [2]


def test_circuit_breaker_opens_after_threshold():
    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=60)
    breaker.record_failure(RuntimeError("one"))
    assert breaker.state == CircuitState.CLOSED
    breaker.record_failure(RuntimeError("two"))
    assert breaker.state == CircuitState.OPEN
    assert not breaker.allow_request()


def test_circuit_breaker_half_open_and_recovers():
    now = datetime.now(timezone.utc)
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=10)
    breaker.record_failure(RuntimeError("x"), now=now)
    assert breaker.allow_request(now + timedelta(seconds=11))
    assert breaker.state == CircuitState.HALF_OPEN
    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED


def test_file_cache_roundtrip(tmp_path):
    cache = FileDataCache(tmp_path)
    cache.save("SVGYO", "1d", _df(), "primary")
    record = cache.load("SVGYO", "1d", max_age_minutes=10)
    assert record is not None
    assert record.provider == "primary"
    assert len(record.df) == 80


def test_file_cache_rejects_too_old_data(tmp_path):
    cache = FileDataCache(tmp_path)
    cache.save("SVGYO", "1d", _df(), "primary")
    _, meta_path = cache._paths("SVGYO", "1d")
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    assert cache.load("SVGYO", "1d", max_age_minutes=30) is None
    assert cache.rejected_stale == 1


def test_reliable_provider_uses_secondary_and_reports_source(tmp_path):
    primary = _Provider("primary", error=ValueError("permanent"))
    fallback = _Provider("secondary", data=_df())
    provider = ReliableMarketDataProvider(
        primary, fallback, FileDataCache(tmp_path), retry_policy=RetryPolicy(max_attempts=1)
    )
    end = datetime.now(timezone.utc)
    result = provider.get_ohlcv("SVGYO", "1d", end - timedelta(days=500), end)
    assert not result.empty
    meta = provider.metadata_for("SVGYO", "1d")
    assert meta["provider"] == "secondary"
    assert meta["fallback_used"] is True


def test_reliable_provider_uses_fresh_cache_when_both_down(tmp_path):
    cache = FileDataCache(tmp_path)
    cache.save("SVGYO", "1d", _df(), "last_good")
    provider = ReliableMarketDataProvider(
        _Provider("primary", error=ValueError("down")),
        _Provider("secondary", error=ValueError("down")),
        cache,
        retry_policy=RetryPolicy(max_attempts=1),
    )
    end = datetime.now(timezone.utc)
    result = provider.get_ohlcv("SVGYO", "1d", end - timedelta(days=500), end)
    assert not result.empty
    meta = provider.metadata_for("SVGYO", "1d")
    assert meta["cache_used"] is True
    assert meta["provider"] == "last_good"


def test_reliable_provider_rejects_stale_cache_when_both_down(tmp_path):
    cache = FileDataCache(tmp_path)
    cache.save("SVGYO", "1d", _df(), "last_good")
    _, meta_path = cache._paths("SVGYO", "1d")
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["fetched_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    provider = ReliableMarketDataProvider(
        _Provider("primary", error=ValueError("down")),
        _Provider("secondary", error=ValueError("down")),
        cache,
        retry_policy=RetryPolicy(max_attempts=1),
        cache_max_age_daily_minutes=30,
    )
    end = datetime.now(timezone.utc)
    with pytest.raises(DataUnavailableError):
        provider.get_ohlcv("SVGYO", "1d", end - timedelta(days=500), end)


def test_reliable_provider_never_uses_mock_implicitly(tmp_path):
    provider = ReliableMarketDataProvider(
        _Provider("primary", error=ValueError("down")),
        None,
        FileDataCache(tmp_path),
        retry_policy=RetryPolicy(max_attempts=1),
    )
    end = datetime.now(timezone.utc)
    with pytest.raises(DataUnavailableError) as exc:
        provider.get_ohlcv("SVGYO", "1d", end - timedelta(days=500), end)
    assert "mock" not in str(exc.value).lower()


def test_yahoo_chart_adapter_parses_adjusted_ohlcv():
    timestamps = [int((datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)).timestamp()) for i in range(3)]
    payload = {
        "chart": {
            "error": None,
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [{"open": [10, 11, 12], "high": [11, 12, 13], "low": [9, 10, 11], "close": [10, 11, 12], "volume": [100, 110, 120]}],
                        "adjclose": [{"adjclose": [5, 5.5, 6]}],
                    },
                    "events": {"splits": {"x": {"date": timestamps[1], "splitRatio": "2:1"}}},
                }
            ],
        }
    }
    provider = YahooChartMarketDataProvider(price_mode="adjusted")
    df = provider._parse(payload, "SVGYO")
    assert df.iloc[0]["close"] == 5
    assert df.iloc[0]["high"] == 5.5
    assert provider.get_corporate_actions("SVGYO")[0]["type"] == "split"
