from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from app.data.base_provider import BaseMarketDataProvider, DataFreshness, DataUnavailableError
from app.services.alert_service import evaluate_alert
from app.services.current_price_service import CurrentPriceResolver, FALLBACK_WARNING
from app.services.portfolio_service import portfolio_summary


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


def _bars(closes, timeframe="1d", end=NOW - timedelta(days=1)):
    freq = {"1d": "1D", "5m": "5min", "15m": "15min", "1h": "1h"}[timeframe]
    idx = pd.date_range(end=end, periods=len(closes), freq=freq, tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": closes,
            "high": [x + 0.2 for x in closes],
            "low": [x - 0.2 for x in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        }
    )


class PriceProvider(BaseMarketDataProvider):
    name = "fake"

    def __init__(self, data=None, snapshot=None, quote=None):
        self.data = data or {}
        self.snapshot = snapshot
        self.quote = quote

    def get_latest_intraday_snapshot(self, symbol):
        if self.snapshot is None:
            raise DataUnavailableError("snapshot yok")
        return self.snapshot

    def get_quote(self, symbol):
        if self.quote is None:
            raise DataUnavailableError("quote yok")
        return self.quote

    def get_ohlcv(self, symbol, timeframe, start, end):
        if timeframe not in self.data:
            raise DataUnavailableError(f"{timeframe} yok")
        return self.data[timeframe].copy()

    def get_index_data(self, index_symbol, timeframe):
        return self.get_ohlcv(index_symbol, timeframe, NOW - timedelta(days=10), NOW)

    def is_market_open(self):
        return True

    def get_data_freshness(self, symbol, timeframe):
        return DataFreshness(symbol, timeframe, NOW, True, 30, self.name)

    def health_check(self):
        return {"status": "ok"}


def test_intraday_snapshot_is_the_primary_current_price():
    daily = _bars([12.5, 13.0])
    provider = PriceProvider(
        {"1d": daily, "5m": _bars([12.6], "5m", NOW - timedelta(minutes=10))},
        snapshot={"available": True, "last_price": 12.72, "timestamp": NOW, "provider": "snapshot"},
    )
    result = CurrentPriceResolver(provider).resolve("SVGYO", now=NOW)
    assert result.current_price == 12.72
    assert result.analysis_close == 13.0
    assert result.current_price_source == "snapshot"
    assert result.is_live_price is True


def test_completed_5m_has_priority_over_15m_and_quote():
    provider = PriceProvider(
        {
            "1d": _bars([12.5, 13.0]),
            "5m": _bars([12.7], "5m", NOW - timedelta(minutes=10)),
            "15m": _bars([12.8], "15m", NOW - timedelta(minutes=30)),
        },
        quote={"price": 12.9, "timestamp": NOW},
    )
    result = CurrentPriceResolver(provider).resolve("SVGYO", now=NOW)
    assert result.current_price == 12.7
    assert result.current_price_source == "completed_5m"


def test_daily_change_uses_last_confirmed_close_for_live_price():
    provider = PriceProvider(
        {"1d": _bars([12.5, 13.0])},
        snapshot={"available": True, "last_price": 12.74, "timestamp": NOW},
    )
    result = CurrentPriceResolver(provider).resolve("SVGYO", now=NOW)
    assert result.daily_change_percent == -2.0


def test_daily_close_fallback_is_explicit():
    result = CurrentPriceResolver(PriceProvider({"1d": _bars([12.5, 13.0])})).resolve(
        "SVGYO", now=NOW
    )
    assert result.current_price == 13.0
    assert result.is_live_price is False
    assert result.fallback_used is True
    assert result.warning == FALLBACK_WARNING


def test_incomplete_intraday_bar_is_not_used():
    provider = PriceProvider(
        {
            "1d": _bars([12.5, 13.0]),
            "5m": _bars([12.60, 99.0], "5m", NOW - timedelta(minutes=2)),
            "15m": _bars([12.65], "15m", NOW - timedelta(minutes=20)),
        }
    )
    result = CurrentPriceResolver(provider).resolve("SVGYO", now=NOW)
    assert result.current_price == 12.6
    assert result.current_price != 99.0


def test_quote_is_used_after_all_intraday_candidates_fail():
    provider = PriceProvider(
        {"1d": _bars([12.5, 13.0])},
        quote={"price": 12.71, "timestamp": NOW, "provider": "quote-feed"},
    )
    result = CurrentPriceResolver(provider).resolve("SVGYO", now=NOW)
    assert result.current_price == 12.71
    assert result.current_price_source == "provider_quote:quote-feed"
