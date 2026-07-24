import numpy as np
import pandas as pd

from app.analysis.bist_trade_plan import build_bist_trade_plan, format_bist_trade_plan
from app.services.chart_service import delete_chart_file, generate_bist_trade_plan_chart


def _ohlcv(rows=240):
    rng = np.random.default_rng(42)
    base = 85 + np.linspace(0, 30, rows) + np.sin(np.arange(rows) / 7) * 3
    close = base + rng.normal(0, .35, rows)
    open_ = close + rng.normal(0, .45, rows)
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=rows, freq="B", tz="UTC"),
        "open": open_, "high": np.maximum(open_, close) + 1.1,
        "low": np.minimum(open_, close) - 1.1, "close": close,
        "volume": rng.integers(1_000_000, 8_000_000, rows),
    })


def test_plan_has_bidirectional_zones_five_targets_and_layered_stops():
    plan = build_bist_trade_plan(_ohlcv(), "THYAO.IS")
    assert plan.symbol == "THYAO"
    assert len(plan.long.targets) == len(plan.short.targets) == 5
    assert list(plan.long.targets) == sorted(plan.long.targets)
    assert list(plan.short.targets) == sorted(plan.short.targets, reverse=True)
    assert plan.long.stop_conservative < plan.long.stop_standard < plan.long.stop_aggressive < plan.long.entry_low
    assert plan.short.entry_high < plan.short.stop_aggressive < plan.short.stop_standard < plan.short.stop_conservative
    assert all(target > plan.long.entry_high for target in plan.long.targets)
    assert all(target < plan.short.entry_low for target in plan.short.targets)


def test_rich_message_and_chart_include_requested_layers():
    data = _ohlcv()
    plan = build_bist_trade_plan(data, "THYAO")
    message = format_bist_trade_plan(plan)
    assert "ğŸš€ LONG" in message and "ğŸ» SHORT" in message
    assert "TP5" in message and "SL korumacÄ±" in message
    path = generate_bist_trade_plan_chart(data, plan)
    try:
        assert path.endswith(".png")
    finally:
        delete_chart_file(path)


def test_plan_rejects_short_history():
    try:
        build_bist_trade_plan(_ohlcv(30), "ASELS")
    except ValueError as exc:
        assert "60" in str(exc)
    else:
        raise AssertionError("Kisa veri reddedilmeliydi")

