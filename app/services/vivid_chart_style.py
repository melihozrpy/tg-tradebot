from __future__ import annotations

"""Montana Terminal'in tek-PNG grafik tasarım sistemi.

Bu modül veri veya sinyal üretmez. Tüm grafik üreticileri aynı flat koyu
paleti, mono fiyat tipografisini ve mumların üstüne yazı bindirmeyen zone rail
yerleşimini buradan alır.
"""

from dataclasses import dataclass

from matplotlib.patches import ConnectionPatch, FancyBboxPatch, Rectangle
from matplotlib.transforms import blended_transform_factory


@dataclass(frozen=True)
class VividPalette:
    background: str = "#0A0D13"
    panel: str = "#10141C"
    panel_alt: str = "#161B25"
    grid: str = "#1A2029"
    border: str = "#232936"
    text: str = "#EAEDF3"
    muted: str = "#8B93A3"
    faint: str = "#565F70"
    bull: str = "#2FD8A3"
    bear: str = "#FF5C72"
    amber: str = "#E3B04D"
    blue: str = "#8B93FF"
    cyan: str = "#8B93FF"
    purple: str = "#8B93FF"
    orange: str = "#E3B04D"


VIVID = VividPalette()
SANS_FONT = "DejaVu Sans"
MONO_FONT = "DejaVu Sans Mono"


@dataclass(frozen=True)
class ZoneRailItem:
    """A zone whose only text representation is rendered in the right rail."""

    kind: str
    low: float
    high: float
    color: str
    direction: str = ""
    status: str = "AKTİF"


def style_axes(ax, *, right_axis: bool = True, grid_alpha: float = 0.52) -> None:
    ax.set_facecolor(VIVID.panel)
    ax.grid(True, color=VIVID.grid, linewidth=0.55, alpha=min(0.58, grid_alpha))
    ax.tick_params(colors=VIVID.faint, labelsize=8)
    ax.xaxis.label.set_color(VIVID.muted)
    ax.yaxis.label.set_color(VIVID.muted)
    ax.title.set_color(VIVID.text)
    for spine in ax.spines.values():
        spine.set_color(VIVID.border)
        spine.set_linewidth(0.7)
    if right_axis:
        ax.yaxis.tick_right()
        ax.yaxis.set_label_position("right")


def add_watermark(fig, *, text: str = "SMXM Analiz Sistemi • MONTANA FİNANS ROBOTU") -> None:
    fig.text(0.975, 0.018, text, color=VIVID.faint, fontsize=7.8, ha="right", fontfamily=MONO_FONT)


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
        Rectangle((left, bottom), width, 0.014, transform=fig.transFigure, color=VIVID.panel_alt, zorder=30)
    )
    fig.patches.append(
        Rectangle(
            (left, bottom), width * bounded / 100.0, 0.014,
            transform=fig.transFigure, color=color, zorder=31,
        )
    )
    fig.text(
        left, bottom + 0.024, f"{label}  {bounded:.0f}/100  ·  {status}",
        color=VIVID.text, fontsize=9.3, fontweight="bold", fontfamily=SANS_FONT,
    )


def add_banner(fig, text: str, color: str, *, left: float = 0.065, top: float = 0.885) -> None:
    fig.text(
        left,
        top,
        text,
        color=VIVID.text,
        fontsize=13.0,
        fontweight="bold",
        bbox={"boxstyle": "round,pad=0.40", "facecolor": color, "edgecolor": color, "linewidth": 0.7, "alpha": 0.96},
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
        color=VIVID.background,
        fontsize=21,
        fontweight="bold",
        ha="center",
        fontfamily=MONO_FONT,
        bbox={"boxstyle": "round,pad=0.42", "facecolor": color, "edgecolor": color, "linewidth": 0.7, "alpha": 0.98},
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
            "edgecolor": VIVID.border,
            "alpha": 0.97,
        },
    )


def draw_zone_rail(
    price_ax,
    rail_ax,
    zones: list[ZoneRailItem] | tuple[ZoneRailItem, ...],
    *,
    current_price: float,
    decimals: int,
    maximum: int = 6,
) -> None:
    """Draw non-overlapping OB/FVG cards on a dedicated right-side rail.

    Zone bands stay text-free on the price panel. Their cards are ordered by
    vertical price position, then passed through a one-way sweep with a fixed
    gap. This keeps labels from hiding candles or each other.
    """

    y_min, y_max = price_ax.get_ylim()
    if y_max <= y_min:
        return
    valid = [
        item for item in zones
        if item.high >= item.low and item.high > 0 and item.low > 0
    ][:max(1, maximum)]
    rail_ax.set_facecolor(VIVID.panel)
    rail_ax.set_xlim(0, 1)
    rail_ax.set_ylim(y_min, y_max)
    rail_ax.set_xticks([])
    rail_ax.set_yticks([])
    for spine in rail_ax.spines.values():
        spine.set_color(VIVID.border)
        spine.set_linewidth(0.7)
    rail_ax.text(
        0.06, 0.985, "BÖLGELER", transform=rail_ax.transAxes, va="top",
        color=VIVID.muted, fontsize=7.4, fontweight="bold", fontfamily=SANS_FONT,
    )
    if not valid:
        rail_ax.text(
            0.06, 0.5, "Aktif OB/FVG yok", transform=rail_ax.transAxes,
            color=VIVID.faint, fontsize=7.6, fontfamily=SANS_FONT,
        )
        return

    span = y_max - y_min
    card_height = max(span * 0.105, max(abs(current_price) * 0.008, 1e-8))
    minimum_gap = max(span * 0.018, card_height * 0.15)
    ordered = sorted(valid, key=lambda item: (item.low + item.high) / 2.0)
    desired = [(item.low + item.high) / 2.0 for item in ordered]
    resolved: list[float] = []
    previous = y_min + card_height / 2.0 - minimum_gap
    for wanted in desired:
        value = max(wanted, previous + card_height + minimum_gap)
        resolved.append(value)
        previous = value
    overflow = resolved[-1] + card_height / 2.0 - y_max
    if overflow > 0:
        resolved = [value - overflow for value in resolved]
    underflow = y_min - (resolved[0] - card_height / 2.0)
    if underflow > 0:
        resolved = [value + underflow for value in resolved]

    transform = blended_transform_factory(rail_ax.transAxes, rail_ax.transData)
    x_right = price_ax.get_xlim()[1]
    for item, display_y in zip(ordered, resolved):
        actual_y = (item.low + item.high) / 2.0
        connector = ConnectionPatch(
            xyA=(x_right, actual_y), coordsA=price_ax.transData,
            xyB=(0.04, display_y), coordsB=transform,
            color=item.color, linewidth=0.65, linestyle=(0, (2, 3)), alpha=0.48,
            zorder=8,
        )
        price_ax.figure.add_artist(connector)
        card = FancyBboxPatch(
            (0.04, display_y - card_height / 2.0), 0.92, card_height,
            transform=transform, boxstyle="round,pad=0.008,rounding_size=0.018",
            facecolor=VIVID.panel_alt, edgecolor=VIVID.border, linewidth=0.7, zorder=9,
        )
        rail_ax.add_patch(card)
        mid = (item.low + item.high) / 2.0
        distance = ((mid / current_price) - 1.0) * 100 if current_price else 0.0
        title = " ".join(part for part in (item.direction.upper(), item.kind.upper()) if part).strip()
        rail_ax.text(
            0.09, display_y + card_height * 0.18, title or item.kind.upper(), transform=transform,
            color=item.color, fontsize=7.0, fontweight="bold", fontfamily=SANS_FONT, va="center", zorder=10,
        )
        rail_ax.text(
            0.09, display_y - card_height * 0.10,
            f"{item.low:.{decimals}f} – {item.high:.{decimals}f}", transform=transform,
            color=VIVID.text, fontsize=7.0, fontfamily=MONO_FONT, va="center", zorder=10,
        )
        rail_ax.text(
            0.09, display_y - card_height * 0.34,
            f"{distance:+.1f}% · {item.status}", transform=transform,
            color=VIVID.faint, fontsize=6.1, fontfamily=MONO_FONT, va="center", zorder=10,
        )
