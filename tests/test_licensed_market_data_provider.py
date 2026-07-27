from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.data.base_provider import DataUnavailableError
from app.data.licensed_rest_provider import LicensedRestMarketDataProvider
from app.data.provider_factory import build_market_data_provider


def _provider(handler):
    return LicensedRestMarketDataProvider(
        base_url="https://feed.example/v1",
        api_key="secret",
        provider_name="licensed-bist-test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_licensed_provider_requires_explicit_live_timestamp_and_api_key_header():
    now = datetime.now(timezone.utc)
    observed = {}

    def handler(request):
        observed["key"] = request.headers["x-api-key"]
        return httpx.Response(200, json={
            "symbol": "THYAO", "price": 301.25, "timestamp": now.isoformat(),
            "is_live": True, "market_open": True, "valid_transaction": True, "trade_id": "T-1",
        })

    provider = _provider(handler)
    quote = provider.get_quote("THYAO.IS")
    provider._client.close()
    assert observed["key"] == "secret"
    assert quote["is_fresh"] is True
    assert quote["provider"] == "licensed-bist-test"


def test_licensed_provider_rejects_quote_without_timezone():
    provider = _provider(lambda _request: httpx.Response(200, json={
        "symbol": "THYAO", "price": 301.25, "timestamp": "2026-07-24 12:00:00", "is_live": True,
        "valid_transaction": True,
    }))
    with pytest.raises(DataUnavailableError, match="timestamp"):
        provider.get_quote("THYAO")
    provider._client.close()


def test_licensed_provider_validates_ohlcv_and_keeps_completion_flag():
    now = datetime.now(timezone.utc)

    def handler(request):
        assert request.url.params["timeframe"] == "5m"
        return httpx.Response(200, json={"bars": [{
            "timestamp": (now - timedelta(minutes=5)).isoformat(),
            "open": 100, "high": 102, "low": 99, "close": 101,
            "volume": 5000, "is_complete": True,
        }]})

    provider = _provider(handler)
    frame = provider.get_ohlcv("ASELS", "5m", now - timedelta(days=1), now)
    provider._client.close()
    assert len(frame) == 1
    assert bool(frame.iloc[0]["is_complete"])


def test_market_state_is_fail_closed_when_timestamp_is_stale():
    stale = datetime.now(timezone.utc) - timedelta(minutes=10)
    provider = _provider(lambda _request: httpx.Response(200, json={"is_open": True, "timestamp": stale.isoformat()}))
    assert provider.is_market_open() is False
    provider._client.close()


def test_market_data_factory_wires_licensed_rest_settings():
    settings = SimpleNamespace(
        market_data_provider="licensed_rest",
        licensed_market_data_base_url="https://feed.example/v1",
        licensed_market_data_api_key="secret",
        licensed_market_data_api_key_header="X-Key",
        licensed_market_data_quote_path="/q/{symbol}",
        licensed_market_data_ohlcv_path="/bars/{symbol}",
        licensed_market_data_market_state_path="/state",
        licensed_market_data_provider_name="bist-vendor",
        licensed_market_data_timeout_seconds=8,
    )
    provider = build_market_data_provider(settings)
    assert isinstance(provider, LicensedRestMarketDataProvider)
    assert provider.name == "bist-vendor"
    assert provider.api_key_header == "X-Key"
