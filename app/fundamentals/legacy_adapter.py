from __future__ import annotations

from threading import Lock
from time import monotonic
from typing import Callable

from app.data.base_provider import FundamentalProvider
from app.fundamentals.base import FundamentalDataError, FundamentalDataProvider


class LegacyFundamentalProviderAdapter(FundamentalProvider):
    """Expose normalized snapshots to the pre-existing valuation interface."""

    def __init__(
        self,
        provider: FundamentalDataProvider,
        *,
        cache_ttl_seconds: float = 300.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.provider = provider
        self.name = provider.name
        self.cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self._clock = clock
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = Lock()

    def _snapshot(self, symbol: str):
        normalized = symbol.strip().upper().removesuffix(".IS")
        with self._lock:
            now = self._clock()
            cached = self._cache.get(normalized)
            if cached is not None and cached[0] > now:
                return cached[1]
            try:
                snapshot = self.provider.fetch(normalized)
            except FundamentalDataError:
                # Outages are not cached: a recovered licensed provider must be
                # usable on the very next request.
                self._cache.pop(normalized, None)
                return None
            self._cache[normalized] = (now + self.cache_ttl_seconds, snapshot)
            return snapshot

    def _base(self, symbol: str) -> tuple[dict, object | None]:
        snapshot = self._snapshot(symbol)
        if snapshot is None:
            return {"status": "unavailable", "symbol": symbol.strip().upper()}, None
        latest = snapshot.latest_period
        return {
            "status": "available",
            "symbol": snapshot.symbol,
            "financial_period_date": latest.period_end.isoformat(),
            "financial_period": latest.period_end.isoformat(),
            "currency": latest.currency,
            "consolidated": latest.consolidated,
            "revision": latest.revision,
            "source": snapshot.provenance.provider,
            "source_url": snapshot.provenance.source_url,
            "source_trust": snapshot.provenance.trust.value,
        }, snapshot

    def get_income_statement(self, symbol: str) -> dict:
        payload, snapshot = self._base(symbol)
        if snapshot is None:
            return payload
        latest = snapshot.latest_period
        for key in ("revenue", "net_income", "gross_profit", "operating_income", "ebitda", "financing_expenses", "rental_income"):
            if latest.value(key) is not None:
                payload[key] = latest.value(key)
        return payload

    def get_balance_sheet(self, symbol: str) -> dict:
        payload, snapshot = self._base(symbol)
        if snapshot is None:
            return payload
        latest = snapshot.latest_period
        for key in (
            "total_assets", "total_liabilities", "total_equity", "total_debt",
            "cash_and_equivalents", "current_assets", "current_liabilities",
            "market_cap", "shares_outstanding", "net_asset_value", "property_portfolio_value",
        ):
            if latest.value(key) is not None:
                payload[key] = latest.value(key)
        previous = next((item for item in snapshot.periods if item.period_end < latest.period_end), None)
        if previous is not None and previous.value("total_equity") is not None:
            payload["previous_equity"] = previous.value("total_equity")
        return payload

    def get_cash_flow(self, symbol: str) -> dict:
        payload, snapshot = self._base(symbol)
        if snapshot is None:
            return payload
        latest = snapshot.latest_period
        for key in ("operating_cash_flow", "capital_expenditure", "free_cash_flow"):
            value = latest.value(key)
            if value is None and key == "free_cash_flow":
                value = snapshot.ratios.free_cash_flow
            if value is not None:
                payload[key] = value
        return payload

    def get_ratios(self, symbol: str) -> dict:
        payload, snapshot = self._base(symbol)
        if snapshot is not None:
            payload.update({key: value for key, value in snapshot.ratios.as_dict().items() if value is not None})
        return payload

    def get_quarterly_growth(self, symbol: str) -> dict:
        payload, snapshot = self._base(symbol)
        if snapshot is not None:
            if snapshot.ratios.revenue_growth is not None:
                payload["revenue_growth"] = snapshot.ratios.revenue_growth
            if snapshot.ratios.earnings_growth is not None:
                payload["earnings_growth"] = snapshot.ratios.earnings_growth
        return payload
