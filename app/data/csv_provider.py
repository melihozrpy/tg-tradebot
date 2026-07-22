from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from app.data.base_provider import BaseMarketDataProvider, DataFreshness, DataUnavailableError

REQUIRED_COLUMNS = {"timestamp", "open", "high", "low", "close", "volume"}


class CsvMarketDataProvider(BaseMarketDataProvider):
    """Ucretsiz gun sonu (EOD) verisini yerel CSV dosyalarindan okuyan saglayici.

    Beklenen dosya yolu: {csv_data_dir}/{SYMBOL}.csv
    Beklenen kolonlar: timestamp,open,high,low,close,volume
    timestamp ISO 8601 formatinda olmali (ornek: 2026-07-10).

    Bu saglayici sadece gunluk (1d) zaman dilimini destekler; kullanici
    intraday (5m/15m/1h/4h) istese bile CSV'de veri yoksa
    DataUnavailableError firlatilir ve o zaman dilimi kullanilmaz.
    """

    name = "csv"

    def __init__(self, csv_data_dir: str):
        self.csv_data_dir = Path(csv_data_dir)
        self._cache: dict[str, pd.DataFrame] = {}

    def _load(self, symbol: str) -> pd.DataFrame:
        if symbol in self._cache:
            return self._cache[symbol].copy()

        path = self.csv_data_dir / f"{symbol}.csv"
        if not path.exists():
            raise DataUnavailableError(
                f"CSV saglayicida '{symbol}' icin veri dosyasi bulunamadi: {path}"
            )

        df = pd.read_csv(path)
        missing = REQUIRED_COLUMNS - set(df.columns.str.lower())
        if missing:
            raise DataUnavailableError(
                f"CSV dosyasi '{path.name}' eksik kolonlar icermektedir: {missing}"
            )
        df.columns = [c.lower() for c in df.columns]
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.sort_values("timestamp").reset_index(drop=True)

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        if df[["open", "high", "low", "close", "volume"]].isnull().any().any():
            raise DataUnavailableError(
                f"CSV dosyasi '{path.name}' icinde eksik/gecersiz sayisal deger var. "
                "Fail-closed: veri tutarsiz oldugu icin kullanilmayacak."
            )

        self._cache[symbol] = df
        return df.copy()

    def get_quote(self, symbol: str) -> dict:
        df = self._load(symbol)
        last = df.iloc[-1]
        return {
            "symbol": symbol,
            "price": round(float(last["close"]), 2),
            "timestamp": last["timestamp"].to_pydatetime(),
            "provider": self.name,
        }

    def get_ohlcv(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        if timeframe != "1d":
            raise DataUnavailableError(
                f"CSV saglayicida yalnizca '1d' zaman dilimi mevcut, '{timeframe}' desteklenmiyor."
            )
        df = self._load(symbol)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        mask = (df["timestamp"] >= start) & (df["timestamp"] <= end)
        result = df.loc[mask].reset_index(drop=True)
        if result.empty:
            raise DataUnavailableError(
                f"CSV saglayicida {symbol} icin istenen tarih araliginda veri yok."
            )
        return result

    def get_index_data(self, index_symbol: str, timeframe: str) -> pd.DataFrame:
        return self._load(index_symbol)

    def is_market_open(self) -> bool:
        now_ist = datetime.now(timezone.utc) + timedelta(hours=3)
        if now_ist.weekday() >= 5:
            return False
        return 10 <= now_ist.hour < 18

    def get_data_freshness(self, symbol: str, timeframe: str) -> DataFreshness:
        df = self._load(symbol)
        last_ts = df.iloc[-1]["timestamp"].to_pydatetime()
        lag_minutes = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60
        max_lag = 4200  # ~ hafta sonu dahil bir sonraki is gunu kapanisina kadar tolerans
        return DataFreshness(
            symbol=symbol,
            timeframe=timeframe,
            last_timestamp=last_ts,
            is_fresh=lag_minutes <= max_lag,
            max_allowed_lag_minutes=max_lag,
            provider=self.name,
        )

    def health_check(self) -> dict:
        exists = self.csv_data_dir.exists()
        return {
            "provider": self.name,
            "status": "ok" if exists else "degraded",
            "detail": f"csv_data_dir={self.csv_data_dir} exists={exists}",
        }
