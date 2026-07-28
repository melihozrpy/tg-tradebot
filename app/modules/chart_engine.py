from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import Rectangle
from matplotlib.ticker import FuncFormatter

from app.analysis.smart_money_engine import SmartMoneyResult
from app.services.vivid_chart_style import (
    VIVID,
    add_checklist as add_vivid_checklist,
    add_price_card,
    add_score_bar,
    add_watermark,
)


BG = VIVID.background
PANEL = VIVID.panel_alt
GRID = VIVID.grid
TEXT = VIVID.text
MUTED = VIVID.muted
BULL = VIVID.bull
BEAR = VIVID.bear
ENTRY = VIVID.blue
TP = "#22c55e"
SL = VIVID.bear
OB_BULL = "#14b8a6"
OB_BEAR = "#fb7185"
FVG_BULL = VIVID.cyan
FVG_BEAR = VIVID.amber


@dataclass(frozen=True)
class ChecklistVisual:
    label: str
    passed: bool


@dataclass(frozen=True)
class NewsTimelineItem:
    time: str
    title: str
    impact: str = "medium"


@dataclass(frozen=True)
class ReportChartSpec:
    instrument: str
    timeframe: str
    report_kind: str  # morning | evening | analysis
    direction: str
    sentiment_score: float
    change_percent: float | None = None
    checklist: Sequence[ChecklistVisual] = field(default_factory=tuple)
    entry_low: float | None = None
    entry_high: float | None = None
    stop: float | None = None
    targets: Sequence[float] = field(default_factory=tuple)
    rr: float | None = None
    liquidity_levels: Sequence[tuple[float, str]] = field(default_factory=tuple)
    news_timeline: Sequence[NewsTimelineItem] = field(default_factory=tuple)
    date_label: str = ""


def _price_decimals(symbol: str) -> int:
    clean = symbol.upper().removesuffix(".IS")
    if clean in {"EURUSD", "GBPUSD", "USDJPY"} or clean.endswith("=X"):
        return 5
    return 2


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "report"


def _normalise_frame(df: pd.DataFrame) -> pd.DataFrame:
    required = {"timestamp", "open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Grafik için eksik OHLC kolonları: {sorted(missing)}")
    data = df.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["timestamp", "open", "high", "low", "close"])
    data = data.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)
    if len(data) < 10:
        raise ValueError("Grafik için en az 10 geçerli mum gerekir.")
    return data


def _draw_candles(ax, data: pd.DataFrame) -> None:
    width = 0.66
    for index, row in data.iterrows():
        opened = float(row["open"])
        closed = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        color = BULL if closed >= opened else BEAR
        ax.vlines(index, low, high, color=color, linewidth=1.1, alpha=0.95, zorder=3)
        body_low = min(opened, closed)
        body_height = max(abs(closed - opened), max(abs(closed) * 0.00025, 1e-8))
        ax.add_patch(
            Rectangle(
                (index - width / 2, body_low), width, body_height,
                facecolor=color, edgecolor=color, linewidth=0.7, zorder=4,
            )
        )


def _label_price(ax, price: float, text: str, color: str, decimals: int, *, x: float = 1.002) -> None:
    ax.annotate(
        f"{text}  {price:.{decimals}f}",
        xy=(x, price),
        xycoords=("axes fraction", "data"),
        xytext=(8, 0),
        textcoords="offset points",
        ha="left",
        va="center",
        fontsize=9.2,
        fontweight="bold",
        color="#ffffff",
        bbox={"boxstyle": "round,pad=0.32", "facecolor": color, "edgecolor": color, "alpha": 0.92},
        clip_on=False,
        zorder=20,
    )


def _draw_zone(
    ax,
    low: float,
    high: float,
    label: str,
    color: str,
    *,
    x_start: float,
    x_end: float,
    alpha: float = 0.16,
    decimals: int = 2,
) -> None:
    lower, upper = sorted((float(low), float(high)))
    if not math.isfinite(lower) or not math.isfinite(upper) or upper <= 0:
        return
    height = max(upper - lower, max(upper * 0.0003, 1e-8))
    ax.add_patch(
        Rectangle(
            (x_start, lower), max(0.5, x_end - x_start), height,
            facecolor=color, edgecolor=color, linewidth=1.0, alpha=alpha, zorder=1,
        )
    )
    ax.text(
        x_start + 0.5,
        upper,
        f"{label}  {lower:.{decimals}f}-{upper:.{decimals}f}",
        color=color,
        fontsize=8.5,
        fontweight="bold",
        va="bottom",
        zorder=8,
    )


def _draw_smart_money(ax, smart: SmartMoneyResult | None, *, offset: int, length: int, decimals: int) -> None:
    if smart is None:
        return
    x_end = length - 0.2
    for zone in (*smart.order_blocks[-3:], *smart.fvg[-3:]):
        local_index = max(0.0, float((zone.origin_index if zone.origin_index is not None else zone.index) - offset))
        if local_index > length:
            continue
        if zone.kind == "OB":
            color = OB_BULL if zone.direction == "bullish" else OB_BEAR
        else:
            color = FVG_BULL if zone.direction == "bullish" else FVG_BEAR
        _draw_zone(
            ax, zone.low, zone.high, zone.kind, color,
            x_start=local_index, x_end=x_end, alpha=0.13, decimals=decimals,
        )

    for event in smart.structure[-5:]:
        local_index = event.index - offset
        if not 0 <= local_index < length:
            continue
        color = BULL if event.direction == "bullish" else BEAR
        marker = "▲" if event.direction == "bullish" else "▼"
        ax.annotate(
            f"{marker} {event.kind}",
            xy=(local_index, event.price),
            xytext=(0, 18 if event.direction == "bullish" else -22),
            textcoords="offset points",
            ha="center",
            va="center",
            fontsize=9,
            fontweight="bold",
            color=color,
            arrowprops={"arrowstyle": "-|>", "color": color, "lw": 1.1},
            zorder=15,
        )


def _draw_sentiment_bar(fig, score: float) -> None:
    add_score_bar(fig, score, label="PİYASA GÜVENİ", left=0.105, bottom=0.025, width=0.49)


def _draw_checklist(fig, checklist: Sequence[ChecklistVisual]) -> None:
    if not checklist:
        return
    add_vivid_checklist(
        fig,
        [(item.label, item.passed) for item in checklist],
        title="SMXM CHECKLIST",
        x=0.705,
        y=0.245,
    )


def _draw_timeline(fig, items: Sequence[NewsTimelineItem]) -> None:
    if not items:
        return
    color_map = {"high": BEAR, "medium": "#fb923c", "low": "#eab308"}
    visible = list(items[:4])
    x_values = [0.12 + index * (0.48 / max(1, len(visible) - 1)) for index in range(len(visible))]
    y = 0.105
    if len(visible) > 1:
        fig.lines.append(plt.Line2D([x_values[0], x_values[-1]], [y, y], transform=fig.transFigure, color=GRID, lw=2))
    for x, item in zip(x_values, visible):
        color = color_map.get(item.impact.casefold(), "#f59e0b")
        fig.patches.append(plt.Circle((x, y), 0.006, transform=fig.transFigure, color=color))
        title = item.title if len(item.title) <= 27 else item.title[:24] + "..."
        fig.text(x, y - 0.018, f"{item.time}\n{title}", color=MUTED, fontsize=7, ha="center", va="top")


def render_report_chart(
    df: pd.DataFrame,
    spec: ReportChartSpec,
    *,
    smart_money: SmartMoneyResult | None = None,
    output_dir: str | Path = "/tmp/mergen_quant_reports",
    dpi: int = 150,
) -> str:
    """Telegram'a gönderilmeye hazır, tek dosyalık koyu tema PNG üretir."""

    all_data = _normalise_frame(df)
    offset = max(0, len(all_data) - 90)
    data = all_data.tail(90).reset_index(drop=True)
    decimals = _price_decimals(spec.instrument)
    current = float(data.iloc[-1]["close"])
    direction = spec.direction.strip().upper()
    bullish = direction in {"BULLISH", "YUKARI", "LONG", "RISK-ON"}
    bearish = direction in {"BEARISH", "AŞAĞI", "ASAGI", "SHORT", "RISK-OFF"}
    bias_color = BULL if bullish else BEAR if bearish else "#f59e0b"
    arrow = "▲" if bullish else "▼" if bearish else "◆"

    fig = plt.figure(figsize=(15, 9), facecolor=BG)
    ax = fig.add_axes([0.075, 0.19, 0.72, 0.66], facecolor=BG)
    _draw_candles(ax, data)
    _draw_smart_money(ax, smart_money, offset=offset, length=len(data), decimals=decimals)

    x_end = len(data) - 0.2
    x_start = max(0.0, len(data) - 32.0)
    if spec.entry_low is not None and spec.entry_high is not None:
        zone_label = "BUY ZONE" if not bearish else "SELL ZONE"
        zone_color = BULL if not bearish else BEAR
        _draw_zone(
            ax, spec.entry_low, spec.entry_high, zone_label, zone_color,
            x_start=x_start, x_end=x_end, alpha=0.18, decimals=decimals,
        )
        entry = (float(spec.entry_low) + float(spec.entry_high)) / 2.0
        ax.axhline(entry, color=ENTRY, linewidth=1.35, zorder=9)
        _label_price(ax, entry, "ENTRY", ENTRY, decimals)
    if spec.stop is not None:
        ax.axhline(spec.stop, color=SL, linestyle=(0, (6, 4)), linewidth=1.35, zorder=9)
        _label_price(ax, float(spec.stop), "SL", SL, decimals)
    for index, target in enumerate(spec.targets[:5], start=1):
        ax.axhline(target, color=TP, linestyle=(0, (5, 4)), linewidth=1.05, alpha=max(0.45, 1 - index * 0.08), zorder=7)
        _label_price(ax, float(target), f"TP{index}", TP, decimals, x=1.002 + (index % 2) * 0.0001)
    for level, label in spec.liquidity_levels:
        ax.axhline(level, color="#c084fc", linestyle=(0, (2, 5)), linewidth=1.0, alpha=0.9)
        ax.text(0.7, level, f"Liquidity Sweep • {label}", color="#d8b4fe", fontsize=8, va="bottom")

    ax.axhline(current, color=bias_color, linewidth=0.8, alpha=0.55)
    _label_price(ax, current, "GÜNCEL", bias_color, decimals)
    ax.set_xlim(-1, len(data) + 2)
    price_min_candidates = [float(data["low"].min())]
    if spec.stop is not None:
        price_min_candidates.append(float(spec.stop))
    price_min = min(price_min_candidates)
    price_max_candidates = [float(data["high"].max()), *[float(value) for value in spec.targets[:5]]]
    price_max = max(price_max_candidates)
    padding = max((price_max - price_min) * 0.12, current * 0.015)
    ax.set_ylim(max(0, price_min - padding), price_max + padding)
    ax.grid(True, color=GRID, linewidth=0.55, alpha=0.55)
    ax.tick_params(colors=MUTED, labelsize=9)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value:.{decimals}f}"))
    tick_positions = list(range(0, len(data), max(1, len(data) // 8)))
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(
        [pd.Timestamp(data.iloc[position]["timestamp"]).strftime("%d %b") for position in tick_positions],
        rotation=0,
    )
    for spine in ax.spines.values():
        spine.set_color(GRID)

    title_date = spec.date_label or datetime.now().strftime("%d.%m.%Y")
    fig.text(0.075, 0.93, f"{spec.instrument}  •  {spec.timeframe}  •  {title_date}", color=TEXT, fontsize=20, fontweight="bold")
    if spec.report_kind == "morning":
        banner = f"BUGÜN OLASI YÖN: {direction or 'NÖTR'}"
    elif spec.report_kind == "evening":
        day_open = float(data.iloc[-1]["open"])
        change = (
            float(spec.change_percent)
            if spec.change_percent is not None
            else (current / day_open - 1) * 100 if day_open else 0.0
        )
        banner = f"GÜN KAPANIŞI: {current:.{decimals}f}  ({change:+.2f}%)"
    else:
        banner = f"{arrow}  {direction or 'NÖTR'} BIAS"
    fig.text(
        0.075, 0.87, f"{arrow}  {banner}", color="#ffffff", fontsize=15, fontweight="bold",
        bbox={"boxstyle": "round,pad=0.5", "facecolor": bias_color, "edgecolor": bias_color, "alpha": 0.9},
    )
    add_price_card(fig, current, bias_color, decimals=decimals, x=0.94, y=0.865)
    if spec.rr is not None:
        fig.text(0.94, 0.805, f"RR  1:{spec.rr:.2f}", color=TEXT, fontsize=15, fontweight="bold", ha="center")

    _draw_sentiment_bar(fig, spec.sentiment_score)
    _draw_checklist(fig, spec.checklist)
    if spec.report_kind == "evening":
        _draw_timeline(fig, spec.news_timeline)
    add_watermark(fig)

    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{_safe_filename(spec.report_kind)}_{_safe_filename(spec.instrument)}_{uuid.uuid4().hex[:10]}.png"
    output_path = target_dir / filename
    fig.savefig(output_path, facecolor=BG, dpi=max(100, int(dpi)), bbox_inches="tight")
    plt.close(fig)
    return str(output_path)


def render_equity_curve(
    timestamps: Iterable[datetime | pd.Timestamp],
    equities: Iterable[float],
    *,
    title: str,
    output_dir: str | Path = "/tmp/mergen_quant_reports",
    dpi: int = 150,
) -> str:
    dates = pd.to_datetime(list(timestamps), utc=True, errors="coerce")
    values = pd.Series(list(equities), dtype="float64")
    if len(dates) != len(values) or len(values) < 2:
        raise ValueError("Equity curve için en az iki eşleşen tarih/değer gerekir.")
    running_max = values.cummax().replace(0, pd.NA)
    drawdown = (values / running_max - 1.0) * 100.0

    fig, (ax, ax_dd) = plt.subplots(
        2, 1, figsize=(14, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1]}, facecolor=BG,
    )
    for panel in (ax, ax_dd):
        panel.set_facecolor(BG)
        panel.grid(True, color=GRID, linewidth=0.55, alpha=0.55)
        panel.tick_params(colors=MUTED)
        for spine in panel.spines.values():
            spine.set_color(GRID)
    ax.plot(dates, values, color=BULL, linewidth=2.2)
    ax.fill_between(dates, values, float(values.min()), color=BULL, alpha=0.08)
    ax.set_ylabel("Portföy Değeri", color=TEXT)
    ax.set_title(title, color=TEXT, fontsize=17, fontweight="bold", loc="left")
    ax_dd.fill_between(dates, drawdown.fillna(0), 0, color=BEAR, alpha=0.42)
    ax_dd.set_ylabel("DD %", color=TEXT)
    ax_dd.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m.%Y"))
    fig.autofmt_xdate()
    add_watermark(fig)

    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    output_path = target_dir / f"equity_{uuid.uuid4().hex[:10]}.png"
    fig.savefig(output_path, facecolor=BG, dpi=max(100, int(dpi)), bbox_inches="tight")
    plt.close(fig)
    return str(output_path)
