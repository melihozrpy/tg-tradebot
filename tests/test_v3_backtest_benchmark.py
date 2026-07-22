from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.backtest.engine import run_backtest


def test_backtest_includes_xu100_comparison(mock_provider, strategy_config):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    df = mock_provider.get_ohlcv("THYAO", "1d", start, end)
    benchmark_df = mock_provider.get_ohlcv("XU100", "1d", start, end)

    result = run_backtest(
        df, "THYAO", "1d", strategy_config, initial_capital=100_000,
        benchmark_df=benchmark_df, benchmark_symbol="XU100",
    )
    assert result.benchmark_symbol == "XU100"
    assert result.benchmark_return_percent is not None
    assert result.alpha_vs_benchmark_percent is not None
    # alpha = toplam getiri - benchmark getirisi
    total_return = round((result.final_equity / result.initial_capital - 1) * 100, 2)
    assert abs(result.alpha_vs_benchmark_percent - (total_return - result.benchmark_return_percent)) < 0.5


def test_backtest_includes_buy_and_hold(mock_provider, strategy_config):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    df = mock_provider.get_ohlcv("THYAO", "1d", start, end)
    result = run_backtest(df, "THYAO", "1d", strategy_config, initial_capital=100_000)
    assert result.buy_and_hold_return_percent is not None


def test_backtest_without_benchmark_has_none_comparison(mock_provider, strategy_config):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    df = mock_provider.get_ohlcv("THYAO", "1d", start, end)
    result = run_backtest(df, "THYAO", "1d", strategy_config, initial_capital=100_000)
    assert result.benchmark_return_percent is None
    assert result.alpha_vs_benchmark_percent is None
