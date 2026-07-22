from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from app.backtest.engine_v5g import BacktestConfig, SignalInstruction, TransactionCostConfig
from app.backtest.universe import UniverseBacktestEngine, UniverseBacktestRequest


START = datetime(2025, 1, 1, tzinfo=timezone.utc)
END = START + timedelta(days=10)


def _bars(*_args):
    return pd.DataFrame([
        {"timestamp": START + timedelta(days=i), "open": 100, "high": 101, "low": 99,
         "close": 100, "volume": 1000, "is_complete": True,
         "data_quality": "VALID", "price_mode": "adjusted"}
        for i in range(5)
    ])


def _engine(mapping=None):
    return UniverseBacktestEngine(
        BacktestConfig(minimum_history_bars=1, transaction_costs=TransactionCostConfig(0, 0, 0, 0, 0)),
        symbol_to_sector=mapping,
    )


def test_63_watchlist_scope_runs_each_requested_symbol():
    request = UniverseBacktestRequest("watchlist", START, END, symbols=("THYAO", "ASELS"))
    result = _engine().run(
        request, data_loader=_bars, signal_provider_factory=lambda _symbol: lambda _ctx: SignalInstruction()
    )
    assert set(result.results) == {"THYAO", "ASELS"}
    assert result.failures == {}


def test_64_sector_scope_filters_symbols_and_regime_signal_type():
    request = UniverseBacktestRequest(
        "sector", START, END, symbols=("AAA", "BBB", "CCC"), sector="BANKA",
        signal_type="BUY_CANDIDATE", market_regime="boga",
    )
    result = _engine({"AAA": "BANKA", "BBB": "SANAYI", "CCC": "BANKA"}).run(
        request, data_loader=_bars,
        signal_provider_factory=lambda _symbol: lambda ctx: SignalInstruction(
            action="BUY", stop_price=90, targets=(110, 120, 130),
            signal_type="BUY_CANDIDATE", market_regime="boga",
        ),
    )
    assert set(result.results) == {"AAA", "CCC"}
    assert all(len(item.trades) == 1 for item in result.results.values())
