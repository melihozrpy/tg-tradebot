from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Iterable, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from app.backtest.engine_v5g import BacktestResultV5G
from app.services.vivid_chart_style import VIVID, add_score_bar, add_watermark, style_axes


def _temporary_chart_path(label: str) -> Path:
    directory = Path(tempfile.gettempdir()) / "mergen_quant_stage5g_charts"
    directory.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix=f"{label}_", suffix=".png", dir=directory, delete=False)
    handle.close()
    return Path(handle.name)


def _style_backtest_figure(fig, axes) -> None:
    fig.patch.set_facecolor(VIVID.background)
    for axis in axes:
        style_axes(axis)
    add_watermark(fig)


def generate_equity_chart(result: BacktestResultV5G) -> Path:
    path = _temporary_chart_path("equity")
    timestamps = pd.to_datetime([item.timestamp for item in result.equity_points])
    equity = np.asarray([item.equity for item in result.equity_points], dtype=float)
    benchmark = [item.benchmark_equity for item in result.equity_points]
    running_max = np.maximum.accumulate(equity) if len(equity) else np.array([])
    drawdown = (equity / running_max - 1.0) * 100.0 if len(equity) else np.array([])

    fig, (ax, dd_ax) = plt.subplots(
        2, 1, figsize=(14, 8), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]}, facecolor=VIVID.background,
    )
    _style_backtest_figure(fig, (ax, dd_ax))
    ax.plot(timestamps, equity, label="Strateji sermayesi", color=VIVID.bull, linewidth=2.1)
    if len(equity):
        ax.fill_between(timestamps, equity, float(np.nanmin(equity)), color=VIVID.bull, alpha=.08)
    if benchmark and any(value is not None for value in benchmark):
        values = [np.nan if value is None else value for value in benchmark]
        ax.plot(timestamps, values, label="XU100 benchmark", color=VIVID.muted, linewidth=1.25)
    timestamp_index = pd.Index(timestamps)
    for trade in result.trades:
        if trade.entry_time is not None and len(timestamp_index):
            index = int(np.argmin(np.abs(timestamp_index - pd.Timestamp(trade.entry_time))))
            ax.scatter(timestamps[index], equity[index], marker="^", color=VIVID.bull, s=32, zorder=5)
        if trade.exit_time is not None and len(timestamp_index):
            index = int(np.argmin(np.abs(timestamp_index - pd.Timestamp(trade.exit_time))))
            ax.scatter(timestamps[index], equity[index], marker="v", color=VIVID.bear, s=32, zorder=5)
    ax.set_title(f"MONTANA FİNANS ROBOTU  •  {result.symbol}  •  EQUITY VE XU100", loc="left", fontsize=16, fontweight="bold")
    ax.set_ylabel("Sermaye (TRY)")
    ax.legend(loc="best", facecolor=VIVID.panel_alt, edgecolor=VIVID.grid, labelcolor=VIVID.text)
    dd_ax.fill_between(timestamps, drawdown, 0, where=drawdown < 0, color=VIVID.bear, alpha=0.35)
    dd_ax.plot(timestamps, drawdown, color=VIVID.bear, linewidth=1)
    dd_ax.set_ylabel("DD %")
    if len(equity) and equity[0]:
        return_score = max(0, min(100, 50 + (equity[-1] / equity[0] - 1) * 100))
        add_score_bar(fig, return_score, label="BACKTEST GÜVENİ")
    fig.subplots_adjust(left=.07, right=.94, top=.90, bottom=.11, hspace=.08)
    fig.savefig(path, dpi=140, bbox_inches="tight", facecolor=VIVID.background)
    plt.close(fig)
    return path


def generate_monthly_returns_chart(result: BacktestResultV5G) -> Path:
    path = _temporary_chart_path("monthly")
    series = pd.Series(
        [item.equity for item in result.equity_points],
        index=pd.to_datetime([item.timestamp for item in result.equity_points]),
        dtype=float,
    )
    monthly = series.resample("ME").last().pct_change().dropna() * 100.0 if not series.empty else pd.Series(dtype=float)
    colors = [VIVID.bull if value >= 0 else VIVID.bear for value in monthly]
    fig, ax = plt.subplots(figsize=(13, 6), facecolor=VIVID.background)
    _style_backtest_figure(fig, (ax,))
    labels = [item.strftime("%Y-%m") for item in monthly.index]
    ax.bar(labels, monthly.values, color=colors)
    ax.axhline(0, color=VIVID.muted, linewidth=0.8)
    ax.set_title("MONTANA FİNANS ROBOTU  •  AYLIK NET GETİRİ", loc="left", fontsize=16, fontweight="bold")
    ax.set_ylabel("Getiri %")
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    fig.savefig(path, dpi=140, bbox_inches="tight", facecolor=VIVID.background)
    plt.close(fig)
    return path


def generate_calibration_chart(bins: Iterable[object]) -> Path:
    path = _temporary_chart_path("calibration")
    items = list(bins)
    labels = [f"{item.score_min}-{item.score_max}" for item in items]
    observed = [item.observed_success_rate for item in items]
    calibrated = [item.calibrated_success_rate for item in items]
    x = np.arange(len(items))
    fig, ax = plt.subplots(figsize=(13, 6), facecolor=VIVID.background)
    _style_backtest_figure(fig, (ax,))
    ax.bar(x - 0.18, observed, width=0.36, label="Gerçekleşen", color=VIVID.cyan)
    ax.bar(x + 0.18, calibrated, width=0.36, label="Kalibre", color=VIVID.amber)
    ax.set_xticks(x, labels)
    ax.set_ylim(0, 100)
    ax.set_ylabel("Basari %")
    ax.set_title("MONTANA FİNANS ROBOTU  •  PUANA GÖRE TARİHSEL BAŞARI", loc="left", fontsize=16, fontweight="bold")
    ax.legend(facecolor=VIVID.panel_alt, edgecolor=VIVID.grid, labelcolor=VIVID.text)
    fig.tight_layout()
    fig.savefig(path, dpi=140, bbox_inches="tight", facecolor=VIVID.background)
    plt.close(fig)
    return path


def generate_outcome_distribution_chart(result: BacktestResultV5G) -> Path:
    path = _temporary_chart_path("outcomes")
    labels = ["Hedef 1", "Hedef 2", "Hedef 3", "Stop", "Zaman"]
    values = [
        sum(item.target_1_hit for item in result.trades),
        sum(item.target_2_hit for item in result.trades),
        sum(item.target_3_hit for item in result.trades),
        sum(item.exit_reason == "STOP" for item in result.trades),
        sum(item.exit_reason == "TIME_EXIT" for item in result.trades),
    ]
    fig, ax = plt.subplots(figsize=(13, 6), facecolor=VIVID.background)
    _style_backtest_figure(fig, (ax,))
    ax.bar(labels, values, color=[VIVID.bull, "#22c55e", "#16a34a", VIVID.bear, VIVID.muted])
    ax.set_title("MONTANA FİNANS ROBOTU  •  HEDEF / STOP DAĞILIMI", loc="left", fontsize=16, fontweight="bold")
    ax.set_ylabel("Islem")
    fig.tight_layout()
    fig.savefig(path, dpi=140, bbox_inches="tight", facecolor=VIVID.background)
    plt.close(fig)
    return path


def generate_backtest_charts(
    result: BacktestResultV5G,
    *,
    calibration_bins: Optional[Iterable[object]] = None,
) -> list[Path]:
    paths = [
        generate_equity_chart(result),
        generate_monthly_returns_chart(result),
        generate_outcome_distribution_chart(result),
    ]
    if calibration_bins is not None:
        paths.append(generate_calibration_chart(calibration_bins))
    return paths


def generate_persisted_equity_chart(symbol: str, points: Iterable[object]) -> Path:
    path = _temporary_chart_path("equity_saved")
    items = list(points)
    timestamps = pd.to_datetime([item.trading_date for item in items])
    strategy = [item.strategy_equity for item in items]
    benchmark = [np.nan if item.benchmark_equity is None else item.benchmark_equity for item in items]
    fig, (ax, dd_ax) = plt.subplots(
        2, 1, figsize=(14, 8), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]}, facecolor=VIVID.background,
    )
    _style_backtest_figure(fig, (ax, dd_ax))
    ax.plot(timestamps, strategy, label="Strateji", color=VIVID.bull, linewidth=2)
    if any(not np.isnan(value) for value in benchmark):
        ax.plot(timestamps, benchmark, label="XU100", color=VIVID.muted)
    equity = np.asarray(strategy, dtype=float)
    drawdown = (equity / np.maximum.accumulate(equity) - 1.0) * 100.0 if len(equity) else np.array([])
    dd_ax.fill_between(timestamps, drawdown, 0, color=VIVID.bear, alpha=0.35)
    dd_ax.set_ylabel("DD %")
    ax.set_title(f"MONTANA FİNANS ROBOTU  •  {symbol}  •  KAYITLI BACKTEST EQUITY", loc="left", fontsize=16, fontweight="bold")
    ax.set_ylabel("TRY")
    ax.legend(facecolor=VIVID.panel_alt, edgecolor=VIVID.grid, labelcolor=VIVID.text)
    fig.subplots_adjust(left=.07, right=.94, top=.90, bottom=.09, hspace=.08)
    fig.savefig(path, dpi=140, bbox_inches="tight", facecolor=VIVID.background)
    plt.close(fig)
    return path


def delete_backtest_chart(path: str | Path) -> bool:
    target = Path(path)
    try:
        target.unlink(missing_ok=True)
        return not target.exists()
    except OSError:
        return False
