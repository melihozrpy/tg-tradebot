from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

from app.data.base_provider import BaseMarketDataProvider, DataFreshness
from app.modules.report_presentation import format_breadth_panel
from app.services.market_breadth_service import BreadthCandidate, MarketBreadthResult, compute_market_breadth


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


def test_breadth_panel_explains_each_selected_candidate_with_condition_and_target():
    long = BreadthCandidate(
        symbol="THYAO", direction="LONG", score=88, change_percent=2.1,
        last_close=300.0, relative_volume=1.3,
        reasons=("EMA20 üstü", "EMA20 > EMA50", "20g momentum +4.0"),
        confirmation_level=302.0, technical_target=308.0, target_basis="önceki 20g direnç",
    )
    short = BreadthCandidate(
        symbol="SASA", direction="SHORT/RİSK", score=82, change_percent=-1.8,
        last_close=4.0, relative_volume=1.1,
        reasons=("EMA20 altı", "EMA20 < EMA50", "20g momentum -5.0"),
        confirmation_level=3.9, technical_target=3.6, target_basis="önceki 20g destek",
    )
    breadth = MarketBreadthResult(
        available=True, note="", universe_size=571, scanned=540, failed=31, coverage_ratio=94.6,
        advancers=311, decliners=200, unchanged=29, net_breadth=111,
        above_ema20_ratio=30.9, above_ema50_ratio=29.8, above_ema200_ratio=37.9,
        new_20d_highs=25, new_20d_lows=31, rising_volume_ratio=36.7,
        breadth_score=41, regime="ILIMLI NEGATİF", tomorrow_bias="NEGATİF-YATAY",
        long_count=132, short_count=329, neutral_count=79,
        long_candidates=(long,), short_candidates=(short,),
    )

    text = "\n".join(format_breadth_panel(breadth, report_kind="evening"))

    assert "GÜÇLÜ LONG İZLEME" in text
    assert "Neden güçlü: EMA20 üstü" in text
    assert "Potansiyel hedef: 308,00" in text
    assert "ZAYIF / SHORT-RİSK İZLEME" in text
    assert "3,90 altında günlük kapanış sürerse" in text
