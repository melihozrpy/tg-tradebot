from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

from app.data.base_provider import BaseMarketDataProvider, DataFreshness
from app.services.market_breadth_service import compute_market_breadth


class _TrendProvider(BaseMarketDataProvider):
    name = "test"

    def get_quote(self, symbol: str) -> dict:
        raise NotImplementedError

    def get_ohlcv(self, symbol, timeframe, start, end):
        rising = symbol in {"AAA", "BBB"}
        values = [100 + index * (0.4 if rising else -0.25) for index in range(250)]
        volume = [1000.0] * 249 + [1800.0]
        return pd.DataFrame(
            {
                "timestamp": pd.date_range("2025-01-01", periods=250, tz="UTC"),
                "open": values,
                "high": [value + 1 for value in values],
                "low": [value - 1 for value in values],
                "close": values,
                "volume": volume,
            }
        )

    def get_market_state(self, at=None):
        return "closed"

    def get_index_data(self, index_symbol, timeframe):
        return self.get_ohlcv(index_symbol, timeframe, datetime.now(timezone.utc), datetime.now(timezone.utc))

    def is_market_open(self):
        return False

    def get_data_freshness(self, symbol, timeframe):
        return DataFreshness(symbol, timeframe, datetime.now(timezone.utc), True, 60, self.name)

    def health_check(self):
        return {"ok": True}


def test_json_universe_is_scanned_completely_and_classified(tmp_path):
    path = tmp_path / "bist.json"
    path.write_text(
        json.dumps({"instruments": [{"symbol": value} for value in ("AAA", "BBB", "CCC", "DDD")]}),
        encoding="utf-8",
    )
    result = compute_market_breadth(
        _TrendProvider(),
        str(path),
        minimum_signal_score=60,
        top_n=4,
    )

    assert result.available is True
    assert result.universe_size == result.scanned == 4
    assert result.coverage_ratio == 100.0
    assert result.advancers == 2
    assert result.decliners == 2
    assert {item.symbol for item in result.long_candidates} == {"AAA", "BBB"}
    assert {item.symbol for item in result.short_candidates} == {"CCC", "DDD"}
    assert result.above_ema200_ratio == 50.0
    assert "spot satış emri değil" in result.note
