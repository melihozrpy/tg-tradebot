from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from app.backtest.engine_v5g import BacktestConfig, SignalInstruction, TransactionCostConfig
from app.backtest.walk_forward import WalkForwardConfig, WalkForwardEngine


def _long_bars(days=500):
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return pd.DataFrame([
        {
            "timestamp": start + timedelta(days=i), "open": 100 + i * 0.01,
            "high": 101 + i * 0.01, "low": 99 + i * 0.01,
            "close": 100 + i * 0.01, "volume": 1000,
            "is_complete": True, "data_quality": "VALID", "price_mode": "adjusted",
        }
        for i in range(days)
    ])


def _wf(mode="rolling"):
    return WalkForwardEngine(WalkForwardConfig(
        train_days=100, validation_days=30, test_days=30, step_days=30, mode=mode, seed=7,
    ))


def _bt_config():
    return BacktestConfig(
        minimum_history_bars=1, max_holding_bars=3,
        transaction_costs=TransactionCostConfig(0, 0, 0, 0, 0),
    )


def test_18_training_validation_test_dates_do_not_overlap():
    for window in _wf().split(_long_bars()):
        assert window.train_end <= window.validation_start
        assert window.validation_end <= window.test_start


def test_19_test_data_is_not_passed_to_parameter_selector():
    seen = []
    def selector(train, validation):
        seen.append((train.timestamp.max(), validation.timestamp.max()))
        return {}
    result = _wf().run(
        _long_bars(), "THYAO", lambda _: SignalInstruction(),
        backtest_config=_bt_config(), parameter_selector=selector,
    )
    assert all(validation_max < pd.Timestamp(window.test_start) for (_, validation_max), window in zip(seen, result.windows))


def test_20_rolling_window_moves_training_start():
    windows = _wf("rolling").split(_long_bars())
    assert len(windows) > 2
    assert windows[1].train_start > windows[0].train_start


def test_21_expanding_window_keeps_training_start():
    windows = _wf("expanding").split(_long_bars())
    assert len(windows) > 2
    assert windows[0].train_start == windows[1].train_start
    assert windows[1].train_end > windows[0].train_end


def test_22_out_of_sample_results_are_separate_and_flagged():
    result = _wf().run(_long_bars(), "THYAO", lambda _: SignalInstruction(), backtest_config=_bt_config())
    assert result.out_of_sample_results
    assert all(item.out_of_sample for item in result.out_of_sample_results)
    assert all(window.out_of_sample_result is not None for window in result.windows)


def test_23_same_config_and_data_produce_same_walk_forward_result():
    one = _wf().run(_long_bars(), "THYAO", lambda _: SignalInstruction(), backtest_config=_bt_config())
    two = _wf().run(_long_bars(), "THYAO", lambda _: SignalInstruction(), backtest_config=_bt_config())
    assert one.run_id == two.run_id
    assert [item.run_id for item in one.out_of_sample_results] == [item.run_id for item in two.out_of_sample_results]
