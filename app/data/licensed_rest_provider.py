from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import pandas as pd

from app.data.base_provider import BaseMarketDataProvider, DataFreshness, DataUnavailableError


class LicensedRestMarketDataProvider(BaseMarketDataProvider):
    """Strict adapter for an operator-contracted BIST market-data gateway.

    The gateway contract is deliberately small and configurable. It must return
    timestamps and live/completion flags; missing metadata is rejected instead
    of presenting an old close as a current price.
    """

    name = "licensed_rest"
    supports_verified_live_transactions = True

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_key_header: str = "X-API-Key",
        quote_path: str = "/quote/{symbol}",
        ohlcv_path: str = "/ohlcv/{symbol}",
        market_state_path: str = "/market-state",
        provider_name: str = "licensed_rest",
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
        if (parsed.scheme != "https" and not local_http) or not parsed.netloc:
            raise ValueError("Lisanslı piyasa veri adresi HTTPS olmalıdır.")
        if not api_key.strip():
            raise ValueError("Lisanslı piyasa veri API anahtarı eksik.")
        if "{symbol}" not in quote_path or "{symbol}" not in ohlcv_path:
            raise ValueError("Quote ve OHLCV yolları {symbol} alanını içermelidir.")
        if not api_key_header.strip() or any(char in api_key_header for char in "\r\n"):
            raise ValueError("Piyasa veri API anahtarı başlığı geçersiz.")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key.strip()
        self.api_key_header = api_key_header.strip()
        self.quote_path = quote_path
        self.ohlcv_path = ohlcv_path
        self.market_state_path = market_state_path
        self.name = provider_name.strip() or self.name
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._last_timestamps: dict[tuple[str, str], datetime] = {}

    @staticmethod
    def _symbol(value: str) -> str:
        normalized = value.strip().upper().removesuffix(".IS")
        if not normalized.isalnum() or not 3 <= len(normalized) <= 12:
            raise DataUnavailableError("Geçersiz BIST sembolü.")
        return normalized

    def _url(self, path: str, *, symbol: str | None = None) -> str:
        if symbol is not None:
            path = path.format(symbol=quote(symbol, safe=""))
        return f"{self.base_url}/{path.lstrip('/')}"

    def _get(self, path: str, *, symbol: str | None = None, params: dict[str, str] | None = None) -> Any:
        url = self._url(path, symbol=symbol)
        headers = {self.api_key_header: self._api_key, "Accept": "application/json"}

        def execute(client: httpx.Client):
            try:
                response = client.get(url, headers=headers, params=params)
            except httpx.HTTPError as exc:
                raise DataUnavailableError("Lisanslı piyasa veri bağlantısı kurulamadı.") from exc
            if response.status_code >= 400:
                raise DataUnavailableError(f"Lisanslı piyasa veri servisi HTTP {response.status_code} hatası verdi.")
            try:
                return response.json()
            except ValueError as exc:
                raise DataUnavailableError("Lisanslı piyasa veri servisi geçersiz JSON döndürdü.") from exc

        if self._client is not None:
            return execute(self._client)
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=False) as client:
            return execute(client)

    @staticmethod
    def _datetime(value: Any) -> datetime:
        try:
            parsed = pd.Timestamp(value)
            if parsed.tzinfo is None:
                raise ValueError
            return parsed.tz_convert("UTC").to_pydatetime()
        except Exception as exc:
            raise DataUnavailableError("Piyasa verisinde saat dilimli timestamp eksik veya geçersiz.") from exc

    @staticmethod
    def _payload(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise DataUnavailableError("Piyasa veri servisi JSON nesnesi döndürmedi.")
        result = value.get("result")
        return result if isinstance(result, dict) else value

    def get_quote(self, symbol: str) -> dict:
        normalized = self._symbol(symbol)
        payload = self._payload(self._get(self.quote_path, symbol=normalized))
        returned_symbol = self._symbol(str(payload.get("symbol") or normalized))
        if returned_symbol != normalized:
            raise DataUnavailableError("Piyasa veri servisi farklı sembol döndürdü.")
        try:
            price = float(payload["price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DataUnavailableError("Quote fiyatı eksik veya geçersiz.") from exc
        if not isfinite(price) or price <= 0:
            raise DataUnavailableError("Quote fiyatı pozitif ve sonlu olmalıdır.")
        timestamp = self._datetime(payload.get("timestamp"))
        is_live = payload.get("is_live") is True
        valid_transaction = payload.get("valid_transaction") is True
        self._last_timestamps[(normalized, "quote")] = timestamp
        return {
            "symbol": normalized,
            "price": price,
            "timestamp": timestamp,
            "provider": self.name,
            "is_live": is_live,
            "is_fresh": is_live and valid_transaction,
            "market_open": payload.get("market_open") is True,
            "trade_id": payload.get("trade_id"),
            "open": payload.get("open"),
            "high": payload.get("high"),
            "low": payload.get("low"),
            "volume": payload.get("volume"),
            "last_trade_quantity": payload.get("last_trade_quantity"),
            "valid_transaction": valid_transaction,
            "trading_state": payload.get("trading_state"),
            "upper_limit": payload.get("upper_limit"),
            "lower_limit": payload.get("lower_limit"),
            "upper_limit_locked": payload.get("upper_limit_locked") is True,
            "lower_limit_locked": payload.get("lower_limit_locked") is True,
            "available_buy_quantity": payload.get("available_buy_quantity"),
            "available_sell_quantity": payload.get("available_sell_quantity"),
            "volume_ratio": payload.get("volume_ratio"),
            # A quote is a transaction tick, not a completed candle.  A vendor
            # may explicitly attach a completed bar, but missing metadata must
            # never satisfy completed-close breakout confirmation.
            "bar_complete": payload.get("bar_complete") is True,
        }

    def get_latest_intraday_snapshot(self, symbol: str) -> dict:
        quote_payload = self.get_quote(symbol)
        return {
            "available": True,
            "last_price": quote_payload["price"],
            "timestamp": quote_payload["timestamp"],
            "provider": quote_payload["provider"],
            "is_fresh": quote_payload["is_fresh"],
            "is_live": quote_payload["is_live"],
            "valid_transaction": quote_payload["valid_transaction"],
            "timeframe": "quote",
        }

    def get_ohlcv(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        normalized = self._symbol(symbol)
        payload = self._get(
            self.ohlcv_path,
            symbol=normalized,
            params={
                "timeframe": timeframe,
                "start": start.astimezone(timezone.utc).isoformat(),
                "end": end.astimezone(timezone.utc).isoformat(),
            },
        )
        if isinstance(payload, dict):
            payload = payload.get("bars", payload.get("result"))
        if not isinstance(payload, list):
            raise DataUnavailableError("OHLCV yanıtında bars listesi bulunamadı.")
        frame = pd.DataFrame(payload)
        required = {"timestamp", "open", "high", "low", "close", "volume", "is_complete"}
        if frame.empty or not required.issubset(frame.columns):
            raise DataUnavailableError("OHLCV verisinde zorunlu alanlar eksik.")
        frame = frame.copy()
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["is_complete"] = frame["is_complete"].map(
            lambda value: value is True
            or (isinstance(value, (int, float)) and not pd.isna(value) and value == 1)
            or (isinstance(value, str) and value.strip().casefold() in {"true", "1", "yes"})
        )
        frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
        valid = (
            (frame[["open", "high", "low", "close"]] > 0).all(axis=1)
            & (frame["volume"] >= 0)
            & (frame["high"] >= frame[["open", "close", "low"]].max(axis=1))
            & (frame["low"] <= frame[["open", "close", "high"]].min(axis=1))
        )
        frame = frame.loc[valid].sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)
        if frame.empty:
            raise DataUnavailableError("OHLCV verisinde doğrulanabilir mum bulunamadı.")
        self._last_timestamps[(normalized, timeframe)] = frame.iloc[-1]["timestamp"].to_pydatetime()
        return frame

    def get_index_data(self, index_symbol: str, timeframe: str) -> pd.DataFrame:
        end = datetime.now(timezone.utc)
        return self.get_ohlcv(index_symbol, timeframe, end - pd.Timedelta(days=730), end)

    def is_market_open(self) -> bool:
        if not self.market_state_path:
            return False
        try:
            payload = self._payload(self._get(self.market_state_path))
            timestamp = self._datetime(payload.get("timestamp"))
            age = (datetime.now(timezone.utc) - timestamp).total_seconds()
            return payload.get("is_open") is True and -30 <= age <= 120
        except DataUnavailableError:
            return False

    def get_data_freshness(self, symbol: str, timeframe: str) -> DataFreshness:
        normalized = self._symbol(symbol)
        timestamp = self._last_timestamps.get((normalized, timeframe))
        if timestamp is None and timeframe != "quote":
            timestamp = self._last_timestamps.get((normalized, "quote"))
        max_lag = {"5m": 20, "15m": 35, "1h": 120, "1d": 1440, "1wk": 10080}.get(timeframe, 20)
        age_minutes = (datetime.now(timezone.utc) - timestamp).total_seconds() / 60 if timestamp else None
        return DataFreshness(normalized, timeframe, timestamp, age_minutes is not None and 0 <= age_minutes <= max_lag, max_lag, self.name)

    def health_check(self) -> dict:
        if not self.market_state_path:
            return {"provider": self.name, "status": "degraded", "reason": "market_state_path_missing"}
        try:
            payload = self._payload(self._get(self.market_state_path))
            self._datetime(payload.get("timestamp"))
            return {"provider": self.name, "status": "ok"}
        except DataUnavailableError:
            return {"provider": self.name, "status": "down"}
