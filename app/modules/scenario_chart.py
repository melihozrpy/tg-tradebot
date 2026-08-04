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
from app.analysis.indicator_engine import compute_indicator_bundle
from app.analysis.staged_entry import StagedEntryPlan
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


def _projected_candle(ax, x: float, opened: float, closed: float, *, color: str) -> None:
    """Draw an unmistakably hypothetical hollow/dashed candle."""

    spread = max(abs(closed - opened) * 0.35, abs(closed) * 0.002)
    low, high = min(opened, closed) - spread, max(opened, closed) + spread
    ax.vlines(x, low, high, color=color, linewidth=1.0, linestyles=(0, (3, 3)), zorder=7)
    ax.add_patch(
        Rectangle(
            (x - 0.28, min(opened, closed)),
            0.56,
            max(abs(closed - opened), abs(closed) * 0.0003),
            fill=False,
            edgecolor=color,
            linewidth=1.15,
            linestyle=(0, (3, 2)),
            zorder=8,
        )
    )


def generate_scenario_chart(
    df: pd.DataFrame,
    *,
    symbol: str,
    plan: StagedEntryPlan,
    direction: str | None = None,
    output_dir: str | Path = "/tmp/mergen_quant_reports",
    dpi: int = 120,
) -> str:
    """Render a clean PENDING/CONFIRMED chart without current-price entry.

    Only candles, EMA50/EMA200, the selected OB/FVG and entry/SL/TP levels are
    placed on the price panel.  Forward candles are hollow and dashed so they
    cannot be confused with observed market data.  Scenario B is explicitly an
    invalidation/no-trade route, not a fabricated opposite entry.
    """

    source = df.copy()
    source["timestamp"] = pd.to_datetime(source["timestamp"], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        source[column] = pd.to_numeric(source[column], errors="coerce")
    source = source.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
    source = source.sort_values("timestamp").tail(260).reset_index(drop=True)
    chosen = (direction or plan.direction).upper()
    if chosen not in {"LONG", "SHORT", "BULLISH", "BEARISH"}:
        raise ValueError("direction bullish/bearish veya LONG/SHORT olmali.")
    bundle = compute_indicator_bundle(source, symbol=symbol, timeframe="1d")
    data = bundle.frame.tail(55).reset_index(drop=True)

    fig, ax = plt.subplots(figsize=(14.5, 8.4), facecolor=VIVID.background)
    _candles(ax, data)
    style_axes(ax, right_axis=True, grid_alpha=0.13)
    x = pd.Series(range(len(data)), index=data.index)
    for column, label, color in (
        ("ema50", "EMA50", "#4dabf7"),
        ("ema200", "EMA200", "#f59f00"),
    ):
        if column in data and data[column].notna().any():
            ax.plot(x, data[column], color=color, linewidth=1.15, alpha=0.9, label=label, zorder=5)

    zone_color = VIVID.bull if plan.direction == "LONG" else VIVID.bear
    zone_kind_color = "#ffb000" if plan.zone_kind.upper() == "FVG" else zone_color
    ax.axhspan(plan.zone_low, plan.zone_high, color=zone_kind_color, alpha=0.16, zorder=1)
    ax.axhline(plan.zone_low, color=zone_kind_color, linewidth=0.8, alpha=0.8)
    ax.axhline(plan.zone_high, color=zone_kind_color, linewidth=0.8, alpha=0.8)
    ax.text(
        len(data) * 0.02,
        plan.zone_high,
        f"{plan.zone_kind}  {plan.zone_low:.2f}-{plan.zone_high:.2f}",
        color=zone_kind_color,
        fontsize=9,
        fontweight="bold",
        va="bottom",
        bbox={"boxstyle": "round,pad=0.22", "facecolor": VIVID.background, "edgecolor": zone_kind_color, "alpha": 0.88},
        zorder=10,
    )

    planned_entry = plan.structural_entry
    levels = (
        (planned_entry, "ENTRY", "#ffd43b", "-"),
        (plan.invalidation, "SL", VIVID.bear, (0, (5, 4))),
        (plan.target_1, "TP1", VIVID.bull, (0, (5, 4))),
        (plan.target_2, "TP2", VIVID.bull, (0, (2, 4))),
    )
    for value, label, color, linestyle in levels:
        if value is None:
            continue
        ax.axhline(value, color=color, linewidth=1.15, linestyle=linestyle, alpha=0.92, zorder=4)
        ax.text(
            1.003,
            value,
            f"{label} {value:.2f}",
            transform=ax.get_yaxis_transform(),
            ha="left",
            va="center",
            color="#0d1117" if label == "ENTRY" else "#ffffff",
            fontsize=8.5,
            fontweight="bold",
            bbox={"boxstyle": "round,pad=0.28", "facecolor": color, "edgecolor": color, "alpha": 0.96},
            clip_on=False,
            zorder=10,
        )

    current = float(data.iloc[-1]["close"])
    last_x = len(data) - 1
    target = plan.target_1 if plan.target_1 is not None else planned_entry
    main_path = [current, planned_entry, (planned_entry + target) / 2.0, target]
    counter_path = [current, plan.invalidation, plan.invalidation]
    for offset in range(1, len(main_path)):
        _projected_candle(ax, last_x + offset * 1.25, main_path[offset - 1], main_path[offset], color=zone_color)
    ax.plot(
        [last_x + index * 1.25 for index in range(len(main_path))],
        main_path,
        color=zone_color,
        linestyle=(0, (4, 4)),
        linewidth=1.2,
        alpha=0.85,
        label="Senaryo A (varsayimsal)",
        zorder=6,
    )
    ax.plot(
        [last_x, last_x + 1.5, last_x + 3.0],
        counter_path,
        color=VIVID.bear if plan.direction == "LONG" else VIVID.bull,
        linestyle=(0, (2, 4)),
        linewidth=1.0,
        alpha=0.72,
        label="Senaryo B: invalidation / islem yok",
        zorder=6,
    )

    status_color = VIVID.bull if plan.status == "CONFIRMED" else "#ffb000"
    status_text = (
        "CONFIRMED • zone + yapi + confluence hazir"
        if plan.status == "CONFIRMED"
        else f"PENDING • su an entry YOK • {planned_entry:.2f} bekleniyor"
    )
    ax.set_title(
        f"{plan.symbol} • 1D • KADEMELI {plan.direction} SENARYOSU\n{status_text}",
        loc="left",
        color=status_color,
        fontsize=15,
        fontweight="bold",
        pad=12,
    )
    ax.legend(loc="upper left", frameon=False, labelcolor=VIVID.text, fontsize=8, ncol=2)
    ax.set_xlim(-1, len(data) + 5.5)
    visible = [float(data["low"].min()), float(data["high"].max()), plan.zone_low, plan.zone_high, plan.invalidation]
    visible.extend(value for value in (plan.target_1, plan.target_2) if value is not None)
    low, high = min(visible), max(visible)
    pad = max((high - low) * 0.1, current * 0.01)
    ax.set_ylim(max(0, low - pad), high + pad)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value:.2f}"))
    ticks = list(range(0, len(data), max(1, len(data) // 6)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([data.iloc[index]["timestamp"].strftime("%d %b") for index in ticks])
    add_price_card(fig, current, status_color, decimals=2, x=0.93, y=0.925)
    fig.text(
        0.055,
        0.025,
        "Kesik/ici bos mumlar varsayimsal projeksiyondur; gercek piyasa mumu degildir. Son kapanis entry olarak kullanilmaz.",
        color=VIVID.muted,
        fontsize=8.3,
    )
    add_watermark(fig)
    fig.subplots_adjust(left=0.055, right=0.89, top=0.86, bottom=0.11)

    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_symbol = re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol)
    output = target_dir / f"staged_scenario_{safe_symbol}_{uuid.uuid4().hex[:10]}.png"
    fig.savefig(output, facecolor=VIVID.background, dpi=max(100, int(dpi)), bbox_inches="tight")
    plt.close(fig)
    return str(output)
