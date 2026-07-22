from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from app.data.base_provider import BaseMarketDataProvider, DataFreshness, DataUnavailableError


def _seed_from_symbol(symbol: str) -> int:
    digest = hashlib.sha256(symbol.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


class MockMarketDataProvider(BaseMarketDataProvider):
    """Deterministik, tohumlu (seeded) rastgelelik ile sentetik OHLCV uretir.

    ONEMLI: Bu saglayici GERCEK piyasa verisi degildir. Sadece gelistirme
    ve test amaclidir. Uretilen veriler asla gercek veri gibi sunulmaz;
    provider adi her zaman "mock" olarak mesajlarda gosterilir.
    """

    name = "mock"

    def __init__(self, base_prices: dict | None = None):
        self._base_prices = base_prices or {}
        self._cache: dict[tuple, pd.DataFrame] = {}

    def _generate_ohlcv(self, symbol: str, timeframe: str, periods: int, anchor: datetime | None = None) -> pd.DataFrame:
        cache_key = (symbol, timeframe, periods)
        if cache_key in self._cache:
            return self._cache[cache_key].copy()

        seed = _seed_from_symbol(f"{symbol}:{timeframe}")
        rng = np.random.default_rng(seed)

        base_price = self._base_prices.get(symbol, 20 + (seed % 200))
        freq_map = {
            "5m": "5min",
            "15m": "15min",
            "1h": "1h",
            "4h": "4h",
            "1d": "1B",  # is gunu
            "1w": "1W",
            "1wk": "1W",  # V3.1: coklu zaman dilimi motorunun kullandigi kanonik kod
        }
        freq = freq_map.get(timeframe, "1B")

        # ONEMLI: anchor disaridan verilmezse "now()" kullanilir, ancak bu
        # metot cache-miss aninda cagrilir cagrilmaz cache'e yazilir. Eger
        # cagiran taraf (ornegin get_ohlcv) mask filtrelemesinde KENDI
        # onceden hesapladigi "end" degerini kullaniyorsa, burada da AYNI
        # anchor'in gecirilmesi gerekir; aksi halde bu fonksiyonun kendi ic
        # "now()" cagrisi disaridaki "end"den birkac milisaniye sonraya
        # denk gelebilir ve en son barin mask disinda kalmasina (dolayisiyla
        # ayni sembol icin ardisik cagrilarda farkli sonuc donmesine, yani
        # idempotency/backtest belirlenimsizligine) yol acabilir.
        end = anchor if anchor is not None else datetime.now(timezone.utc)
        idx = pd.date_range(end=end, periods=periods, freq=freq, tz="UTC")
        # NOT: bazi pandas surumlerinde belirli freq degerleri (orn. haftalik)
        # icin date_range, istenen "periods" sayisindan bir eksik/fazla index
        # dondurebiliyor. Asagidaki dizi uretimlerinin index ile ayni
        # uzunlukta olmasi icin gercek uretilen index uzunlugu kullanilir.
        actual_periods = len(idx)

        # Hafif trend + gurultu birlesimi ile gercekci gorunumlu ama tamamen
        # sentetik bir fiyat serisi olustur. Look-ahead bias yok: her adim
        # yalnizca kendinden onceki adima bagli.
        drift = rng.normal(loc=0.0002, scale=0.001)
        returns = rng.normal(loc=drift, scale=0.018, size=actual_periods)
        prices = base_price * np.cumprod(1 + returns)

        highs = prices * (1 + np.abs(rng.normal(0, 0.006, size=actual_periods)))
        lows = prices * (1 - np.abs(rng.normal(0, 0.006, size=actual_periods)))
        opens = np.roll(prices, 1)
        opens[0] = prices[0]
        volumes = np.abs(rng.normal(loc=1_500_000, scale=500_000, size=actual_periods))

        df = pd.DataFrame(
            {
                "timestamp": idx,
                "open": opens,
                "high": np.maximum.reduce([highs, opens, prices]),
                "low": np.minimum.reduce([lows, opens, prices]),
                "close": prices,
                "volume": volumes,
            }
        )
        self._cache[cache_key] = df
        return df.copy()

    def get_quote(self, symbol: str) -> dict:
        df = self._generate_ohlcv(symbol, "1d", 5)
        last = df.iloc[-1]
        return {
            "symbol": symbol,
            "price": round(float(last["close"]), 2),
            "timestamp": last["timestamp"].to_pydatetime(),
            "provider": self.name,
        }

    def get_ohlcv(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        if start >= end:
            raise ValueError("start, end degerinden once olmali.")
        periods = 400
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        df = self._generate_ohlcv(symbol, timeframe, periods, anchor=end)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        mask = (df["timestamp"] >= start) & (df["timestamp"] <= end)
        result = df.loc[mask].reset_index(drop=True)
        if result.empty:
            raise DataUnavailableError(
                f"Mock saglayicida {symbol}/{timeframe} icin istenen tarih araliginda veri yok."
            )
        return result

    def get_index_data(self, index_symbol: str, timeframe: str) -> pd.DataFrame:
        return self._generate_ohlcv(index_symbol, timeframe, 400)

    def is_market_open(self) -> bool:
        now_ist = datetime.now(timezone.utc) + timedelta(hours=3)
        if now_ist.weekday() >= 5:
            return False
        return 10 <= now_ist.hour < 18

    def get_data_freshness(self, symbol: str, timeframe: str) -> DataFreshness:
        df = self._generate_ohlcv(symbol, timeframe, 5)
        last_ts = df.iloc[-1]["timestamp"].to_pydatetime()
        lag_minutes = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60
        max_lag = {"5m": 15, "15m": 30, "1h": 90, "4h": 300, "1d": 4200, "1w": 11000}.get(
            timeframe, 4200
        )
        return DataFreshness(
            symbol=symbol,
            timeframe=timeframe,
            last_timestamp=last_ts,
            is_fresh=lag_minutes <= max_lag,
            max_allowed_lag_minutes=max_lag,
            provider=self.name,
        )

    def health_check(self) -> dict:
        return {"provider": self.name, "status": "ok", "detail": "mock veri saglayicisi calisiyor"}
