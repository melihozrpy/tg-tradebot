from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.backtest.engine_v5g import (
    BacktestConfig, BacktestEngine, BacktestValidationError, LookAheadBiasError, PointInTimeContext,
    SignalInstruction, TransactionCostConfig,
)
from app.backtest.metrics import compute_metrics


ZERO_COSTS = TransactionCostConfig(0, 0, 0, 0, 0)


def _bars(rows: list[tuple[float, float, float, float]], **extra) -> pd.DataFrame:
    start = datetime(2025, 1, 2, tzinfo=timezone.utc)
    data = []
    for index, (open_, high, low, close) in enumerate(rows):
        row = {
            "timestamp": start + timedelta(days=index), "open": open_, "high": high,
            "low": low, "close": close, "volume": 1_000_000,
            "is_complete": True, "data_quality": "VALID", "price_mode": "adjusted",
        }
        for key, values in extra.items():
            row[key] = values[index]
        data.append(row)
    return pd.DataFrame(data)


def _buy_once(stop=95.0, targets=(105.0, 110.0, 115.0)):
    def provider(context):
        if len(context.bars) == 1:
            return SignalInstruction(
                action="BUY", stop_price=stop, targets=targets,
                raw_signal_score=72, signal_type="BUY_CANDIDATE",
                signal_time=context.as_of, levels_as_of=context.as_of,
            )
        return SignalInstruction()
    return provider


def _config(**changes) -> BacktestConfig:
    values = dict(
        initial_capital=100_000, max_position_pct=20, minimum_history_bars=1,
        minimum_sample_size=30, transaction_costs=ZERO_COSTS, max_holding_bars=20,
    )
    values.update(changes)
    return BacktestConfig(**values)


def test_01_future_candle_access_is_blocked():
    context = PointInTimeContext(_bars([(100, 101, 99, 100)]))
    with pytest.raises(LookAheadBiasError):
        context.future_bar()
    with pytest.raises(LookAheadBiasError):
        context.bar(1)


def test_02_signal_close_is_not_used_as_same_price_entry():
    frame = _bars([(100, 101, 99, 100), (107, 108, 106, 107)])
    result = BacktestEngine(_config()).run(frame, "THYAO", _buy_once(stop=90, targets=(120, 130, 140)))
    assert result.trades[0].entry_price == 107
    assert result.trades[0].entry_price != frame.iloc[0].close


def test_03_next_open_entry_is_correct():
    frame = _bars([(100, 101, 99, 100), (103, 104, 102, 103), (104, 105, 103, 104)])
    result = BacktestEngine(_config(entry_model="next_open")).run(frame, "THYAO", _buy_once(stop=90, targets=(120, 130, 140)))
    assert result.trades[0].entry_time == frame.iloc[1].timestamp.to_pydatetime()
    assert result.trades[0].entry_price == 103


def test_04_incomplete_candle_is_excluded():
    frame = _bars([(100, 101, 99, 100), (101, 102, 100, 101), (102, 103, 101, 102)])
    frame.loc[1, "is_complete"] = False
    result = BacktestEngine(_config()).run(frame, "THYAO", _buy_once(stop=90, targets=(120, 130, 140)))
    assert any(item["reason"] == "INCOMPLETE_CANDLE" for item in result.excluded_periods)
    assert result.trades[0].entry_time == frame.iloc[2].timestamp.to_pydatetime()


def test_05_commission_is_applied():
    frame = _bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100)])
    free = BacktestEngine(_config()).run(frame, "THYAO", _buy_once(stop=90, targets=(120, 130, 140)))
    paid = BacktestEngine(_config(transaction_costs=TransactionCostConfig(100, 0, 0, 0, 0))).run(
        frame, "THYAO", _buy_once(stop=90, targets=(120, 130, 140))
    )
    assert paid.final_equity < free.final_equity
    assert paid.trades[0].total_cost > 0


def test_06_slippage_is_applied_on_both_sides():
    frame = _bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100)])
    result = BacktestEngine(_config(transaction_costs=TransactionCostConfig(0, 100, 0, 0, 0))).run(
        frame, "THYAO", _buy_once(stop=90, targets=(120, 130, 140))
    )
    assert result.trades[0].entry_price == pytest.approx(101)
    assert result.trades[0].exit_price == pytest.approx(99)


def test_07_conservative_policy_chooses_stop_when_target_same_bar():
    frame = _bars([(100, 101, 99, 100), (100, 110, 90, 100)])
    result = BacktestEngine(_config(intrabar_policy="conservative")).run(frame, "THYAO", _buy_once())
    assert result.trades[0].exit_reason == "STOP"
    assert not result.trades[0].target_1_hit


def test_08_partial_targets_reduce_position_correctly():
    frame = _bars([
        (100, 101, 99, 100), (100, 106, 99, 105),
        (106, 111, 105, 110), (111, 116, 110, 115),
    ])
    result = BacktestEngine(_config()).run(frame, "THYAO", _buy_once())
    trade = result.trades[0]
    assert [item["quantity"] for item in trade.partial_exits] == pytest.approx([
        trade.quantity * 0.4, trade.quantity * 0.35, trade.quantity * 0.25,
    ])
    assert trade.target_1_hit and trade.target_2_hit and trade.target_3_hit


def test_09_trailing_stop_moves_and_triggers():
    frame = _bars([(100, 101, 99, 100), (100, 110, 100, 108), (108, 109, 98, 99)])
    result = BacktestEngine(_config(trailing_stop_percent=10)).run(
        frame, "THYAO", _buy_once(stop=90, targets=(120, 130, 140))
    )
    assert result.trades[0].exit_reason == "STOP"
    assert result.trades[0].exit_price == pytest.approx(99)


def test_10_maximum_holding_time_exit_works():
    frame = _bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100)])
    result = BacktestEngine(_config(max_holding_bars=2)).run(
        frame, "THYAO", _buy_once(stop=90, targets=(120, 130, 140))
    )
    assert result.trades[0].exit_reason == "TIME_EXIT"
    assert result.trades[0].holding_bars == 2


def test_11_raw_split_adjusts_price_quantity_stop_and_targets():
    frame = _bars(
        [(100, 101, 99, 100), (100, 102, 99, 101), (50, 52, 49, 51)],
        split_factor=[1.0, 1.0, 2.0], price_mode=["raw", "raw", "raw"],
    )
    result = BacktestEngine(_config(price_adjustment_mode="raw")).run(
        frame, "THYAO", _buy_once(stop=90, targets=(120, 130, 140))
    )
    trade = result.trades[0]
    assert trade.entry_price == 50
    assert trade.stop_price == 45
    assert trade.quantity == 400


def test_12_invalid_quality_period_is_rejected():
    frame = _bars([(100, 101, 99, 100), (101, 102, 100, 101), (102, 103, 101, 102)])
    frame.loc[1, "data_quality"] = "INVALID"
    result = BacktestEngine(_config()).run(frame, "THYAO", _buy_once(stop=90, targets=(120, 130, 140)))
    assert result.excluded_periods == [{"timestamp": str(frame.iloc[1].timestamp.tz_convert("Europe/Istanbul")), "reason": "INVALID_DATA"}]


def test_13_benchmark_comparison_is_correct():
    frame = _bars([(100, 101, 99, 100), (100, 101, 99, 100), (100, 101, 99, 100)])
    benchmark = _bars([(100, 101, 99, 100), (105, 106, 104, 105), (110, 111, 109, 110)])
    result = BacktestEngine(_config()).run(
        frame, "THYAO", lambda _: SignalInstruction(), benchmark_bars=benchmark
    )
    assert result.metrics.benchmark_return_percent == pytest.approx(10.0)
    assert result.metrics.alpha_vs_benchmark_percent == pytest.approx(-10.0)


def test_14_maximum_drawdown_is_correct():
    metrics = compute_metrics([100, 120, 90, 95], [], [])
    assert metrics.max_drawdown_percent == -25.0
    assert metrics.drawdown_duration_periods == 2


def test_15_profit_factor_is_correct():
    metrics = compute_metrics([100, 101], [10, 20, -5, -5], [1, 1, 1, 1])
    assert metrics.profit_factor == 3.0


def test_16_mae_and_mfe_are_correct():
    frame = _bars([(100, 101, 99, 100), (100, 110, 90, 105)])
    result = BacktestEngine(_config()).run(frame, "THYAO", _buy_once(stop=80, targets=(120, 130, 140)))
    assert result.trades[0].mfe_percent == pytest.approx(10.0)
    assert result.trades[0].mae_percent == pytest.approx(-10.0)


def test_17_low_trade_count_has_sufficiency_warning():
    frame = _bars([(100, 101, 99, 100), (100, 101, 99, 100)])
    result = BacktestEngine(_config()).run(frame, "THYAO", _buy_once(stop=90, targets=(120, 130, 140)))
    assert not result.metrics.sample_sufficient
    assert "yetersiz" in result.metrics.sample_warning.lower()


def test_67_signal_invalidation_closes_open_position():
    frame = _bars([(100, 101, 99, 100), (100, 102, 97, 98), (98, 99, 97, 98)])
    def provider(context):
        if len(context.bars) == 1:
            return SignalInstruction(
                action="BUY", stop_price=90, targets=(120, 130, 140),
                invalidation_price=99, signal_type="BUY_CANDIDATE",
            )
        return SignalInstruction()
    result = BacktestEngine(_config()).run(frame, "THYAO", provider)
    assert result.trades[0].exit_reason == "INVALIDATED"


def test_completion_flag_is_parsed_strictly_and_can_be_required():
    frame = _bars([(100, 101, 99, 100), (101, 102, 100, 101), (102, 103, 101, 102)])
    frame["is_complete"] = frame["is_complete"].astype(object)
    frame.loc[1, "is_complete"] = "false"
    result = BacktestEngine(_config(require_complete_bar_flag=True)).run(
        frame, "THYAO", _buy_once(stop=90, targets=(120, 130, 140))
    )
    assert any(item["reason"] == "INCOMPLETE_CANDLE" for item in result.excluded_periods)

    with pytest.raises(BacktestValidationError, match="is_complete"):
        BacktestEngine(_config(require_complete_bar_flag=True)).run(
            frame.drop(columns=["is_complete"]),
            "THYAO",
            _buy_once(stop=90, targets=(120, 130, 140)),
        )


def test_commission_tax_is_applied_to_commission_not_full_notional():
    costs = TransactionCostConfig(
        commission_bps=10,
        slippage_bps=0,
        spread_bps=0,
        bsmv_bps=0,
        minimum_cost=0,
        commission_tax_rate=0.05,
    )
    assert costs.cash_cost(100_000) == pytest.approx(105.0)
