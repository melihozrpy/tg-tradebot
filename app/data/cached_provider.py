from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from app.data.base_provider import BaseMarketDataProvider, DataFreshness


@dataclass
class _CacheEntry:
    fetched_at: datetime
    df: pd.DataFrame


class CachedMarketDataProvider(BaseMarketDataProvider):
    """Baska bir BaseMarketDataProvider'i sarmalayip kisa sureli (TTL'li)
    bellek-ici (in-memory) cache ekler.

    Amac: ayni sembolun verisini kisa sure icinde tekrar tekrar yfinance
    gibi ucretsiz/rate-limitli kaynaklardan indirmemek (bkz. spesifikasyon
    bolum 15). Cache YALNIZCA performans icindir; hicbir zaman veri
    uydurmaz — TTL doldugunda veya cache bosken her zaman alttaki
    saglayiciya (ve onun kendi 'mock veriye asla gecme' kuralina) gider.
    """

    def __init__(
        self,
        inner: BaseMarketDataProvider,
        daily_ttl_minutes: int = 30,
        intraday_ttl_minutes: int = 5,
    ):
        self._inner = inner
        self.name = f"cached({inner.name})"
        self.daily_ttl_minutes = daily_ttl_minutes
        self.intraday_ttl_minutes = intraday_ttl_minutes
        self._cache: dict[tuple, _CacheEntry] = {}
        self.hits = 0
        self.misses = 0

    def _ttl_for(self, timeframe: str) -> int:
        return self.daily_ttl_minutes if timeframe == "1d" else self.intraday_ttl_minutes

    def _is_fresh(self, entry: _CacheEntry, timeframe: str) -> bool:
        age_minutes = (datetime.now(timezone.utc) - entry.fetched_at).total_seconds() / 60
        return age_minutes <= self._ttl_for(timeframe)

    def get_ohlcv(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        cache_key = (symbol.upper(), timeframe)
        cached = self._cache.get(cache_key)
        if cached is not None and self._is_fresh(cached, timeframe):
            self.hits += 1
            df = cached.df
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            mask = (df["timestamp"] >= start) & (df["timestamp"] <= end)
            return df.loc[mask].reset_index(drop=True)

        self.misses += 1
        df = self._inner.get_ohlcv(symbol, timeframe, start, end)
        self._cache[cache_key] = _CacheEntry(fetched_at=datetime.now(timezone.utc), df=df.copy())
        return df

    def get_quote(self, symbol: str) -> dict:
        return self._inner.get_quote(symbol)

    def get_index_data(self, index_symbol: str, timeframe: str) -> pd.DataFrame:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=500)
        return self.get_ohlcv(index_symbol, timeframe, start, end)

    def get_sector_data(self, sector_symbol: str, timeframe: str) -> pd.DataFrame:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=500)
        return self.get_ohlcv(sector_symbol, timeframe, start, end)

    def get_market_breadth(self) -> dict:
        return self._inner.get_market_breadth()

    def get_corporate_actions(self, symbol: str) -> list:
        return self._inner.get_corporate_actions(symbol)

    def is_market_open(self) -> bool:
        return self._inner.is_market_open()

    def get_data_freshness(self, symbol: str, timeframe: str) -> DataFreshness:
        return self._inner.get_data_freshness(symbol, timeframe)

    def health_check(self) -> dict:
        inner_health = self._inner.health_check()
        return {
            **inner_health,
            "cache_hits": self.hits,
            "cache_misses": self.misses,
        }

    def clear_cache(self) -> None:
        self._cache.clear()
        self.hits = 0
        self.misses = 0
