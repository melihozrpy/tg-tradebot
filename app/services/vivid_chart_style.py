from __future__ import annotations

"""Montana Finans Robotu grafiklerinin ortak, yüksek kontrastlı görsel dili.

Bu modül veri veya sinyal üretmez. Yalnızca bütün PNG grafiklerinin aynı koyu
zemini, canlı renkleri, fiyat rozetlerini ve imzasını kullanmasını sağlar.
"""

from dataclasses import dataclass

from matplotlib.patches import Rectangle


@dataclass(frozen=True)
class VividPalette:
    background: str = "#0d1117"
    panel: str = "#0d1117"
    panel_alt: str = "#111827"
    grid: str = "#263244"
    text: str = "#f8fafc"
    muted: str = "#94a3b8"
    bull: str = "#00d9a3"
    bear: str = "#ff4d6d"
    amber: str = "#f59e0b"
    blue: str = "#3b82f6"
    cyan: str = "#38bdf8"
    purple: str = "#c084fc"
    orange: str = "#fb923c"


VIVID = VividPalette()


def style_axes(ax, *, right_axis: bool = True, grid_alpha: float = 0.52) -> None:
    ax.set_facecolor(VIVID.panel)
    ax.grid(True, color=VIVID.grid, linewidth=0.55, alpha=grid_alpha)
    ax.tick_params(colors=VIVID.muted, labelsize=8)
    ax.xaxis.label.set_color(VIVID.muted)
    ax.yaxis.label.set_color(VIVID.muted)
    ax.title.set_color(VIVID.text)
    for spine in ax.spines.values():
        spine.set_color(VIVID.grid)
    if right_axis:
        ax.yaxis.tick_right()
        ax.yaxis.set_label_position("right")


def add_watermark(fig, *, text: str = "SMXM Analiz Sistemi • MONTANA FİNANS ROBOTU") -> None:
    fig.text(0.975, 0.018, text, color="#64748b", fontsize=7.8, ha="right")


def add_score_bar(
    fig,
    score: float,
    *,
    label: str = "TEKNİK GÜVEN",
    left: float = 0.075,
    bottom: float = 0.035,
    width: float = 0.49,
) -> None:
    bounded = max(0.0, min(100.0, float(score)))
    color = VIVID.bull if bounded >= 67 else VIVID.bear if bounded <= 33 else VIVID.amber
    status = "GÜÇLÜ" if bounded >= 67 else "ZAYIF" if bounded <= 33 else "NÖTR"
    fig.patches.append(
        Rectangle((left, bottom), width, 0.021, transform=fig.transFigure, color="#253247", zorder=30)
    )
    fig.patches.append(
        Rectangle(
            (left, bottom), width * bounded / 100.0, 0.021,
            transform=fig.transFigure, color=color, zorder=31,
        )
    )
    fig.text(
        left, bottom + 0.03, f"{label}  {bounded:.0f}/100  •  {status}",
        color=VIVID.text, fontsize=10, fontweight="bold",
    )


def add_banner(fig, text: str, color: str, *, left: float = 0.065, top: float = 0.885) -> None:
    fig.text(
        left,
        top,
        text,
        color="#ffffff",
        fontsize=14.5,
        fontweight="bold",
        bbox={"boxstyle": "round,pad=0.48", "facecolor": color, "edgecolor": color, "alpha": 0.96},
    )


def add_price_card(
    fig,
    price: float,
    color: str,
    *,
    decimals: int = 2,
    x: float = 0.875,
    y: float = 0.79,
) -> None:
    fig.text(
        x,
        y,
        f"{float(price):.{decimals}f}",
        color="#06110d",
        fontsize=23,
        fontweight="bold",
        ha="center",
        bbox={"boxstyle": "round,pad=0.45", "facecolor": color, "edgecolor": color, "alpha": 0.98},
    )


def add_checklist(
    fig,
    items: list[tuple[str, bool]] | tuple[tuple[str, bool], ...],
    *,
    title: str = "TEKNİK CHECKLIST",
    x: float = 0.835,
    y: float = 0.23,
) -> None:
    if not items:
        return
    passed = sum(bool(state) for _label, state in items)
    lines = [f"{title}  {passed}/{len(items)}"]
    lines.extend(f"{'✓' if state else '✕'}  {label}" for label, state in items)
    fig.text(
        x,
        y,
        "\n".join(lines),
        color=VIVID.text,
        fontsize=8.2,
        linespacing=1.42,
        va="bottom",
        bbox={
            "boxstyle": "round,pad=0.65",
            "facecolor": VIVID.panel_alt,
            "edgecolor": VIVID.grid,
            "alpha": 0.97,
        },
    )
