from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class BacktestMetrics:
    total_return_percent: float
    annualized_return_percent: float
    max_drawdown_percent: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    win_rate_percent: float
    profit_factor: float
    expected_value: float
    average_win: float
    average_loss: float
    longest_losing_streak: int
    trade_count: int
    average_holding_period_days: float
    final_equity: float = 0.0
    benchmark_return_percent: float | None = None
    alpha_vs_benchmark_percent: float | None = None
    losing_rate_percent: float = 0.0
    win_loss_ratio: float = 0.0
    drawdown_duration_periods: int = 0
    volatility_percent: float = 0.0
    market_exposure_percent: float = 0.0
    average_mae_percent: float = 0.0
    average_mfe_percent: float = 0.0
    target_1_hit_rate_percent: float = 0.0
    target_2_hit_rate_percent: float = 0.0
    target_3_hit_rate_percent: float = 0.0
    stop_rate_percent: float = 0.0
    timeout_exit_rate_percent: float = 0.0
    longest_winning_streak: int = 0
    sample_sufficient: bool = False
    sample_warning: str | None = None


def _drawdown_series(equity_curve: list[float]) -> np.ndarray:
    equity = np.array(equity_curve, dtype=float)
    running_max = np.maximum.accumulate(equity)
    drawdown = (equity - running_max) / running_max
    return drawdown


def compute_metrics(
    equity_curve: list[float],
    trade_pnls: list[float],
    holding_periods_days: list[float],
    periods_per_year: int = 252,
    *,
    trade_details: list[object] | None = None,
    exposure_periods: int = 0,
    minimum_sample_size: int = 20,
    benchmark_return_percent: float | None = None,
) -> BacktestMetrics:
    if len(equity_curve) < 2:
        return BacktestMetrics(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    equity = np.array(equity_curve, dtype=float)
    returns = np.diff(equity) / equity[:-1]
    total_return = (equity[-1] / equity[0] - 1) * 100

    n_periods = len(equity) - 1
    years = max(n_periods / periods_per_year, 1e-9)
    annualized_return = ((equity[-1] / equity[0]) ** (1 / years) - 1) * 100 if equity[0] > 0 else 0.0

    drawdown = _drawdown_series(equity_curve)
    max_dd = float(np.min(drawdown)) * 100
    drawdown_duration = 0
    current_drawdown_duration = 0
    for value in drawdown:
        if value < 0:
            current_drawdown_duration += 1
            drawdown_duration = max(drawdown_duration, current_drawdown_duration)
        else:
            current_drawdown_duration = 0

    mean_return = float(np.mean(returns)) if len(returns) else 0.0
    std_return = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0.0
    sharpe = (mean_return / std_return) * np.sqrt(periods_per_year) if std_return > 0 else 0.0

    downside_returns = returns[returns < 0]
    downside_std = float(np.std(downside_returns, ddof=1)) if len(downside_returns) > 1 else 0.0
    sortino = (mean_return / downside_std) * np.sqrt(periods_per_year) if downside_std > 0 else 0.0

    calmar = (annualized_return / abs(max_dd)) if max_dd != 0 else 0.0

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    win_rate = (len(wins) / len(trade_pnls) * 100) if trade_pnls else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    expected_value = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)

    longest_streak = 0
    current_streak = 0
    for pnl in trade_pnls:
        if pnl < 0:
            current_streak += 1
            longest_streak = max(longest_streak, current_streak)
        else:
            current_streak = 0

    longest_winning_streak = 0
    current_winning_streak = 0
    for pnl in trade_pnls:
        if pnl > 0:
            current_winning_streak += 1
            longest_winning_streak = max(longest_winning_streak, current_winning_streak)
        else:
            current_winning_streak = 0

    avg_holding = float(np.mean(holding_periods_days)) if holding_periods_days else 0.0

    details = trade_details or []
    trade_count = len(trade_pnls)
    losing_rate = (len(losses) / trade_count * 100) if trade_count else 0.0
    win_loss_ratio = (avg_win / abs(avg_loss)) if avg_loss < 0 else (float("inf") if avg_win > 0 else 0.0)
    avg_mae = float(np.mean([float(getattr(t, "mae_percent", 0.0)) for t in details])) if details else 0.0
    avg_mfe = float(np.mean([float(getattr(t, "mfe_percent", 0.0)) for t in details])) if details else 0.0

    def _rate(attr: str) -> float:
        return (sum(bool(getattr(t, attr, False)) for t in details) / len(details) * 100) if details else 0.0

    stop_rate = (
        sum(str(getattr(t, "exit_reason", "")).upper() == "STOP" for t in details) / len(details) * 100
        if details else 0.0
    )
    timeout_rate = (
        sum(str(getattr(t, "exit_reason", "")).upper() in {"TIME_EXIT", "MAX_HOLDING"} for t in details)
        / len(details) * 100 if details else 0.0
    )
    volatility = std_return * np.sqrt(periods_per_year) * 100 if std_return > 0 else 0.0
    exposure = exposure_periods / max(n_periods, 1) * 100
    sample_sufficient = trade_count >= minimum_sample_size
    warning = None if sample_sufficient else "Istatistiksel degerlendirme icin islem sayisi yetersiz."
    alpha = total_return - benchmark_return_percent if benchmark_return_percent is not None else None

    return BacktestMetrics(
        total_return_percent=round(total_return, 2),
        annualized_return_percent=round(annualized_return, 2),
        max_drawdown_percent=round(max_dd, 2),
        sharpe_ratio=round(sharpe, 2),
        sortino_ratio=round(sortino, 2),
        calmar_ratio=round(calmar, 2),
        win_rate_percent=round(win_rate, 2),
        profit_factor=round(profit_factor, 2) if profit_factor != float("inf") else profit_factor,
        expected_value=round(expected_value, 2),
        average_win=round(avg_win, 2),
        average_loss=round(avg_loss, 2),
        longest_losing_streak=longest_streak,
        trade_count=len(trade_pnls),
        average_holding_period_days=round(avg_holding, 2),
        final_equity=round(float(equity[-1]), 2),
        benchmark_return_percent=(round(benchmark_return_percent, 2) if benchmark_return_percent is not None else None),
        alpha_vs_benchmark_percent=(round(alpha, 2) if alpha is not None else None),
        losing_rate_percent=round(losing_rate, 2),
        win_loss_ratio=round(win_loss_ratio, 2) if win_loss_ratio != float("inf") else win_loss_ratio,
        drawdown_duration_periods=drawdown_duration,
        volatility_percent=round(float(volatility), 2),
        market_exposure_percent=round(exposure, 2),
        average_mae_percent=round(avg_mae, 2),
        average_mfe_percent=round(avg_mfe, 2),
        target_1_hit_rate_percent=round(_rate("target_1_hit"), 2),
        target_2_hit_rate_percent=round(_rate("target_2_hit"), 2),
        target_3_hit_rate_percent=round(_rate("target_3_hit"), 2),
        stop_rate_percent=round(stop_rate, 2),
        timeout_exit_rate_percent=round(timeout_rate, 2),
        longest_winning_streak=longest_winning_streak,
        sample_sufficient=sample_sufficient,
        sample_warning=warning,
    )
