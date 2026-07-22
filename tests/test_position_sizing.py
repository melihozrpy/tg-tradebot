from __future__ import annotations

import pytest

from app.risk.position_sizing import (
    InvalidStopError,
    calculate_position_size,
    enforce_daily_loss_limit,
    enforce_max_open_positions,
)


def test_position_size_basic():
    result = calculate_position_size(
        total_capital=100_000, risk_percent=1.0, entry_price=50.0, stop_price=48.0
    )
    assert result.lot == int((100_000 * 0.01) / 2.0)
    assert result.risk_amount == 1000.0


def test_zero_stop_distance_rejected():
    with pytest.raises(InvalidStopError):
        calculate_position_size(total_capital=100_000, risk_percent=1.0, entry_price=50.0, stop_price=50.0)


def test_too_narrow_stop_rejected():
    with pytest.raises(InvalidStopError):
        calculate_position_size(total_capital=100_000, risk_percent=1.0, entry_price=50.0, stop_price=49.95)


def test_daily_loss_limit_triggers():
    assert enforce_daily_loss_limit(realized_pnl_today=-2500, total_capital=100_000, max_daily_loss_percent=2.0) is True
    assert enforce_daily_loss_limit(realized_pnl_today=-500, total_capital=100_000, max_daily_loss_percent=2.0) is False


def test_max_open_positions():
    assert enforce_max_open_positions(5, 5) is True
    assert enforce_max_open_positions(4, 5) is False
