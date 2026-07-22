from __future__ import annotations

"""Yahoo Chart HTTP endpoint adapter.

Bu adapter yfinance kütüphanesinden bağımsız ikinci çağrı yoludur. BIST
desteği yalnızca Yahoo'nun gerçekten döndürdüğü ``.IS`` sembollerle sınırlıdır;
boş/eksik cevapta veri uydurmaz ve ``DataUnavailableError`` üretir.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import pandas as pd

from app.data.base_provider import BaseMarketDataProvider, DataFreshness, DataUnavailableError
from app.data.yfinance_provider import MAX_LAG_MINUTES_MAP, normalize_bist_symbol

INTERVALS = {"5m", "15m", "1h", "1d", "1wk"}


class YahooChartMarketDataProvider(BaseMarketDataProvider):
    name = "yahoo_chart"

    def __init__(
        self,
        timeout_seconds: int = 10,
        price_mode: str = "unadjusted",
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.price_mode = price_mode
        self._client = client
        self._corporate_actions: dict[str, list[dict]] = {}

    def _request(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> dict:
        yf_symbol = normalize_bist_symbol(symbol)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}"
        params = {
            "period1": int(start.timestamp()),
            "period2": int((end + timedelta(minutes=1)).timestamp()),
            "interval": timeframe,
            "events": "div,splits",
            "includeAdjustedClose": "true",
        }
        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=self.timeout_seconds, follow_redirects=True)
        try:
            response = client.get(url, params=params)
            if response.status_code == 429:
                raise DataUnavailableError("Yahoo Chart rate limit (HTTP 429).")
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException as exc:
            raise DataUnavailableError(f"Yahoo Chart timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise DataUnavailableError(f"Yahoo Chart HTTP hatası: {exc}") from exc
        finally:
            if owns_client:
                client.close()

    def _parse(self, payload: dict, symbol: str) -> pd.DataFrame:
        chart = payload.get("chart") or {}
        if chart.get("error"):
            raise DataUnavailableError(f"Yahoo Chart hata cevabı: {chart['error']}")
        results = chart.get("result") or []
        if not results:
            raise DataUnavailableError(f"'{symbol}' için Yahoo Chart verisi bulunamadı.")
        result = results[0]
        timestamps = result.get("timestamp") or []
        indicators = result.get("indicators") or {}
        quotes = indicators.get("quote") or []
        if not timestamps or not quotes:
            raise DataUnavailableError(f"'{symbol}' için Yahoo Chart OHLCV alanları boş.")
        quote = quotes[0]
        length = len(timestamps)
        required = {key: quote.get(key) or [] for key in ("open", "high", "low", "close", "volume")}
        if any(len(values) != length for values in required.values()):
            raise DataUnavailableError(f"'{symbol}' için Yahoo Chart kolon uzunlukları tutarsız.")

        df = pd.DataFrame({"timestamp": pd.to_datetime(timestamps, unit="s", utc=True), **required})
        adj_rows = indicators.get("adjclose") or []
        if self.price_mode == "adjusted" and adj_rows and len(adj_rows[0].get("adjclose") or []) == length:
            adjusted = pd.Series(adj_rows[0]["adjclose"], dtype="float64")
            close = pd.to_numeric(df["close"], errors="coerce")
            factor = (adjusted / close).replace([float("inf"), -float("inf")], pd.NA).fillna(1.0)
            for col in ("open", "high", "low", "close"):
                df[col] = pd.to_numeric(df[col], errors="coerce") * factor

        events = result.get("events") or {}
        parsed_actions: list[dict] = []
        for raw in (events.get("splits") or {}).values():
            parsed_actions.append({"type": "split", "date": datetime.fromtimestamp(raw["date"], timezone.utc), "ratio": raw.get("splitRatio")})
        for raw in (events.get("dividends") or {}).values():
            parsed_actions.append({"type": "dividend", "date": datetime.fromtimestamp(raw["date"], timezone.utc), "amount": raw.get("amount")})
        self._corporate_actions[normalize_bist_symbol(symbol)] = parsed_actions

        df = df.dropna(subset=["open", "high", "low", "close", "volume"])
        df = df.drop_duplicates(subset="timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)
        if df.empty:
            raise DataUnavailableError(f"'{symbol}' için Yahoo Chart geçerli satır döndürmedi.")
        return df

    def get_ohlcv(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        if timeframe not in INTERVALS:
            raise DataUnavailableError(f"Yahoo Chart zaman dilimi desteklenmiyor: {timeframe}")
        if start >= end:
            raise ValueError("start, end değerinden önce olmalı.")
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return self._parse(self._request(symbol, timeframe, start, end), symbol)

    def get_quote(self, symbol: str) -> dict:
        end = datetime.now(timezone.utc)
        df = self.get_ohlcv(symbol, "1d", end - timedelta(days=15), end)
        row = df.iloc[-1]
        return {"symbol": normalize_bist_symbol(symbol), "price": float(row["close"]), "timestamp": row["timestamp"].to_pydatetime(), "provider": self.name}

    def get_index_data(self, index_symbol: str, timeframe: str) -> pd.DataFrame:
        end = datetime.now(timezone.utc)
        return self.get_ohlcv(index_symbol, timeframe, end - timedelta(days=500), end)

    def get_corporate_actions(self, symbol: str) -> list:
        return list(self._corporate_actions.get(normalize_bist_symbol(symbol), []))

    def is_market_open(self) -> bool:
        now_ist = datetime.now(timezone.utc) + timedelta(hours=3)
        return now_ist.weekday() < 5 and 10 <= now_ist.hour < 18

    def get_data_freshness(self, symbol: str, timeframe: str) -> DataFreshness:
        max_lag = MAX_LAG_MINUTES_MAP.get(timeframe, 7200)
        try:
            end = datetime.now(timezone.utc)
            df = self.get_ohlcv(symbol, timeframe, end - timedelta(days=15), end)
            last = df.iloc[-1]["timestamp"].to_pydatetime()
            lag = (end - last).total_seconds() / 60
            return DataFreshness(symbol, timeframe, last, lag <= max_lag, max_lag, self.name)
        except DataUnavailableError:
            return DataFreshness(symbol, timeframe, None, False, max_lag, self.name)

    def health_check(self) -> dict:
        try:
            self.get_quote("XU100")
            return {"provider": self.name, "status": "ok", "detail": "Yahoo Chart erişilebilir."}
        except Exception as exc:  # noqa: BLE001
            return {"provider": self.name, "status": "down", "detail": str(exc)}
