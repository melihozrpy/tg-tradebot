from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.data.cached_provider import CachedMarketDataProvider
from app.data.mock_provider import MockMarketDataProvider


def test_cache_hit_avoids_redundant_fetch():
    inner = MockMarketDataProvider()
    cached = CachedMarketDataProvider(inner, daily_ttl_minutes=30)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)

    df1 = cached.get_ohlcv("THYAO", "1d", start, end)
    assert cached.misses == 1
    assert cached.hits == 0

    df2 = cached.get_ohlcv("THYAO", "1d", start, end)
    assert cached.misses == 1  # ikinci cagri cache'ten geldi, tekrar indirmedi
    assert cached.hits == 1
    assert len(df1) == len(df2)


def test_cache_miss_for_different_symbol():
    inner = MockMarketDataProvider()
    cached = CachedMarketDataProvider(inner, daily_ttl_minutes=30)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)

    cached.get_ohlcv("THYAO", "1d", start, end)
    cached.get_ohlcv("ASELS", "1d", start, end)
    assert cached.misses == 2
    assert cached.hits == 0


def test_cache_expired_ttl_refetches():
    inner = MockMarketDataProvider()
    cached = CachedMarketDataProvider(inner, daily_ttl_minutes=30)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)
    cached.get_ohlcv("THYAO", "1d", start, end)

    # TTL'i manuel olarak gecmis gibi ayarla
    key = ("THYAO", "1d")
    cached._cache[key].fetched_at = datetime.now(timezone.utc) - timedelta(minutes=31)

    cached.get_ohlcv("THYAO", "1d", start, end)
    assert cached.misses == 2  # TTL doldugu icin tekrar indirildi


def test_cache_clear_resets_stats():
    inner = MockMarketDataProvider()
    cached = CachedMarketDataProvider(inner, daily_ttl_minutes=30)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)
    cached.get_ohlcv("THYAO", "1d", start, end)
    cached.clear_cache()
    assert cached.hits == 0
    assert cached.misses == 0
    assert len(cached._cache) == 0
