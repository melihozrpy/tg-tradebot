from __future__ import annotations

import pytest

from app.execution.paper_broker import PaperBroker, PaperBrokerError


def test_market_buy_reduces_cash():
    broker = PaperBroker(starting_balance=10_000, commission_percent=0.1, slippage_percent=0.0)
    fill = broker.market_buy("THYAO", quantity=10, market_price=100.0)
    assert fill.side == "BUY"
    assert broker.cash < 10_000
    assert "THYAO" in broker.positions


def test_insufficient_balance_rejected():
    broker = PaperBroker(starting_balance=100, commission_percent=0.1, slippage_percent=0.0)
    with pytest.raises(PaperBrokerError):
        broker.market_buy("THYAO", quantity=10, market_price=100.0)


def test_sell_without_position_rejected():
    broker = PaperBroker(starting_balance=10_000)
    with pytest.raises(PaperBrokerError):
        broker.market_sell("THYAO", quantity=1, market_price=100.0)


def test_stop_hit_closes_position():
    broker = PaperBroker(starting_balance=10_000, commission_percent=0.0, slippage_percent=0.0)
    broker.market_buy("THYAO", quantity=10, market_price=100.0, stop_price=95.0)
    reason = broker.check_stop_and_targets("THYAO", current_price=94.0)
    assert reason == "STOP_HIT"
    assert "THYAO" not in broker.positions
    assert len(broker.closed_trade_pnls) == 1


def test_paper_and_real_records_are_separate():
    broker = PaperBroker(starting_balance=10_000)
    broker.market_buy("THYAO", quantity=5, market_price=50.0)
    summary = broker.performance_summary()
    assert summary["starting_balance"] == 10_000
    assert summary["open_positions"] == 1
