from __future__ import annotations

import os
import hashlib
import json
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")  # sunucu ortaminda GUI olmadan calisir
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Rectangle
from matplotlib.ticker import FuncFormatter

from app.analysis.indicator_engine import ema, macd, rsi
from app.analysis.smart_money_engine import SmartMoneyResult, detect_smart_money
from app.analysis.support_resistance_engine import SupportResistanceResult
from app.services.vivid_chart_style import (
    MONO_FONT,
    SANS_FONT,
    VIVID,
    ZoneRailItem,
    add_banner,
    add_price_card,
    add_score_bar,
    add_watermark,
    draw_zone_rail,
    style_axes as style_vivid_axes,
)

PERIOD_DAYS = {"3ay": 90, "6ay": 180, "1yil": 365, "2yil": 730}


@dataclass(frozen=True)
class ChartTheme:
    name: str
    background: str
    panel: str
    foreground: str
    grid: str
    up: str
    down: str
    support: str
    resistance: str
    accent: str
    muted: str


THEMES = {
    "light": ChartTheme("light", "#f7f9fc", "#ffffff", "#17202a", "#c8d0d9", "#0b8f55", "#d33f49", "#0b8f55", "#c62828", "#2457c5", "#667085"),
    "dark": ChartTheme(
        "dark", VIVID.background, VIVID.panel, VIVID.text, VIVID.grid,
        VIVID.bull, VIVID.bear, VIVID.bull, VIVID.bear, VIVID.blue, VIVID.muted,
    ),
}


def _chart_settings():
    from app.config.settings import get_settings

    settings = get_settings()
    theme = THEMES.get(settings.chart_theme, THEMES["dark"])
    return settings, theme


class ChartFileCache:
    """Render edilmiş asıl PNG'yi saklar, çağırana silinebilir geçici kopya verir."""

    def __init__(self, root: str | Path, ttl_minutes: int) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_minutes = max(0, int(ttl_minutes))
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[str]:
        source = self.root / f"{key}.png"
        if not source.exists():
            self.misses += 1
            return None
        age_minutes = (datetime.now(timezone.utc).timestamp() - source.stat().st_mtime) / 60
        if age_minutes > self.ttl_minutes:
            self.misses += 1
            return None
        self.hits += 1
        target = os.path.join(tempfile.gettempdir(), f"mergen_cached_chart_{uuid.uuid4().hex[:10]}.png")
        shutil.copy2(source, target)
        return target

    def put(self, key: str, rendered_path: str) -> None:
        target = self.root / f"{key}.png"
        temp = self.root / f".{key}.{uuid.uuid4().hex[:6]}.tmp"
        shutil.copy2(rendered_path, temp)
        temp.replace(target)

    def clear(self) -> None:
        for path in self.root.glob("*.png"):
            try:
                path.unlink()
            except OSError:
                pass
        self.hits = 0
        self.misses = 0


_CHART_CACHES: dict[tuple[str, int], ChartFileCache] = {}


def _get_chart_cache() -> ChartFileCache:
    settings, _ = _chart_settings()
    key = (str(Path(settings.chart_cache_dir).resolve()), settings.chart_cache_ttl_minutes)
    if key not in _CHART_CACHES:
        _CHART_CACHES[key] = ChartFileCache(*key)
    return _CHART_CACHES[key]


def clear_chart_cache() -> None:
    for cache in _CHART_CACHES.values():
        cache.clear()
    _CHART_CACHES.clear()


def chart_cache_stats() -> dict:
    return {
        "hits": sum(cache.hits for cache in _CHART_CACHES.values()),
        "misses": sum(cache.misses for cache in _CHART_CACHES.values()),
        "entries": sum(len(list(cache.root.glob("*.png"))) for cache in _CHART_CACHES.values()),
    }


def _chart_cache_key(df: pd.DataFrame, symbol: str, timeframe: str, chart_type: str, context: Optional[dict] = None) -> str:
    last = pd.Timestamp(df.sort_values("timestamp").iloc[-1]["timestamp"])
    payload = {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "data_timestamp": last.isoformat(),
        "rows": len(df),
        "last_close": round(float(df.sort_values("timestamp").iloc[-1]["close"]), 6),
        "chart_type": chart_type,
        "context": context or {},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _draw_candles(ax, df: pd.DataFrame, theme: ChartTheme, width: float = 0.64) -> np.ndarray:
    """Gerçek mum gövdesi (Open-Close) ve fitili (High-Low), boş gün aralığı olmadan."""
    x = np.arange(len(df), dtype=float)
    prices = df[["open", "high", "low", "close"]].astype(float)
    median_price = max(float(prices["close"].median()), 0.01)
    doji_height = median_price * 0.00035
    for index, row in prices.iterrows():
        xpos = x[index]
        color = theme.up if row["close"] >= row["open"] else theme.down
        ax.vlines(xpos, row["low"], row["high"], color=color, linewidth=0.75, zorder=2)
        bottom = min(row["open"], row["close"])
        height = abs(row["close"] - row["open"])
        if height < doji_height:
            bottom = (row["open"] + row["close"]) / 2 - doji_height / 2
            height = doji_height
        ax.add_patch(
            Rectangle(
                (xpos - width / 2, bottom),
                width,
                height,
                facecolor=color,
                edgecolor=color,
                linewidth=0.65,
                zorder=3,
            )
        )
    return x


def _format_trading_axis(
    ax,
    df: pd.DataFrame,
    right_margin: float = 4.0,
    label_format: str = "%d.%m.%y",
) -> None:
    count = len(df)
    if not count:
        return
    tick_count = min(9, count)
    positions = np.unique(np.linspace(0, count - 1, tick_count).astype(int))
    timestamps = pd.to_datetime(df["timestamp"], utc=True)
    labels = [timestamps.iloc[index].strftime(label_format) for index in positions]
    ax.set_xticks(positions)
    ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=7)
    ax.set_xlim(-1, count - 1 + right_margin)


def _style_axes(fig, axes, theme: ChartTheme) -> None:
    fig.patch.set_facecolor(theme.background)
    for ax in axes:
        if theme.name == "dark":
            style_vivid_axes(ax)
        else:
            ax.set_facecolor(theme.panel)
            ax.tick_params(colors=theme.foreground, labelsize=7)
            ax.yaxis.label.set_color(theme.foreground)
            ax.title.set_color(theme.foreground)
            for spine in ax.spines.values():
                spine.set_color(theme.grid)
            ax.grid(color=theme.grid, alpha=0.25, linewidth=0.5)


def _resolve_label_positions(items: list[tuple[float, str, str, int]], y_min: float, y_max: float, max_labels: int = 12) -> list[tuple[float, float, str, str]]:
    """Öncelikli seviyeleri seçer ve sağ fiyat etiketlerine asgari dikey aralık verir."""
    if not items:
        return []
    chosen = sorted(items, key=lambda item: (-item[3], item[0]))[:max_labels]
    chosen.sort(key=lambda item: item[0])
    gap = max((y_max - y_min) * 0.022, 0.01)
    resolved: list[tuple[float, float, str, str]] = []
    previous_y = y_min - gap
    for actual, label, color, _priority in chosen:
        display_y = max(actual, previous_y + gap)
        display_y = min(display_y, y_max)
        resolved.append((actual, display_y, label, color))
        previous_y = display_y
    return resolved


def _price_decimals(symbol: str) -> int:
    clean = symbol.upper().removesuffix(".IS")
    if clean in {"EURUSD", "GBPUSD", "USDJPY"} or clean.endswith("=X"):
        return 5
    return 2


def _normalise_chart_frame(df: pd.DataFrame, *, minimum: int = 10) -> pd.DataFrame:
    required = {"timestamp", "open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Grafik için eksik OHLC kolonları: {sorted(missing)}")
    data = df.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        if column in data:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    if "volume" not in data:
        data["volume"] = 0.0
    data = (
        data.dropna(subset=["timestamp", "open", "high", "low", "close"])
        .sort_values("timestamp")
        .drop_duplicates("timestamp", keep="last")
        .reset_index(drop=True)
    )
    if len(data) < minimum:
        raise ValueError(f"Grafik için en az {minimum} geçerli mum gerekir.")
    return data


def _draw_smart_money_overlay(
    ax,
    smart: SmartMoneyResult,
    *,
    length: int,
    decimals: int,
    detailed: bool = True,
    compact: bool = False,
    exclude_zone: tuple[str, float, float] | None = None,
    zone_sink: list[ZoneRailItem] | None = None,
) -> None:
    x_end = length - 0.2
    zone_count = 3 if detailed else 2
    zones = [*smart.order_blocks[-zone_count:], *smart.fvg[-zone_count:]]
    for zone in zones:
        if (
            exclude_zone is not None
            and zone.kind == exclude_zone[0]
            and abs(float(zone.low) - exclude_zone[1]) < 1e-6
            and abs(float(zone.high) - exclude_zone[2]) < 1e-6
        ):
            continue
        origin = zone.origin_index if zone.origin_index is not None else zone.index
        x_start = max(0.0, min(float(origin), x_end))
        if zone.kind == "OB":
            color = VIVID.bull if zone.direction == "bullish" else VIVID.bear
        else:
            color = VIVID.amber
        lower, upper = sorted((float(zone.low), float(zone.high)))
        height = max(upper - lower, max(upper * 0.0003, 1e-8))
        ax.add_patch(
            Rectangle(
                (x_start, lower), max(0.5, x_end - x_start), height,
                facecolor=color, edgecolor=color, linewidth=0.9,
                alpha=0.08 if compact else 0.10, zorder=1,
            )
        )
        if zone_sink is not None:
            zone_sink.append(
                ZoneRailItem(
                    kind=zone.kind,
                    low=lower,
                    high=upper,
                    color=color,
                    direction=zone.direction,
                )
            )

    event_count = 3 if compact else 5
    used_indices: list[int] = []
    minimum_event_distance = max(4, round(length * 0.055))
    for event in reversed(smart.structure[-event_count:]):
        if not 0 <= event.index < length:
            continue
        if any(abs(event.index - item) < minimum_event_distance for item in used_indices):
            continue
        used_indices.append(event.index)
        color = VIVID.bull if event.direction == "bullish" else VIVID.bear
        marker = "▲" if event.direction == "bullish" else "▼"
        ax.annotate(
            f"{marker} {event.kind}",
            xy=(event.index, 0.982 if event.direction == "bullish" else 0.018),
            xycoords=("data", "axes fraction"),
            xytext=(0, 0),
            textcoords="offset points",
            ha="center",
            va="center",
            fontsize=6.1 if compact else 8.2,
            fontweight="bold",
            color=color,
            fontfamily=SANS_FONT,
            zorder=15,
        )


def _numeric_score(info_box: Optional[dict]) -> float | None:
    if not info_box:
        return None
    for key in ("Skor", "Güven", "Piyasa güveni", "Piyasa Güveni"):
        raw = info_box.get(key)
        if raw is None:
            continue
        try:
            return max(0.0, min(100.0, float(str(raw).split("/")[0].replace("%", "").strip())))
        except (TypeError, ValueError):
            continue
    return None


def _technical_visual_state(
    data: pd.DataFrame,
    *,
    entry_zone: Optional[tuple] = None,
    entry_trigger: Optional[float] = None,
    stop_price: Optional[float] = None,
    targets: Optional[list] = None,
    info_box: Optional[dict] = None,
) -> dict:
    close = data["close"].astype(float)
    ema20_series = ema(close, min(20, max(2, len(close) // 2)))
    ema50_period = min(50, max(3, len(close) - 1))
    ema50_series = ema(close, ema50_period)
    current = float(close.iloc[-1])
    ema20_value = float(ema20_series.iloc[-1])
    ema50_value = float(ema50_series.iloc[-1])
    bullish = current > ema20_value >= ema50_value
    bearish = current < ema20_value <= ema50_value

    decision = str((info_box or {}).get("Nihai karar") or "").upper()
    if any(token in decision for token in ("BUY", "LONG", "AL ", "ALIM")):
        bullish, bearish = True, False
    elif any(token in decision for token in ("SELL", "SHORT", "SAT")):
        bullish, bearish = False, True

    direction = "YUKARI" if bullish else "AŞAĞI" if bearish else "RANGE"
    direction_name = "bullish" if bullish else "bearish" if bearish else "range"
    color = VIVID.bull if bullish else VIVID.bear if bearish else VIVID.amber

    rsi_value = float(rsi(close, 14).iloc[-1]) if len(close) >= 15 else 50.0
    macd_line, signal_line, histogram = macd(close)
    macd_value = float(histogram.fillna(0).iloc[-1])
    momentum_ok = (bullish and rsi_value >= 50 and macd_value >= 0) or (
        bearish and rsi_value <= 50 and macd_value <= 0
    )
    smart = detect_smart_money(data)
    zone_ok = bool(smart.order_blocks or smart.fvg)
    structure = smart.structure[-1] if smart.structure else None
    structure_ok = bool(structure and structure.direction == direction_name)

    entry = entry_trigger
    if entry is None and entry_zone and entry_zone[0] is not None and entry_zone[1] is not None:
        entry = (float(entry_zone[0]) + float(entry_zone[1])) / 2.0
    rr_value: float | None = None
    if entry is not None and stop_price is not None:
        risk = abs(float(entry) - float(stop_price))
        valid_targets = [float(value) for value in (targets or []) if value is not None]
        if risk > 0 and valid_targets:
            reward = max(abs(value - float(entry)) for value in valid_targets)
            rr_value = reward / risk

    checklist = [
        ("Ana trend", bullish or bearish),
        ("EMA20 / EMA50 hizası", (ema20_value >= ema50_value) if bullish else (ema20_value <= ema50_value) if bearish else False),
        ("RSI + MACD uyumu", momentum_ok),
        ("FVG / Order Block", zone_ok),
        ("BOS / MSS teyidi", structure_ok),
        ("Minimum 1:2 RR", bool(rr_value is not None and rr_value >= 2.0)),
    ]
    derived_score = sum(state for _label, state in checklist) / len(checklist) * 100.0
    score = _numeric_score(info_box)
    if score is None:
        score = derived_score
    return {
        "current": current,
        "direction": direction,
        "direction_name": direction_name,
        "color": color,
        "score": score,
        "checklist": checklist,
        "smart": smart,
        "rr": rr_value,
        "rsi": rsi_value,
        "macd_hist": macd_value,
        "ema20": ema20_value,
        "ema50": ema50_value,
    }


def _annotate_vivid_price(
    ax,
    actual: float,
    display: float,
    label: str,
    color: str,
    *,
    decimals: int,
    fontsize: float = 8.2,
) -> None:
    ax.annotate(
        f"{label}  {actual:.{decimals}f}",
        xy=(1.0, actual),
        xycoords=("axes fraction", "data"),
        xytext=(1.018, display),
        textcoords=("axes fraction", "data"),
        ha="left",
        va="center",
        fontsize=fontsize,
        fontweight="bold",
        color=VIVID.background,
        fontfamily=MONO_FONT,
        arrowprops={"arrowstyle": "-", "color": color, "lw": 0.7, "alpha": 0.85},
        bbox={"boxstyle": "round,pad=0.30", "facecolor": color, "edgecolor": color, "linewidth": 0.7, "alpha": 0.95},
        clip_on=False,
        zorder=25,
    )


def _draw_indicator_ribbon(ax, state: dict, *, detailed: bool) -> None:
    direction_text = "Pozitif" if state["macd_hist"] >= 0 else "Negatif"
    values = [
        f"EMA20 {state['ema20']:.2f}",
        f"EMA50 {state['ema50']:.2f}",
        f"RSI {state['rsi']:.1f}",
        f"MACD {direction_text}",
    ]
    if not detailed:
        values = values[:3]
    ax.text(
        0.012,
        0.982,
        "   •   ".join(values),
        transform=ax.transAxes,
        va="top",
        ha="left",
        color=VIVID.text,
        fontsize=7.8,
        fontweight="bold",
        fontfamily=SANS_FONT,
        bbox={"boxstyle": "round,pad=0.38", "facecolor": VIVID.panel_alt, "edgecolor": VIVID.border, "alpha": 0.92},
        zorder=20,
    )


def resolve_period_days(period: str) -> int:
    return PERIOD_DAYS.get(period.lower(), 180)


def generate_price_chart(
    df: pd.DataFrame,
    symbol: str,
    sr: Optional[SupportResistanceResult] = None,
    entry_zone: Optional[tuple] = None,
    stop_price: Optional[float] = None,
    targets: Optional[list] = None,
) -> str:
    """Eski çağrılar için canlı tasarımlı, hacimsiz standart teknik grafik."""
    info_box = {}
    if sr is not None:
        info_box = {
            "Ana destek": f"{sr.main_support:.2f}" if sr.main_support is not None else "-",
            "Ana direnç": f"{sr.main_resistance:.2f}" if sr.main_resistance is not None else "-",
        }
    return generate_professional_daily_chart(
        df,
        symbol,
        info_box=info_box,
        entry_zone=entry_zone,
        stop_price=stop_price,
        targets=targets,
        chart_mode="standard",
    )


def generate_relative_strength_chart(
    stock_df: pd.DataFrame, index_df: pd.DataFrame, symbol: str, index_symbol: str
) -> str:
    """Normalize edilmis hisse vs endeks performansi ve relatif oran grafigi."""
    s = stock_df[["timestamp", "close"]].copy()
    i = index_df[["timestamp", "close"]].copy()
    s["date_key"] = pd.to_datetime(s["timestamp"], utc=True).dt.date
    i["date_key"] = pd.to_datetime(i["timestamp"], utc=True).dt.date
    merged = pd.merge(s, i, on="date_key", suffixes=("_stock", "_index"), how="inner").sort_values("date_key")

    if merged.empty:
        raise ValueError("Ortak islem gunu bulunamadi; relatif guc grafigi uretilemedi.")

    norm_stock = merged["close_stock"] / merged["close_stock"].iloc[0] * 100
    norm_index = merged["close_index"] / merged["close_index"].iloc[0] * 100
    ratio = merged["close_stock"] / merged["close_index"]
    ratio_norm = ratio / ratio.iloc[0] * 100

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(14, 8), sharex=True,
        gridspec_kw={"height_ratios": [3.2, 1.25]}, facecolor=VIVID.background,
    )
    for axis in (ax1, ax2):
        style_vivid_axes(axis, right_axis=True)
    ax1.plot(merged["date_key"], norm_stock, label=symbol, color=VIVID.cyan, linewidth=2.1)
    ax1.fill_between(merged["date_key"], norm_stock, 100, color=VIVID.cyan, alpha=0.08)
    ax1.plot(merged["date_key"], norm_index, label=index_symbol, color=VIVID.muted, linewidth=1.25)
    ax1.axhline(100, color=VIVID.grid, linewidth=0.8, linestyle="--")
    ax1.legend(fontsize=8, facecolor=VIVID.panel_alt, edgecolor=VIVID.grid, labelcolor=VIVID.text)

    ratio_last = float(ratio_norm.iloc[-1])
    stronger = ratio_last >= 100
    direction_color = VIVID.bull if stronger else VIVID.bear
    ax2.plot(merged["date_key"], ratio_norm, color=direction_color, linewidth=1.8)
    ax2.fill_between(merged["date_key"], ratio_norm, 100, color=direction_color, alpha=0.10)
    ax2.axhline(y=100, color=VIVID.muted, linewidth=0.8, linestyle="--")
    ax2.set_ylabel("RS", color=VIVID.muted)

    fig.text(
        0.065, 0.94, f"MONTANA FİNANS ROBOTU  •  {symbol.upper()} / {index_symbol.upper()}  •  RELATİF GÜÇ",
        color=VIVID.text, fontsize=18, fontweight="bold",
    )
    add_banner(fig, f"{'▲' if stronger else '▼'}  RELATİF YÖN: {'GÜÇLÜ' if stronger else 'ZAYIF'}", direction_color)
    fig.text(
        0.865, 0.79, f"RS {ratio_last:.1f}", color="#ffffff", fontsize=21, fontweight="bold", ha="center",
        bbox={"boxstyle": "round,pad=0.45", "facecolor": direction_color, "edgecolor": direction_color},
    )
    score = max(0.0, min(100.0, 50.0 + (ratio_last - 100.0) * 2.5))
    add_score_bar(fig, score, label="RELATİF GÜÇ SKORU")
    add_watermark(fig)
    fig.autofmt_xdate()
    fig.subplots_adjust(left=0.065, right=0.92, top=0.81, bottom=0.11, hspace=0.10)

    out_path = os.path.join(tempfile.gettempdir(), f"mergen_rschart_{symbol}_{uuid.uuid4().hex[:8]}.png")
    fig.savefig(out_path, dpi=140, facecolor=VIVID.background, bbox_inches="tight")
    plt.close(fig)
    return out_path


_TIMEFRAME_LEVEL_STYLE = {
    "gunluk": {"support": VIVID.bull, "resistance": VIVID.bear, "alpha": 0.72, "ls": "--"},
    "haftalik": {"support": VIVID.cyan, "resistance": "#fb7185", "alpha": 0.68, "ls": "-."},
    "aylik": {"support": VIVID.purple, "resistance": VIVID.amber, "alpha": 0.68, "ls": ":"},
}


def _draw_timeframe_levels(
    ax,
    x_last,
    timeframe_levels,
    label_items: Optional[list] = None,
    allowed_timeframes: Optional[set[str]] = None,
) -> None:
    """Gunluk/haftalik/aylik destek-direnc seviyelerini GORSEL OLARAK AYRI
    renk/stil ile cizer; etiketler ust uste binmesin diye kisa kodlar kullanilir."""
    if timeframe_levels is None:
        return
    tf_map = {
        "gunluk": timeframe_levels.daily,
        "haftalik": timeframe_levels.weekly,
        "aylik": timeframe_levels.monthly,
    }
    tf_short = {"gunluk": "G", "haftalik": "H", "aylik": "A"}
    for tf_name, tf_result in tf_map.items():
        if allowed_timeframes is not None and tf_name not in allowed_timeframes:
            continue
        if not tf_result or not tf_result.reliable:
            continue
        style = _TIMEFRAME_LEVEL_STYLE[tf_name]
        for lvl, kind in [
            (tf_result.main_support, "support"), (tf_result.main_resistance, "resistance"),
        ]:
            if lvl is None:
                continue
            color = style[kind]
            ax.axhspan(lvl.low, lvl.high, color=color, alpha=max(0.05, style["alpha"] * 0.15))
            ax.axhline(y=lvl.mid, color=color, linestyle=style["ls"], linewidth=0.65, alpha=style["alpha"])
            if label_items is not None:
                priority = 90 if tf_name == "aylik" else (80 if tf_name == "haftalik" else 70)
                label_items.append((lvl.mid, f"{tf_short[tf_name]} {'D' if kind == 'support' else 'R'} {lvl.mid:.2f}", color, priority))
            else:
                ax.text(x_last, lvl.mid, f" {tf_short[tf_name]}", fontsize=6, color=color, va="center")


def _draw_confluence_zones(ax, confluence_zones) -> None:
    """Cakisan guclu bolgeleri golgeli bant olarak vurgular (etiket kalabaligini
    artirmamak icin ayri metin eklemez, sadece bandi renklendirir)."""
    if not confluence_zones:
        return
    for zone in confluence_zones:
        color = VIVID.bull if zone.kind == "destek" else VIVID.bear
        ax.axhspan(zone.low, zone.high, color=color, alpha=0.10)


def _draw_price_scenario_bands(ax, price_scenario) -> None:
    """Yakin geri cekilme / ana dip bolgesi / guclu yukselis senaryosu bantlari."""
    if price_scenario is None or not getattr(price_scenario, "reliable", False):
        return
    for tier in getattr(price_scenario, "support_tiers", []) or []:
        ax.axhspan(tier.low, tier.high, color=VIVID.bull, alpha=0.08)
    for tier in getattr(price_scenario, "resistance_tiers", []) or []:
        ax.axhspan(tier.low, tier.high, color=VIVID.bear, alpha=0.08)


def _draw_anomaly_markers(ax, df: pd.DataFrame, anomalies) -> None:
    if not anomalies:
        return
    for a in anomalies:
        ts = getattr(a, "detected_at", None)
        price = getattr(a, "price_at_detection", None)
        if ts is None or price is None:
            continue
        timestamps = pd.to_datetime(df["timestamp"], utc=True)
        target = pd.Timestamp(ts)
        target = target.tz_localize("UTC") if target.tzinfo is None else target.tz_convert("UTC")
        xpos = int(np.argmin(np.abs((timestamps - target).dt.total_seconds().to_numpy())))
        ax.scatter([xpos], [price], marker="^", s=45, color="#ff9900", zorder=5, edgecolors="black", linewidths=0.4)


def _draw_news_markers(ax, df: pd.DataFrame, news_markers) -> None:
    """Haber tarihlerini dikey ince cizgi olarak isaretler (grafigi kalabaliklastirmamak
    icin metin etiketi eklenmez)."""
    if not news_markers:
        return
    timestamps = pd.to_datetime(df["timestamp"], utc=True)
    for ts in news_markers:
        target = pd.Timestamp(ts)
        target = target.tz_localize("UTC") if target.tzinfo is None else target.tz_convert("UTC")
        xpos = int(np.argmin(np.abs((timestamps - target).dt.total_seconds().to_numpy())))
        ax.axvline(x=xpos, color="#9467bd", linestyle=":", linewidth=0.6, alpha=0.5)


def _level_context_signature(timeframe_levels, confluence_zones) -> dict:
    levels = []
    if timeframe_levels is not None:
        for tf in (timeframe_levels.daily, timeframe_levels.weekly, timeframe_levels.monthly):
            for level in (tf.main_support, tf.main_resistance):
                if level is not None:
                    levels.append((tf.timeframe, level.mid, level.confidence))
    confluence = [
        (zone.kind, zone.low, zone.high, zone.confidence)
        for zone in (confluence_zones or [])
    ]
    return {"levels": levels, "confluence": confluence}


def _render_professional_daily_chart(
    df: pd.DataFrame,
    symbol: str,
    info_box: Optional[dict],
    timeframe_levels,
    confluence_zones: Optional[list],
    price_scenario,
    entry_zone: Optional[tuple],
    entry_trigger: Optional[float],
    stop_price: Optional[float],
    targets: Optional[list],
    anomalies: Optional[list],
    news_markers: Optional[list],
    breakout_markers: Optional[list],
    retest_markers: Optional[list],
    last_signal_point: Optional[tuple],
    chart_mode: str,
) -> str:
    all_data = _normalise_chart_frame(df)
    settings, _theme = _chart_settings()
    chart_mode = str(chart_mode or "detailed").casefold()
    if chart_mode not in {"standard", "detailed"}:
        raise ValueError("Grafik modu 'standard' veya 'detailed' olmalı.")
    detailed = chart_mode == "detailed"
    context_signature = {
        **_level_context_signature(timeframe_levels, confluence_zones),
        "entry_zone": entry_zone,
        "entry_trigger": entry_trigger,
        "stop": stop_price,
        "targets": targets,
        "info": info_box,
        "mode": chart_mode,
        "style": "vivid_reference_v3",
    }
    key = _chart_cache_key(all_data, symbol, "1d", f"professional_daily_{chart_mode}", context_signature)
    cache = _get_chart_cache()
    cached = cache.get(key)
    if cached:
        return cached

    visible_count = min(120, len(all_data))
    data = all_data.tail(visible_count).reset_index(drop=True)
    decimals = _price_decimals(symbol)
    state = _technical_visual_state(
        data,
        entry_zone=entry_zone,
        entry_trigger=entry_trigger,
        stop_price=stop_price,
        targets=targets,
        info_box=info_box,
    )
    theme = THEMES["dark"]
    fig = plt.figure(figsize=(16, 9), facecolor=VIVID.background)
    ax_price = fig.add_axes([0.055, 0.16, 0.60, 0.67], facecolor=VIVID.panel)
    ax_rail = fig.add_axes([0.685, 0.16, 0.26, 0.67], facecolor=VIVID.panel)
    style_vivid_axes(ax_price)
    x = _draw_candles(ax_price, data, theme, width=0.72)
    label_items: list[tuple[float, str, str, int]] = []

    full_close = all_data["close"].astype(float)
    # Main price panel stays deliberately quiet: two trend references maximum.
    ema_styles = [(20, VIVID.amber, 1.15), (50, VIVID.blue, 1.15)]
    for period, color, width in ema_styles:
        if len(all_data) >= period:
            values = ema(full_close, period).tail(visible_count).reset_index(drop=True)
            ax_price.plot(x, values, color=color, linewidth=width, label=f"EMA{period}", alpha=0.94)

    _draw_timeframe_levels(
        ax_price,
        x[-1],
        timeframe_levels,
        label_items,
        allowed_timeframes=None if detailed else {"gunluk", "haftalik"},
    )
    if detailed:
        _draw_confluence_zones(ax_price, confluence_zones)
        _draw_price_scenario_bands(ax_price, price_scenario)
    zone_rail_items: list[ZoneRailItem] = []
    _draw_smart_money_overlay(
        ax_price,
        state["smart"],
        length=len(data),
        decimals=decimals,
        detailed=detailed,
        zone_sink=zone_rail_items,
    )

    current_price = state["current"]
    ax_price.axhline(current_price, color=state["color"], linestyle=(0, (6, 4)), linewidth=1.0, alpha=0.72)
    label_items.append((current_price, "GÜNCEL", state["color"], 110))

    entry = entry_trigger
    if entry_zone and entry_zone[0] is not None and entry_zone[1] is not None:
        lower, upper = sorted((float(entry_zone[0]), float(entry_zone[1])))
        x_start = max(0.0, len(data) - 34.0)
        ax_price.add_patch(
            Rectangle(
                (x_start, lower), len(data) - 0.2 - x_start, max(upper - lower, upper * 0.0003),
                facecolor=VIVID.bull, edgecolor=VIVID.bull, linewidth=0.8, alpha=0.10, zorder=1,
            )
        )
        zone_rail_items.append(
            ZoneRailItem("ENTRY ZONE", lower, upper, VIVID.bull, direction=state["direction_name"])
        )
        if entry is None:
            entry = (lower + upper) / 2.0
    if entry is not None:
        ax_price.axhline(entry, color=VIVID.blue, linewidth=1.35)
        label_items.append((float(entry), "ENTRY", VIVID.blue, 108))
    if stop_price is not None:
        ax_price.axhline(stop_price, color=VIVID.bear, linestyle=(0, (6, 4)), linewidth=1.35)
        label_items.append((float(stop_price), "SL", VIVID.bear, 109))
    visible_targets = [value for value in (targets or []) if value is not None]
    visible_targets = visible_targets[:5] if detailed else visible_targets[:3]
    for index, target in enumerate(visible_targets, start=1):
        target = float(target)
        ax_price.axhline(target, color=VIVID.bull, linestyle=(0, (5, 4)), linewidth=1.0, alpha=max(0.48, 0.9 - index * 0.07))
        label_items.append((target, f"TP{index}", VIVID.bull, 104 - index))

    if detailed:
        _draw_anomaly_markers(ax_price, data, anomalies)
        _draw_news_markers(ax_price, data, news_markers)
    timestamps = pd.to_datetime(data["timestamp"], utc=True)
    for markers, marker, color, label in (
        (breakout_markers, "^", VIVID.bull, "Kırılım"),
        (retest_markers, "o", VIVID.amber, "Retest"),
    ):
        for point in ((markers or []) if detailed else []):
            ts, price = point
            target = pd.Timestamp(ts)
            target = target.tz_localize("UTC") if target.tzinfo is None else target.tz_convert("UTC")
            xpos = int(np.argmin(np.abs((timestamps - target).dt.total_seconds().to_numpy())))
            ax_price.scatter(xpos, price, marker=marker, s=42, color=color, label=label, zorder=17)
    if detailed and last_signal_point:
        ts, price = last_signal_point
        target = pd.Timestamp(ts)
        target = target.tz_localize("UTC") if target.tzinfo is None else target.tz_convert("UTC")
        xpos = int(np.argmin(np.abs((timestamps - target).dt.total_seconds().to_numpy())))
        ax_price.scatter(xpos, price, marker="*", s=105, color=VIVID.amber, edgecolors=VIVID.text, linewidths=0.5, zorder=18)

    all_prices = [float(data["low"].min()), float(data["high"].max()), current_price]
    all_prices.extend(float(value) for value in visible_targets)
    if entry is not None:
        all_prices.append(float(entry))
    if stop_price is not None:
        all_prices.append(float(stop_price))
    price_min, price_max = min(all_prices), max(all_prices)
    padding = max((price_max - price_min) * 0.13, current_price * 0.012)
    ax_price.set_ylim(max(0.0, price_min - padding), price_max + padding)
    ax_price.set_xlim(-1, len(data) + 2)
    y_min, y_max = ax_price.get_ylim()
    for actual, display, label, color in _resolve_label_positions(label_items, y_min, y_max, max_labels=10):
        _annotate_vivid_price(
            ax_price, actual, display, label, color, decimals=decimals,
            fontsize=8.2 if detailed else 8.5,
        )

    ax_price.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value:.{decimals}f}"))
    _format_trading_axis(ax_price, data, right_margin=2.5, label_format="%d %b")
    draw_zone_rail(
        ax_price,
        ax_rail,
        zone_rail_items,
        current_price=current_price,
        decimals=decimals,
    )
    _draw_indicator_ribbon(ax_price, state, detailed=detailed)
    ax_price.set_ylabel("Fiyat", fontsize=8, fontfamily=SANS_FONT)
    handles, labels = ax_price.get_legend_handles_labels()
    if handles:
        unique = dict(zip(labels, handles))
        ax_price.legend(
            unique.values(), unique.keys(), loc="lower left", fontsize=6.4, ncol=4,
            facecolor=VIVID.panel_alt, edgecolor=VIVID.border, labelcolor=VIVID.text, framealpha=0.82,
        )

    mode_label = "STANDART" if not detailed else "DETAYLI"
    date_label = pd.Timestamp(data.iloc[-1]["timestamp"]).strftime("%d.%m.%Y")
    fig.text(
        0.055, 0.94, f"{symbol.upper()}  •  1D  •  {date_label}  •  {mode_label}",
        color=VIVID.text, fontsize=18, fontweight="bold", fontfamily=SANS_FONT,
    )
    arrow = "▲" if state["direction"] == "YUKARI" else "▼" if state["direction"] == "AŞAĞI" else "◆"
    add_banner(fig, f"{arrow}  TEKNİK YÖN: {state['direction']}", state["color"], left=0.055, top=0.875)
    add_price_card(fig, current_price, state["color"], decimals=decimals, x=0.86, y=0.875)
    if state["rr"] is not None:
        fig.text(0.86, 0.81, f"RR  1:{state['rr']:.2f}", color=VIVID.text, fontsize=12, fontweight="bold", ha="center", fontfamily=MONO_FONT)
    add_score_bar(fig, state["score"], label="TEKNİK GÜVEN")
    add_watermark(fig)
    last_mss = next((event for event in reversed(state["smart"].structure) if event.kind == "MSS"), None)
    fig.text(
        0.41,
        0.057,
        f"Trend {state['direction']}  ·  Son MSS {last_mss.direction if last_mss else 'yok'}  ·  {len(zone_rail_items)} aktif bölge",
        color=VIVID.muted,
        fontsize=8.0,
        fontfamily=SANS_FONT,
    )

    out_path = os.path.join(tempfile.gettempdir(), f"mergen_pro_chart_{symbol}_{uuid.uuid4().hex[:8]}.png")
    fig.savefig(out_path, dpi=max(settings.chart_dpi, 140), facecolor=VIVID.background, bbox_inches="tight")
    plt.close(fig)
    cache.put(key, out_path)
    return out_path


def generate_professional_daily_chart(
    df: pd.DataFrame,
    symbol: str,
    info_box: Optional[dict] = None,
    timeframe_levels=None,
    confluence_zones: Optional[list] = None,
    price_scenario=None,
    entry_zone: Optional[tuple] = None,
    entry_trigger: Optional[float] = None,
    stop_price: Optional[float] = None,
    targets: Optional[list] = None,
    anomalies: Optional[list] = None,
    news_markers: Optional[list] = None,
    breakout_markers: Optional[list] = None,
    retest_markers: Optional[list] = None,
    last_signal_point: Optional[tuple] = None,
    chart_mode: str = "detailed",
) -> str:
    """Hacimsiz, sade ve yüksek kontrastlı standart/detaylı SMXM teknik grafik."""
    return _render_professional_daily_chart(
        df,
        symbol,
        info_box,
        timeframe_levels,
        confluence_zones,
        price_scenario,
        entry_zone,
        entry_trigger,
        stop_price,
        targets,
        anomalies,
        news_markers,
        breakout_markers,
        retest_markers,
        last_signal_point,
        chart_mode,
    )


def generate_multi_timeframe_chart(frames: dict[str, pd.DataFrame], symbol: str) -> str:
    """5dk, 15dk, 1s ve 4s için referans tasarımla dört canlı SMXM paneli."""
    settings, _theme = _chart_settings()
    theme = THEMES["dark"]
    fig, axes = plt.subplots(2, 2, figsize=(15, 9), facecolor=VIVID.background)
    states: list[dict] = []
    timeframe_labels = ("5 dk", "15 dk", "1 saat", "4 saat")
    for ax, label in zip(axes.flat, timeframe_labels):
        raw = frames.get(label, pd.DataFrame())
        try:
            data = _normalise_chart_frame(raw, minimum=10).tail(90).reset_index(drop=True)
        except ValueError:
            style_vivid_axes(ax)
            ax.text(.5, .5, "VERİ YOK", transform=ax.transAxes, ha="center", va="center", color=VIVID.muted, fontweight="bold")
            ax.set_title(label.upper(), color=VIVID.muted, fontweight="bold", fontsize=10, loc="left")
            continue

        style_vivid_axes(ax)
        x = _draw_candles(ax, data, theme, width=.70)
        close = data["close"].astype(float)
        if len(data) >= 20:
            ax.plot(x, ema(close, 20), color=VIVID.amber, linewidth=1.0, label="EMA20")
        if len(data) >= 50:
            ax.plot(x, ema(close, 50), color=VIVID.cyan, linewidth=1.0, label="EMA50")
        state = _technical_visual_state(data)
        states.append(state)
        _draw_smart_money_overlay(
            ax, state["smart"], length=len(data), decimals=_price_decimals(symbol),
            detailed=True, compact=True,
        )
        ax.axhline(state["current"], color=state["color"], linewidth=.85, linestyle=(0, (5, 4)), alpha=.75)
        _annotate_vivid_price(
            ax, state["current"], state["current"], "SON", state["color"],
            decimals=_price_decimals(symbol), fontsize=6.5,
        )
        ax.text(
            .985, .93, f"{state['score']:.0f}/100", transform=ax.transAxes,
            ha="right", va="top", color="#ffffff", fontsize=8, fontweight="bold",
            bbox={"boxstyle": "round,pad=.28", "facecolor": state["color"], "edgecolor": state["color"]},
        )
        arrow = "▲" if state["direction"] == "YUKARI" else "▼" if state["direction"] == "AŞAĞI" else "◆"
        ax.set_title(
            f"{label.upper()}  •  {arrow} {state['direction']}  •  {state['current']:.2f}",
            color=state["color"], fontweight="bold", fontsize=10, loc="left", pad=9,
        )
        ax.text(
            .01, .91, f"RSI {state['rsi']:.1f}  •  MACD {'+' if state['macd_hist'] >= 0 else '-'}",
            transform=ax.transAxes, color=VIVID.muted, fontsize=6.5, fontweight="bold",
        )
        _format_trading_axis(ax, data, right_margin=2.5, label_format="%d.%m %H:%M")
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value:.{_price_decimals(symbol)}f}"))

    bullish_count = sum(state["direction"] == "YUKARI" for state in states)
    bearish_count = sum(state["direction"] == "AŞAĞI" for state in states)
    overall = "YUKARI" if bullish_count > bearish_count else "AŞAĞI" if bearish_count > bullish_count else "RANGE"
    overall_color = VIVID.bull if overall == "YUKARI" else VIVID.bear if overall == "AŞAĞI" else VIVID.amber
    overall_score = sum(state["score"] for state in states) / len(states) if states else 0.0
    fig.text(
        .04, .95, f"{symbol.upper()}  •  5DK / 15DK / 1S / 4S  •  ÇOKLU ZAMAN HARİTASI",
        color=VIVID.text, fontsize=17, fontweight="bold",
    )
    add_banner(fig, f"{'▲' if overall == 'YUKARI' else '▼' if overall == 'AŞAĞI' else '◆'}  4'LÜ ANALİZ YÖNÜ: {overall}", overall_color, left=.04, top=.895)
    add_score_bar(fig, overall_score, label="4'LÜ ANALİZ GÜVENİ", left=.04, bottom=.026, width=.43)
    add_watermark(fig)
    fig.text(.49, .03, "FVG • Order Block • BOS/MSS • EMA20/50", color=VIVID.muted, fontsize=7.5)
    fig.subplots_adjust(left=.04, right=.94, top=.80, bottom=.10, hspace=.28, wspace=.15)
    out_path = os.path.join(tempfile.gettempdir(), f"montana_finans_mtf_{symbol}_{uuid.uuid4().hex[:8]}.png")
    fig.savefig(out_path, dpi=max(settings.chart_dpi, 150), facecolor=VIVID.background, bbox_inches="tight")
    plt.close(fig)
    return out_path


def generate_bist_trade_plan_chart(df: pd.DataFrame, plan) -> str:
    """Referans tasarımlı, hacimsiz LONG/SHORT işlem haritası."""
    data = _normalise_chart_frame(df).tail(160).reset_index(drop=True)
    settings, _theme = _chart_settings()
    theme = THEMES["dark"]
    fig = plt.figure(figsize=(16, 9), facecolor=VIVID.background)
    ax = fig.add_axes([0.055, 0.15, 0.60, 0.68], facecolor=VIVID.panel)
    ax_rail = fig.add_axes([0.685, 0.15, 0.26, 0.68], facecolor=VIVID.panel)
    style_vivid_axes(ax)
    x = _draw_candles(ax, data, theme, width=.72)
    close = data["close"].astype(float)
    for period, color, width in ((20, VIVID.amber, 1.15), (50, VIVID.blue, 1.15)):
        if len(data) >= period:
            ax.plot(x, ema(close, period), color=color, linewidth=width, label=f"EMA {period}", alpha=.94)

    quality_zone = getattr(plan, "quality_zone", None)
    preferred = (
        plan.long if quality_zone is not None and quality_zone.direction == "LONG"
        else plan.short if quality_zone is not None
        else plan.long if plan.long.score >= plan.short.score
        else plan.short
    )
    direction_color = VIVID.bull if preferred.direction == "LONG" else VIVID.bear
    label_items: list[tuple[float, str, str, int]] = []
    zone_start = max(0.0, len(data) - 40.0)
    if quality_zone is not None:
        lower, upper = sorted((float(quality_zone.zone_low), float(quality_zone.zone_high)))
        zone_label = f"{quality_zone.zone_kind} • {quality_zone.direction} BÖLGE"
    else:
        lower, upper = sorted((float(preferred.entry_low), float(preferred.entry_high)))
        zone_label = "BUY ZONE" if preferred.direction == "LONG" else "SELL ZONE"
    ax.add_patch(
        Rectangle(
            (zone_start, lower), len(data) - .2 - zone_start, max(upper - lower, upper * .0003),
            facecolor=direction_color, edgecolor=direction_color, linewidth=.8, alpha=.10, zorder=1,
        )
    )
    zone_rail_items: list[ZoneRailItem] = [
        ZoneRailItem(zone_label, lower, upper, direction_color, direction=preferred.direction)
    ]

    entry_price = float(quality_zone.entry) if quality_zone is not None else float(preferred.trigger)
    stop_price = float(quality_zone.invalidation) if quality_zone is not None else float(preferred.stop_standard)
    selected_targets = (
        [value for value in (quality_zone.target_1, quality_zone.target_2) if value is not None]
        if quality_zone is not None
        else list(preferred.targets)
    )
    marker_x = max(len(data) - 24, 1)
    ax.scatter(
        marker_x,
        entry_price,
        marker="^" if preferred.direction == "LONG" else "v",
        s=54,
        color=direction_color,
        edgecolors=VIVID.text,
        linewidths=.5,
        zorder=18,
    )

    smart = detect_smart_money(data)
    _draw_smart_money_overlay(
        ax,
        smart,
        length=len(data),
        decimals=2,
        detailed=True,
        exclude_zone=(quality_zone.zone_kind, quality_zone.zone_low, quality_zone.zone_high)
        if quality_zone is not None
        else None,
        zone_sink=zone_rail_items,
    )
    ax.axhline(plan.current_price, color=direction_color, linewidth=1.0, linestyle=(0, (6, 4)), alpha=.85)
    ax.axhline(entry_price, color=VIVID.blue, linewidth=1.35)
    ax.axhline(stop_price, color=VIVID.bear, linewidth=1.25, linestyle=(0, (6, 4)))
    label_items.extend([
        (float(plan.current_price), "GÜNCEL", direction_color, 115),
        (entry_price, "ENTRY", VIVID.blue, 114),
        (stop_price, "SL", VIVID.bear, 113),
    ])
    for index, target in enumerate(selected_targets, 1):
        ax.axhline(target, color=VIVID.bull, linewidth=.95, linestyle=(0, (5, 4)), alpha=max(.48, .92 - index * .08))
        label_items.append((float(target), f"TP{index}", VIVID.bull, 108 - index))
    for index, level in enumerate(plan.support_levels, 1):
        if any(abs(float(level) - float(target)) < 0.005 for target in selected_targets):
            continue
        ax.axhline(level, color=VIVID.bull, linewidth=.65, linestyle=(0, (3, 5)), alpha=.48)
        if index <= 2:
            label_items.append((float(level), f"D{index}", VIVID.bull, 76 - index))
    for index, level in enumerate(plan.resistance_levels, 1):
        if any(abs(float(level) - float(target)) < 0.005 for target in selected_targets):
            continue
        ax.axhline(level, color=VIVID.bear, linewidth=.65, linestyle=(0, (3, 5)), alpha=.48)
        if index <= 2:
            label_items.append((float(level), f"R{index}", VIVID.bear, 76 - index))

    plotted = [float(data["low"].min()), float(data["high"].max()), stop_price, *map(float, selected_targets)]
    y_min, y_max = min(plotted), max(plotted)
    padding = max((y_max - y_min) * .12, float(plan.current_price) * .012)
    ax.set_ylim(max(0, y_min - padding), y_max + padding)
    ax.set_xlim(-1, len(data) + 2)
    for actual, display, label, color in _resolve_label_positions(label_items, *ax.get_ylim(), max_labels=12):
        _annotate_vivid_price(ax, actual, display, label, color, decimals=2, fontsize=7.8)

    _format_trading_axis(ax, data, right_margin=2.5, label_format="%d %b")
    draw_zone_rail(
        ax,
        ax_rail,
        zone_rail_items,
        current_price=float(plan.current_price),
        decimals=2,
    )
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value:.2f}"))
    ax.text(
        .012, .982,
        f"RSI {plan.rsi:.1f}   ·   ADX {plan.adx:.1f}   ·   RVOL {plan.relative_volume:.2f}x   ·   ATR %{plan.atr_percent:.2f}",
        transform=ax.transAxes, va="top", color=VIVID.muted, fontsize=7.4, fontweight="bold", fontfamily=MONO_FONT,
    )
    ax.legend(loc="lower left", fontsize=6.5, ncol=3, facecolor=VIVID.panel_alt, edgecolor=VIVID.border, labelcolor=VIVID.text)

    date_label = pd.Timestamp(plan.data_timestamp).strftime("%d.%m.%Y")
    fig.text(.055, .94, f"{plan.symbol}  •  1D  •  {date_label}  •  SMXM BÖLGE PLANI", color=VIVID.text, fontsize=18, fontweight="bold", fontfamily=SANS_FONT)
    decision = quality_zone.direction if quality_zone is not None else plan.preferred_direction or "RANGE"
    arrow = "▲" if decision == "LONG" else "▼" if decision == "SHORT" else "◆"
    add_banner(fig, f"{arrow}  ANA İZLEME SENARYOSU: {decision}", direction_color if decision != "RANGE" else VIVID.amber, left=.055, top=.875)
    add_price_card(fig, plan.current_price, direction_color, decimals=2, x=.86, y=.875)
    rr_candidates = (
        [value for value in (quality_zone.rr_1, quality_zone.rr_2) if value is not None]
        if quality_zone is not None
        else [float(value) for value in preferred.risk_multiples if value is not None]
    )
    display_rr = next((value for value in rr_candidates if value >= 2), rr_candidates[-1] if rr_candidates else 0.0)
    rr_text = f"RR  1:{display_rr:.2f}" if display_rr >= 2 else f"RR YETERSİZ  1:{display_rr:.2f}"
    fig.text(
        .86,
        .81,
        rr_text,
        color=VIVID.text if display_rr >= 2 else VIVID.bear,
        fontsize=14.5,
        fontweight="bold",
        ha="center",
        fontfamily=MONO_FONT,
    )
    setup_score = quality_zone.quality_score if quality_zone is not None else preferred.score
    add_score_bar(fig, setup_score, label="İŞLEM KALİTESİ", left=.07, bottom=.035, width=.49)
    add_watermark(fig)
    last_mss = next((event for event in reversed(smart.structure) if event.kind == "MSS"), None)
    fig.text(
        .41,
        .057,
        f"Trend {plan.trend}  ·  Son MSS {last_mss.direction.title() if last_mss else 'yok'}  ·  {len(zone_rail_items)} aktif bölge",
        color=VIVID.muted,
        fontsize=8,
        fontfamily=SANS_FONT,
    )
    out_path = os.path.join(tempfile.gettempdir(), f"bist_tv_{plan.symbol}_{uuid.uuid4().hex[:8]}.png")
    fig.savefig(out_path, dpi=max(settings.chart_dpi, 150), facecolor=VIVID.background, bbox_inches="tight")
    plt.close(fig)
    return out_path


def generate_intraday_chart(
    df_intraday: pd.DataFrame,
    symbol: str,
    daily_support: Optional[float] = None,
    daily_resistance: Optional[float] = None,
    anomalies: Optional[list] = None,
    previous_close: Optional[float] = None,
    active_alarm_points: Optional[list] = None,
    info_box: Optional[dict] = None,
) -> str:
    """15 dakikalık, hacimsiz ve referans tasarımlı seans grafiği."""
    all_data = _normalise_chart_frame(df_intraday)
    df = all_data.tail(140).reset_index(drop=True)
    settings, _theme = _chart_settings()
    context = {
        "support": daily_support,
        "resistance": daily_resistance,
        "previous_close": previous_close,
        "alarms": active_alarm_points,
        "info": info_box,
        "style": "vivid_reference_v3",
    }
    key = _chart_cache_key(all_data, symbol, "15m", "professional_intraday", context)
    cache = _get_chart_cache()
    cached = cache.get(key)
    if cached:
        return cached

    close = df["close"].astype(float)
    theme = THEMES["dark"]
    state = _technical_visual_state(df, info_box=info_box)
    fig = plt.figure(figsize=(16, 9), facecolor=VIVID.background)
    ax_price = fig.add_axes([.055, .16, .60, .67], facecolor=VIVID.panel)
    ax_rail = fig.add_axes([.685, .16, .26, .67], facecolor=VIVID.panel)
    style_vivid_axes(ax_price)
    x = _draw_candles(ax_price, df, theme)
    label_items: list[tuple[float, str, str, int]] = []

    # Her seans için sıfırlanan VWAP.
    session = pd.to_datetime(df["timestamp"], utc=True).dt.date
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    weighted = typical_price * df["volume"]
    cumulative_volume = df["volume"].groupby(session).cumsum().replace(0, pd.NA)
    vwap = weighted.groupby(session).cumsum() / cumulative_volume
    vwap = vwap.astype(float)
    ax_price.plot(x, vwap, color=VIVID.blue, linewidth=1.0, label="VWAP")

    if len(df) >= 20:
        ax_price.plot(x, ema(close, 20), color=VIVID.amber, linewidth=1.05, label="EMA20")
    if len(df) >= 50:
        ax_price.plot(x, ema(close, 50), color=VIVID.blue, linewidth=1.05, label="EMA50")

    latest_session = session.iloc[-1]
    latest_mask = session == latest_session
    day_open = float(df.loc[latest_mask, "open"].iloc[0])
    day_high = float(df.loc[latest_mask, "high"].max())
    day_low = float(df.loc[latest_mask, "low"].min())
    for level, label, priority in (
        (day_open, "Açılış", 72),
        (day_high, "Gün içi yüksek", 68),
        (day_low, "Gün içi düşük", 68),
    ):
        ax_price.axhline(level, color=VIVID.muted, linestyle=(0, (4, 5)), linewidth=0.7, alpha=0.62)
        label_items.append((level, label.upper(), VIVID.muted, priority))

    if previous_close is not None:
        ax_price.axhline(previous_close, color=VIVID.purple, linestyle=(0, (3, 5)), linewidth=0.85, alpha=0.8)
        label_items.append((previous_close, "ÖNCEKİ", VIVID.purple, 82))
        gap_low, gap_high = sorted((float(previous_close), day_open))
        if gap_high > gap_low:
            ax_price.axhspan(gap_low, gap_high, color=VIVID.purple, alpha=0.06)

    if daily_support is not None:
        ax_price.axhline(daily_support, color=VIVID.bull, linestyle=(0, (6, 4)), linewidth=0.95, alpha=0.8)
        label_items.append((daily_support, "DESTEK", VIVID.bull, 90))
    if daily_resistance is not None:
        ax_price.axhline(daily_resistance, color=VIVID.bear, linestyle=(0, (6, 4)), linewidth=0.95, alpha=0.8)
        label_items.append((daily_resistance, "DİRENÇ", VIVID.bear, 90))

    _draw_anomaly_markers(ax_price, df, anomalies)
    zone_rail_items: list[ZoneRailItem] = []
    _draw_smart_money_overlay(
        ax_price, state["smart"], length=len(df), decimals=_price_decimals(symbol),
        detailed=True, zone_sink=zone_rail_items,
    )
    timestamps = pd.to_datetime(df["timestamp"], utc=True)
    for point in active_alarm_points or []:
        timestamp, price = point[:2]
        target = pd.Timestamp(timestamp)
        target = target.tz_localize("UTC") if target.tzinfo is None else target.tz_convert("UTC")
        xpos = int(np.argmin(np.abs((timestamps - target).dt.total_seconds().to_numpy())))
        ax_price.scatter(xpos, price, marker="*", s=85, color=VIVID.amber, edgecolors=VIVID.text, linewidths=0.45, zorder=18)

    current = state["current"]
    ax_price.axhline(current, color=state["color"], linestyle=(0, (6, 4)), linewidth=1.0, alpha=.78)
    label_items.append((current, "GÜNCEL", state["color"], 110))
    prices = [float(df["low"].min()), float(df["high"].max()), current]
    prices.extend(value for value in (daily_support, daily_resistance, previous_close) if value is not None)
    price_min, price_max = min(prices), max(prices)
    padding = max((price_max - price_min) * .13, current * .008)
    ax_price.set_ylim(max(0, price_min - padding), price_max + padding)
    ax_price.set_xlim(-1, len(df) + 2)
    for actual, display, label, color in _resolve_label_positions(label_items, *ax_price.get_ylim(), max_labels=9):
        _annotate_vivid_price(ax_price, actual, display, label, color, decimals=_price_decimals(symbol), fontsize=7.8)

    _format_trading_axis(ax_price, df, right_margin=2.5, label_format="%d.%m %H:%M")
    ax_price.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value:.{_price_decimals(symbol)}f}"))
    draw_zone_rail(
        ax_price, ax_rail, zone_rail_items, current_price=current, decimals=_price_decimals(symbol),
    )
    _draw_indicator_ribbon(ax_price, state, detailed=True)
    ax_price.set_ylabel("Fiyat", fontsize=8)
    ax_price.legend(loc="lower left", fontsize=6.5, ncol=3, facecolor=VIVID.panel_alt, edgecolor=VIVID.grid, labelcolor=VIVID.text)

    vwap_last = float(vwap.iloc[-1]) if pd.notna(vwap.iloc[-1]) else current
    vwap_ok = (state["direction"] == "YUKARI" and current >= vwap_last) or (state["direction"] == "AŞAĞI" and current <= vwap_last)
    intraday_checklist = [
        ("Seans yönü", state["direction"] != "RANGE"),
        ("VWAP uyumu", vwap_ok),
        ("RSI + MACD uyumu", state["checklist"][2][1]),
        ("FVG / Order Block", bool(state["smart"].fvg or state["smart"].order_blocks)),
        ("BOS / MSS teyidi", bool(state["smart"].structure)),
        ("Günlük seviye", daily_support is not None or daily_resistance is not None),
    ]
    score = _numeric_score(info_box)
    if score is None:
        score = sum(ok for _label, ok in intraday_checklist) / len(intraday_checklist) * 100
    date_label = pd.Timestamp(df.iloc[-1]["timestamp"]).strftime("%d.%m.%Y %H:%M")
    fig.text(.055, .94, f"{symbol.upper()}  •  15DK  •  {date_label}  •  GÜN İÇİ", color=VIVID.text, fontsize=18, fontweight="bold", fontfamily=SANS_FONT)
    arrow = "▲" if state["direction"] == "YUKARI" else "▼" if state["direction"] == "AŞAĞI" else "◆"
    add_banner(fig, f"{arrow}  GÜN İÇİ YÖN: {state['direction']}", state["color"], left=.055, top=.875)
    add_price_card(fig, current, state["color"], decimals=_price_decimals(symbol), x=.86, y=.875)
    add_score_bar(fig, score, label="GÜN İÇİ GÜVEN")
    add_watermark(fig)
    last_mss = next((event for event in reversed(state["smart"].structure) if event.kind == "MSS"), None)
    fig.text(
        .41, .057,
        f"Trend {state['direction']}  ·  Son MSS {last_mss.direction if last_mss else 'yok'}  ·  {len(zone_rail_items)} aktif bölge",
        color=VIVID.muted, fontsize=8.0, fontfamily=SANS_FONT,
    )

    out_path = os.path.join(tempfile.gettempdir(), f"mergen_intraday_chart_{symbol}_{uuid.uuid4().hex[:8]}.png")
    fig.savefig(out_path, dpi=max(settings.chart_dpi, 140), facecolor=VIVID.background, bbox_inches="tight")
    plt.close(fig)
    cache.put(key, out_path)
    return out_path


def _long_term_resample(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    from app.analysis.timeframe_levels_engine import _resample

    normalized = timeframe.strip().lower()
    if normalized in {"weekly", "haftalik", "haftalık", "1wk", "1w"}:
        return _resample(df, "W-FRI")
    if normalized in {"monthly", "aylik", "aylık", "1mo", "1m"}:
        return _resample(df, "ME")
    raise ValueError("Uzun vadeli grafik zaman dilimi 'weekly' veya 'monthly' olmalı.")


def generate_long_term_chart(
    df_daily: pd.DataFrame,
    symbol: str,
    *,
    timeframe: str = "weekly",
    current_price: Optional[float] = None,
    user_target: Optional[float] = None,
    roadmap=None,
    long_term_scenario=None,
    corporate_actions: Optional[list] = None,
    valuation_status: Optional[str] = None,
    speculation_risk: Optional[str] = None,
    info_box: Optional[dict] = None,
) -> str:
    """Aşama 5e haftalık/aylık, yalnızca logaritmik uzun vadeli grafik."""
    data = _long_term_resample(df_daily, timeframe)
    if data is None or len(data) < 8:
        raise ValueError("Uzun vadeli grafik için yeterli tamamlanmış mum yok.")
    data = data.sort_values("timestamp").reset_index(drop=True)
    if (data[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("Logaritmik grafik için fiyatların tamamı pozitif olmalı.")

    settings, _theme = _chart_settings()
    theme = THEMES["dark"]
    context = {
        "timeframe": timeframe, "current_price": current_price, "user_target": user_target,
        "roadmap": [getattr(step, "mid", None) for step in getattr(roadmap, "steps", [])],
        "valuation": valuation_status, "speculation": speculation_risk,
        "style": "vivid_reference_v3",
    }
    key = _chart_cache_key(data, symbol, timeframe, "long_term_log", context)
    cache = _get_chart_cache()
    cached = cache.get(key)
    if cached:
        return cached

    fig = plt.figure(figsize=(16, 9), facecolor=VIVID.background)
    ax = fig.add_axes([.055, .16, .60, .67], facecolor=VIVID.panel)
    ax_rail = fig.add_axes([.685, .16, .26, .67], facecolor=VIVID.panel)
    style_vivid_axes(ax)
    x = _draw_candles(ax, data, theme, width=0.62)
    ax.set_yscale("log")

    close = data["close"].astype(float)
    normalized = timeframe.strip().lower()
    ema_periods = (10, 20) if normalized in {"monthly", "aylik", "aylık", "1mo", "1m"} else (20, 50)
    colors = (VIVID.amber, VIVID.blue)
    for period, color in zip(ema_periods, colors):
        if len(data) >= period:
            ax.plot(x, ema(close, period), color=color, linewidth=0.9, label=f"EMA{period}")

    smart = detect_smart_money(data)
    zone_rail_items: list[ZoneRailItem] = []
    _draw_smart_money_overlay(
        ax, smart, length=len(data), decimals=_price_decimals(symbol), detailed=True,
        zone_sink=zone_rail_items,
    )

    # Logaritmik regresyon kanalı.
    log_close = np.log(close.to_numpy())
    slope, intercept = np.polyfit(x, log_close, 1)
    trend = intercept + slope * x
    residual = float(np.std(log_close - trend))
    ax.plot(x, np.exp(trend), color=VIVID.muted, linewidth=1.0, label="Log trend")
    ax.plot(x, np.exp(trend + 2 * residual), color=VIVID.faint, linestyle="--", linewidth=0.7)
    ax.plot(x, np.exp(trend - 2 * residual), color=VIVID.faint, linestyle="--", linewidth=0.7)
    ax.fill_between(x, np.exp(trend - 2 * residual), np.exp(trend + 2 * residual), color=VIVID.blue, alpha=0.035)

    historical_low = float(data["low"].min())
    historical_high = float(data["high"].max())
    price_range = historical_high - historical_low
    for ratio, color in ((0.382, VIVID.bull), (0.5, VIVID.muted), (0.618, VIVID.bear)):
        level = historical_high - price_range * ratio
        ax.axhline(level, color=color, linestyle=":", linewidth=0.65, alpha=0.65)
    for ratio in (1.272, 1.618):
        level = historical_low + price_range * ratio
        ax.axhline(level, color=VIVID.amber, linestyle="--", linewidth=0.55, alpha=0.5)

    # Çok yıllık destek/direnç ve uygun hacim bölgeleri.
    for quantile, _label, color in ((0.10, "Ana destek", VIVID.bull), (0.90, "Ana direnç", VIVID.bear)):
        level = float(close.quantile(quantile))
        ax.axhspan(level * 0.98, level * 1.02, color=color, alpha=0.06)
    typical = (data["high"] + data["low"] + data["close"]) / 3
    if float(data["volume"].sum()) > 0:
        bins = np.geomspace(historical_low, historical_high, min(25, max(8, len(data) // 4)))
        bucket = pd.cut(typical, bins=bins, include_lowest=True)
        profile = data.groupby(bucket, observed=True)["volume"].sum().sort_values(ascending=False).head(3)
        for interval in profile.index:
            ax.axhspan(float(interval.left), float(interval.right), color=theme.muted, alpha=0.045)

    display_current = float(current_price) if current_price is not None and current_price > 0 else float(close.iloc[-1])
    trend_up = slope > 0
    trend_color = VIVID.bull if trend_up else VIVID.bear
    ax.axhline(display_current, color=trend_color, linewidth=1.15, linestyle=(0, (6, 4)), label="Güncel fiyat")
    _annotate_vivid_price(ax, display_current, display_current, "GÜNCEL", trend_color, decimals=_price_decimals(symbol))
    if user_target is not None and user_target > 0:
        ax.axhline(user_target, color=VIVID.amber, linestyle="-.", linewidth=1.15, label="Kullanıcı hedefi")
        _annotate_vivid_price(ax, float(user_target), float(user_target), "HEDEF", VIVID.amber, decimals=_price_decimals(symbol))
    for step in getattr(roadmap, "steps", []) or []:
        if getattr(step, "mid", None):
            ax.axhline(step.mid, color=VIVID.amber, linestyle=":", linewidth=0.75, alpha=0.75)

    if long_term_scenario is not None:
        extreme = getattr(long_term_scenario, "extreme_bull", None)
        bottom = getattr(long_term_scenario, "long_term_bottom", None) or getattr(long_term_scenario, "extreme_negative", None)
        if extreme is not None:
            ax.axhspan(extreme.low, extreme.high, color=theme.up, alpha=0.08, label="Aşırı boğa bölgesi")
        if bottom is not None:
            ax.axhspan(bottom.low, bottom.high, color=theme.down, alpha=0.08, label="Ana dip senaryosu")

    timestamps = pd.to_datetime(data["timestamp"], utc=True)
    for event in corporate_actions or []:
        event_date = getattr(event, "effective_date", None)
        if event_date is None:
            continue
        distances = abs(timestamps.dt.date.apply(lambda value: (value - event_date).days))
        position = int(distances.to_numpy().argmin())
        if distances.iloc[position] <= 40:
            ax.axvline(position, color=VIVID.faint, linestyle=":", linewidth=0.6)

    ax.set_ylabel("Fiyat (log)", fontsize=7)
    label = "Aylık" if normalized in {"monthly", "aylik", "aylık", "1mo", "1m"} else "Haftalık"
    ax.legend(loc="lower left", fontsize=6.5, ncol=4, facecolor=VIVID.panel_alt, edgecolor=VIVID.grid, labelcolor=VIVID.text)
    _format_trading_axis(ax, data, right_margin=2.5, label_format="%m.%Y")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value:.{_price_decimals(symbol)}f}"))
    draw_zone_rail(
        ax, ax_rail, zone_rail_items, current_price=display_current, decimals=_price_decimals(symbol),
    )
    date_label = pd.Timestamp(data.iloc[-1]["timestamp"]).strftime("%m.%Y")
    fig.text(.055, .94, f"{symbol.upper()}  •  {label.upper()}  •  {date_label}  •  LOGARİTMİK", color=VIVID.text, fontsize=18, fontweight="bold", fontfamily=SANS_FONT)
    add_banner(fig, f"{'▲' if trend_up else '▼'}  UZUN VADELİ YÖN: {'YUKARI' if trend_up else 'AŞAĞI'}", trend_color, left=.055, top=.875)
    add_price_card(fig, display_current, trend_color, decimals=_price_decimals(symbol), x=.86, y=.875)
    checklist = [
        ("Log trend eğimi", trend_up),
        ("Fiyat log trend üstünde", display_current >= float(np.exp(trend[-1]))),
        ("EMA ana trend uyumu", len(data) < ema_periods[1] or (close.iloc[-1] >= ema(close, ema_periods[1]).iloc[-1]) == trend_up),
        ("FVG / Order Block", bool(smart.fvg or smart.order_blocks)),
        ("BOS / MSS", bool(smart.structure)),
        ("Hedef mesafesi makul", bool(user_target and display_current and user_target / display_current <= 2.5)),
    ]
    score = sum(ok for _name, ok in checklist) / len(checklist) * 100
    add_score_bar(fig, score, label="UZUN VADE GÜVENİ")
    add_watermark(fig)
    last_mss = next((event for event in reversed(smart.structure) if event.kind == "MSS"), None)
    fig.text(
        .41, .057,
        f"Trend {'YUKARI' if trend_up else 'AŞAĞI'}  ·  Son MSS {last_mss.direction if last_mss else 'yok'}  ·  {len(zone_rail_items)} aktif bölge",
        color=VIVID.muted, fontsize=8.0, fontfamily=SANS_FONT,
    )
    out_path = os.path.join(tempfile.gettempdir(), f"mergen_long_{symbol}_{uuid.uuid4().hex[:8]}.png")
    fig.savefig(out_path, dpi=max(settings.chart_dpi, 140), facecolor=VIVID.background, bbox_inches="tight")
    plt.close(fig)
    cache.put(key, out_path)
    return out_path


def delete_chart_file(path: str) -> bool:
    """Telegram'a gonderim sonrasi gecici grafik dosyasini guvenli sekilde siler."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
            return True
    except OSError:
        pass
    return False


@contextmanager
def temporary_chart(path: str):
    """Grafik dosyasini kullanip, blok sonunda (basarili/basarisiz fark etmeksizin) siler."""
    try:
        yield path
    finally:
        delete_chart_file(path)
