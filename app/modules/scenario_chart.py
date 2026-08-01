from __future__ import annotations

"""Koşullu kırılım senaryoları için Telegram uyumlu mini mum grafiği."""

import re
import uuid
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import Rectangle
from matplotlib.ticker import FuncFormatter

from app.analysis.breakout_scenario_engine import BreakoutCase, BreakoutScenarioResult
from app.services.vivid_chart_style import VIVID, add_price_card, add_watermark, style_axes


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    required = {"timestamp", "open", "high", "low", "close"}
    if not required.issubset(df.columns):
        raise ValueError("Senaryo grafiği için timestamp/open/high/low/close gerekir.")
    data = df.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=list(required)).sort_values("timestamp").tail(55).reset_index(drop=True)
    if len(data) < 10:
        raise ValueError("Senaryo grafiği için en az 10 geçerli mum gerekir.")
    return data


def _candles(ax, data: pd.DataFrame) -> None:
    for index, row in data.iterrows():
        opened = float(row["open"])
        closed = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        color = VIVID.bull if closed >= opened else VIVID.bear
        ax.vlines(index, low, high, color=color, linewidth=0.9, zorder=3)
        body_low = min(opened, closed)
        body_height = max(abs(closed - opened), max(abs(closed) * 0.00025, 1e-8))
        ax.add_patch(
            Rectangle(
                (index - 0.3, body_low),
                0.6,
                body_height,
                facecolor=color,
                edgecolor=color,
                linewidth=0.5,
                zorder=4,
            )
        )


def _scenario_panel(ax, data: pd.DataFrame, case: BreakoutCase | None, *, bullish: bool) -> None:
    _candles(ax, data)
    style_axes(ax, right_axis=True, grid_alpha=0.28)
    color = VIVID.bull if bullish else VIVID.bear
    title = "SENARYO 1 • BULLISH" if bullish else "SENARYO 2 • BEARISH"
    ax.set_title(title, loc="left", fontsize=12, fontweight="bold", color=color, pad=10)
    if case is None:
        ax.text(
            0.5,
            0.5,
            "Doğrulanmış seviye yok",
            transform=ax.transAxes,
            ha="center",
            color=VIVID.muted,
            fontsize=11,
        )
        return

    lower, upper = sorted((case.level_low, case.level_high))
    ax.axhspan(lower, upper, color=color, alpha=0.13, zorder=1)
    zone_label = "DİRENÇ / BREAKOUT" if bullish else "DESTEK / BREAKDOWN"
    ax.text(
        0.015,
        upper if bullish else lower,
        f"{zone_label}  {lower:.2f}-{upper:.2f}",
        transform=ax.get_yaxis_transform(),
        color=color,
        fontsize=8.2,
        fontweight="bold",
        va="bottom" if bullish else "top",
    )
    ax.axhline(case.confirmation_close_level, color=color, linestyle=(0, (6, 4)), linewidth=1.25)
    target_values = [value for value in (case.target_1, case.target_2) if value is not None]
    target_reasons = [case.target_1_reason, case.target_2_reason]
    for index, (target, reason) in enumerate(zip(target_values, target_reasons), 1):
        ax.axhline(target, color=color, linestyle=(0, (2, 4)), linewidth=0.9, alpha=0.85)
        ax.text(
            1.005,
            target,
            f"TP{index} {target:.2f}\n{reason}",
            transform=ax.get_yaxis_transform(),
            ha="left",
            va="center",
            color="#ffffff",
            fontsize=7.5,
            bbox={"boxstyle": "round,pad=0.25", "facecolor": color, "edgecolor": color, "alpha": 0.9},
            clip_on=False,
        )

    current = float(data.iloc[-1]["close"])
    x_last = len(data) - 1
    target = case.target_1 if case.target_1 is not None else case.confirmation_close_level
    ax.annotate(
        "KOŞULLU YOL",
        xy=(x_last + 5.5, target),
        xytext=(x_last, current),
        color=color,
        fontsize=8,
        fontweight="bold",
        arrowprops={"arrowstyle": "-|>", "color": color, "lw": 2.0, "connectionstyle": "arc3,rad=0.12"},
    )
    ax.axhline(current, color="#ffffff", linewidth=0.65, alpha=0.4)
    ax.text(
        1.005,
        current,
        f"GÜNCEL {current:.2f}",
        transform=ax.get_yaxis_transform(),
        ha="left",
        va="center",
        color="#ffffff",
        fontsize=7.8,
        fontweight="bold",
        bbox={"boxstyle": "round,pad=0.25", "facecolor": VIVID.panel_alt, "edgecolor": color, "alpha": 0.95},
        clip_on=False,
    )

    visible_values = [float(data["low"].min()), float(data["high"].max()), lower, upper, *target_values]
    low, high = min(visible_values), max(visible_values)
    pad = max((high - low) * 0.12, current * 0.01)
    ax.set_ylim(max(0, low - pad), high + pad)
    ax.set_xlim(-1, len(data) + 7)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value:.2f}"))
    ticks = list(range(0, len(data), max(1, len(data) // 5)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([data.iloc[index]["timestamp"].strftime("%d %b") for index in ticks])


def render_breakout_scenario_chart(
    df: pd.DataFrame,
    *,
    symbol: str,
    result: BreakoutScenarioResult,
    output_dir: str | Path = "/tmp/mergen_quant_reports",
    dpi: int = 120,
) -> str:
    """Gerçek mumları ve iki *koşullu* yolu yan yana PNG olarak üretir."""
    data = _normalise(df)
    fig, axes = plt.subplots(1, 2, figsize=(15, 7.6), facecolor=VIVID.background)
    _scenario_panel(axes[0], data, result.resistance_breakout, bullish=True)
    _scenario_panel(axes[1], data, result.support_breakdown, bullish=False)
    date_label = data.iloc[-1]["timestamp"].strftime("%d.%m.%Y")
    fig.suptitle(
        f"{symbol.upper().removesuffix('.IS')} • 1D • {date_label}  |  KOŞULLU KIRILIM HARİTASI",
        color=VIVID.text,
        fontsize=18,
        fontweight="bold",
        x=0.055,
        ha="left",
    )
    add_price_card(fig, float(data.iloc[-1]["close"]), VIVID.bull, decimals=2, x=0.94, y=0.94)
    fig.text(
        0.055,
        0.02,
        "Mum kapanışı + hacim teyidi olmadan senaryo aktif değildir. Çizilen ileri yol fiyat tahmini değil, koşullu rota gösterimidir.",
        color=VIVID.muted,
        fontsize=8.5,
    )
    add_watermark(fig)
    fig.subplots_adjust(left=0.055, right=0.91, top=0.87, bottom=0.11, wspace=0.27)

    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_symbol = re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol)
    output = target_dir / f"breakout_{safe_symbol}_{uuid.uuid4().hex[:10]}.png"
    fig.savefig(output, facecolor=VIVID.background, dpi=max(100, int(dpi)), bbox_inches="tight")
    plt.close(fig)
    return str(output)
