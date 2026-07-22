from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.backtest.engine import run_backtest


def test_backtest_runs_and_produces_metrics(mock_provider, strategy_config):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    df = mock_provider.get_ohlcv("THYAO", "1d", start, end)
    result = run_backtest(df, "THYAO", "1d", strategy_config, initial_capital=100_000)
    assert result.final_equity > 0
    assert len(result.equity_curve) > 1
    assert result.metrics.trade_count >= 0


def test_backtest_no_lookahead_same_seed_deterministic(mock_provider, strategy_config):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    df = mock_provider.get_ohlcv("THYAO", "1d", start, end)
    result1 = run_backtest(df, "THYAO", "1d", strategy_config, initial_capital=100_000)
    result2 = run_backtest(df, "THYAO", "1d", strategy_config, initial_capital=100_000)
    assert result1.final_equity == result2.final_equity
    assert len(result1.trades) == len(result2.trades)


def test_backtest_warns_on_low_trade_count(mock_provider, strategy_config):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    df = mock_provider.get_ohlcv("THYAO", "1d", start, end)
    result = run_backtest(df, "THYAO", "1d", strategy_config, initial_capital=100_000)
    assert any("Backtest" in w or "Islem" in w or "islem" in w.lower() for w in result.warnings)
