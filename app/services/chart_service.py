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

from app.analysis.indicator_engine import bollinger_bands, ema, macd, rsi
from app.analysis.support_resistance_engine import SupportResistanceResult

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
    "dark": ChartTheme("dark", "#10141c", "#171d27", "#edf2f7", "#3b4657", "#35c78a", "#ff6b78", "#35c78a", "#ff6b78", "#6ea8fe", "#a0aec0"),
}


def _chart_settings():
    from app.config.settings import get_settings

    settings = get_settings()
    theme = THEMES.get(settings.chart_theme, THEMES["light"])
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
    """Fiyat + EMA20/50/200 + destek/direnc + giris/stop/hedef + hacim grafigi
    uretir. Gecici bir PNG dosyasi olusturur ve dosya yolunu doner.
    Cagiran taraf, Telegram'a gonderdikten SONRA dosyayi silmelidir
    (bkz. delete_chart_file).
    """
    df = df.sort_values("timestamp").reset_index(drop=True)
    close = df["close"]

    fig, (ax_price, ax_vol) = plt.subplots(
        2, 1, figsize=(10, 6), gridspec_kw={"height_ratios": [3, 1]}, sharex=True
    )

    ax_price.plot(df["timestamp"], close, color="#1f77b4", linewidth=1.3, label="Kapanis")
    if len(df) >= 20:
        ax_price.plot(df["timestamp"], ema(close, 20), color="#ff7f0e", linewidth=0.9, label="EMA20")
    if len(df) >= 50:
        ax_price.plot(df["timestamp"], ema(close, 50), color="#2ca02c", linewidth=0.9, label="EMA50")
    if len(df) >= 200:
        ax_price.plot(df["timestamp"], ema(close, 200), color="#9467bd", linewidth=0.9, label="EMA200")

    if sr is not None:
        for level, label, color in [
            (sr.support_1, "Destek 1", "#2ca02c"),
            (sr.main_support, "Ana Destek", "#006400"),
            (sr.resistance_1, "Direnc 1", "#d62728"),
            (sr.main_resistance, "Ana Direnc", "#8b0000"),
        ]:
            if level is not None:
                ax_price.axhline(y=level, color=color, linestyle="--", linewidth=0.8, alpha=0.7)
                ax_price.text(df["timestamp"].iloc[-1], level, f" {label}", fontsize=7, color=color, va="center")

    if stop_price is not None:
        ax_price.axhline(y=stop_price, color="red", linestyle=":", linewidth=1.0, alpha=0.8)
    if targets:
        for t in targets:
            if t is not None:
                ax_price.axhline(y=t, color="blue", linestyle=":", linewidth=0.8, alpha=0.6)

    ax_price.set_title(f"{symbol} - Fiyat Grafigi")
    ax_price.legend(loc="upper left", fontsize=7)
    ax_price.grid(alpha=0.2)

    ax_vol.bar(df["timestamp"], df["volume"], color="#7f7f7f", width=1.0)
    ax_vol.set_ylabel("Hacim", fontsize=8)
    ax_vol.grid(alpha=0.2)

    fig.autofmt_xdate()
    fig.tight_layout()

    out_path = os.path.join(tempfile.gettempdir(), f"mergen_chart_{symbol}_{uuid.uuid4().hex[:8]}.png")
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return out_path


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

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    ax1.plot(merged["date_key"], norm_stock, label=symbol, color="#1f77b4")
    ax1.plot(merged["date_key"], norm_index, label=index_symbol, color="#7f7f7f")
    ax1.set_title(f"{symbol} vs {index_symbol} - Normalize Performans")
    ax1.legend(fontsize=8)
    ax1.grid(alpha=0.2)

    ax2.plot(merged["date_key"], ratio_norm, color="#d62728")
    ax2.axhline(y=100, color="black", linewidth=0.6, linestyle="--")
    ax2.set_title("Relatif Guc Orani (Hisse / Endeks, normalize)")
    ax2.grid(alpha=0.2)

    fig.autofmt_xdate()
    fig.tight_layout()

    out_path = os.path.join(tempfile.gettempdir(), f"mergen_rschart_{symbol}_{uuid.uuid4().hex[:8]}.png")
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return out_path


_TIMEFRAME_LEVEL_STYLE = {
    "gunluk": {"support": "#2ca02c", "resistance": "#d62728", "alpha": 0.55, "ls": "--"},
    "haftalik": {"support": "#17becf", "resistance": "#e377c2", "alpha": 0.55, "ls": "-."},
    "aylik": {"support": "#8c564b", "resistance": "#bcbd22", "alpha": 0.55, "ls": ":"},
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
        color = "#006400" if zone.kind == "destek" else "#8b0000"
        ax.axhspan(zone.low, zone.high, color=color, alpha=0.12)


def _draw_price_scenario_bands(ax, price_scenario) -> None:
    """Yakin geri cekilme / ana dip bolgesi / guclu yukselis senaryosu bantlari."""
    if price_scenario is None or not getattr(price_scenario, "reliable", False):
        return
    for tier in getattr(price_scenario, "support_tiers", []) or []:
        ax.axhspan(tier.low, tier.high, color="#2ca02c", alpha=0.08)
    for tier in getattr(price_scenario, "resistance_tiers", []) or []:
        ax.axhspan(tier.low, tier.high, color="#d62728", alpha=0.08)


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
    df = df.sort_values("timestamp").reset_index(drop=True)
    if df.empty:
        raise ValueError("Grafik için OHLCV verisi boş.")
    settings, theme = _chart_settings()
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
        "theme": theme.name,
        "mode": chart_mode,
    }
    key = _chart_cache_key(df, symbol, "1d", f"professional_daily_{chart_mode}", context_signature)
    cache = _get_chart_cache()
    cached = cache.get(key)
    if cached:
        return cached

    close = df["close"].astype(float)
    fig = plt.figure(figsize=(settings.chart_width, settings.chart_height))
    if detailed:
        gs = fig.add_gridspec(4, 1, height_ratios=[4.5, 1.15, 1.0, 1.1], hspace=0.05)
        ax_price = fig.add_subplot(gs[0])
        ax_vol = fig.add_subplot(gs[1], sharex=ax_price)
        ax_rsi = fig.add_subplot(gs[2], sharex=ax_price)
        ax_macd = fig.add_subplot(gs[3], sharex=ax_price)
        axes = (ax_price, ax_vol, ax_rsi, ax_macd)
    else:
        gs = fig.add_gridspec(3, 1, height_ratios=[4.8, 1.2, 1.0], hspace=0.05)
        ax_price = fig.add_subplot(gs[0])
        ax_vol = fig.add_subplot(gs[1], sharex=ax_price)
        ax_rsi = fig.add_subplot(gs[2], sharex=ax_price)
        ax_macd = None
        axes = (ax_price, ax_vol, ax_rsi)
    _style_axes(fig, axes, theme)
    x = _draw_candles(ax_price, df, theme)
    label_items: list[tuple[float, str, str, int]] = []

    ema_styles = [(20, "#f59e0b"), (50, "#3b82f6")]
    if detailed:
        ema_styles.extend([(100, "#8b5cf6"), (200, "#8c564b")])
    for period, color in ema_styles:
        if len(df) >= period:
            ax_price.plot(x, ema(close, period), color=color, linewidth=0.9, label=f"EMA{period}", alpha=0.95)

    if detailed and len(df) >= 20:
        upper, middle, lower, _ = bollinger_bands(close, 20, 2.0)
        ax_price.plot(x, upper, color=theme.muted, linewidth=0.65, linestyle="--", alpha=0.7, label="Bollinger")
        ax_price.plot(x, middle, color=theme.muted, linewidth=0.55, alpha=0.45)
        ax_price.plot(x, lower, color=theme.muted, linewidth=0.65, linestyle="--", alpha=0.7)
        ax_price.fill_between(x, lower.to_numpy(float), upper.to_numpy(float), color=theme.muted, alpha=0.06)

    if detailed:
        typical = (df["high"] + df["low"] + df["close"]) / 3
        cumulative_volume = df["volume"].cumsum().replace(0, pd.NA)
        anchored_vwap = (typical * df["volume"]).cumsum() / cumulative_volume
        ax_price.plot(x, anchored_vwap.astype(float), color="#ec4899", linewidth=0.95, label="VWAP")

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

    current_price = float(close.iloc[-1])
    ax_price.axhline(current_price, color=theme.foreground, linestyle="-", linewidth=0.7, alpha=0.55)
    label_items.append((current_price, f"Güncel {current_price:.2f}", theme.foreground, 99))

    if entry_zone and entry_zone[0] is not None and entry_zone[1] is not None:
        ax_price.axhspan(entry_zone[0], entry_zone[1], color=theme.accent, alpha=0.12)
        label_items.append(((entry_zone[0] + entry_zone[1]) / 2, "Alım bölgesi", theme.accent, 92))
    if entry_trigger is not None:
        ax_price.axhline(entry_trigger, color=theme.accent, linestyle="-.", linewidth=0.9)
        label_items.append((entry_trigger, f"Tetik {entry_trigger:.2f}", theme.accent, 98))
    if stop_price is not None:
        ax_price.axhline(stop_price, color=theme.down, linestyle=":", linewidth=1.1)
        label_items.append((stop_price, f"Stop {stop_price:.2f}", theme.down, 100))
    visible_targets = list(targets or []) if detailed else list(targets or [])[:2]
    for index, target in enumerate(visible_targets, start=1):
        if target is not None:
            ax_price.axhline(target, color=theme.accent, linestyle=":", linewidth=0.75, alpha=0.8)
            label_items.append((target, f"H{index} {target:.2f}", theme.accent, 96 - index))

    if detailed:
        _draw_anomaly_markers(ax_price, df, anomalies)
        _draw_news_markers(ax_price, df, news_markers)
    timestamps = pd.to_datetime(df["timestamp"], utc=True)
    for markers, marker, color, label in (
        (breakout_markers, "^", theme.up, "Kırılım"),
        (retest_markers, "o", "#f59e0b", "Retest"),
    ):
        for point in ((markers or []) if detailed else []):
            ts, price = point
            target = pd.Timestamp(ts)
            target = target.tz_localize("UTC") if target.tzinfo is None else target.tz_convert("UTC")
            xpos = int(np.argmin(np.abs((timestamps - target).dt.total_seconds().to_numpy())))
            ax_price.scatter(xpos, price, marker=marker, s=36, color=color, label=label, zorder=7)
    if detailed and last_signal_point:
        ts, price = last_signal_point
        target = pd.Timestamp(ts)
        target = target.tz_localize("UTC") if target.tzinfo is None else target.tz_convert("UTC")
        xpos = int(np.argmin(np.abs((timestamps - target).dt.total_seconds().to_numpy())))
        ax_price.scatter(xpos, price, marker="*", s=90, color="#f59e0b", edgecolors=theme.foreground, linewidths=0.4, zorder=8)

    if info_box:
        lines = [f"{key}: {value}" for key, value in info_box.items() if value is not None]
        if lines:
            ax_price.text(
                0.012,
                0.985,
                "\n".join(lines[:10] if not detailed else lines[:10]),
                transform=ax_price.transAxes,
                fontsize=7.2,
                va="top",
                ha="left",
                family="monospace",
                color=theme.foreground,
                bbox={"boxstyle": "round,pad=0.45", "facecolor": theme.panel, "alpha": 0.9, "edgecolor": theme.grid},
                zorder=10,
            )

    y_min, y_max = ax_price.get_ylim()
    for actual, display, label, color in _resolve_label_positions(label_items, y_min, y_max):
        ax_price.annotate(
            label,
            xy=(x[-1], actual),
            xytext=(x[-1] + 1.0, display),
            fontsize=6.3,
            color=color,
            va="center",
            arrowprops={"arrowstyle": "-", "color": color, "lw": 0.45, "alpha": 0.7},
            bbox={"boxstyle": "round,pad=0.16", "facecolor": theme.panel, "edgecolor": color, "alpha": 0.85},
        )

    mode_label = "Standart" if not detailed else "Detaylı"
    ax_price.set_title(f"MERGEN QUANT — {symbol.upper()} {mode_label} Teknik Analiz", fontsize=13, fontweight="bold", loc="left")
    ax_price.set_ylabel("Fiyat", fontsize=8)
    handles, labels = ax_price.get_legend_handles_labels()
    if handles:
        unique = dict(zip(labels, handles))
        ax_price.legend(unique.values(), unique.keys(), loc="upper right", fontsize=6.5, ncol=3, framealpha=0.35)

    up = df["close"] >= df["open"]
    volume_colors = np.where(up, theme.up, theme.down)
    ax_vol.bar(x, df["volume"], color=volume_colors, width=0.68, alpha=0.78)
    volume_avg = df["volume"].rolling(20, min_periods=3).mean()
    ax_vol.plot(x, volume_avg, color="#f59e0b", linewidth=0.8, label="20 ort.")
    spikes = df["volume"] >= volume_avg * 2.0
    if spikes.any():
        ax_vol.scatter(x[spikes.to_numpy()], df.loc[spikes, "volume"], marker="^", s=18, color="#f59e0b", zorder=5)
    ax_vol.set_ylabel("Hacim", fontsize=8)

    rsi_values = rsi(close, 14)
    ax_rsi.plot(x, rsi_values, color="#8b5cf6", linewidth=0.9)
    ax_rsi.axhspan(70, 100, color=theme.down, alpha=0.07)
    ax_rsi.axhspan(0, 30, color=theme.up, alpha=0.07)
    ax_rsi.axhline(70, color=theme.down, linestyle="--", linewidth=0.55)
    ax_rsi.axhline(30, color=theme.up, linestyle="--", linewidth=0.55)
    ax_rsi.set_ylim(0, 100)
    ax_rsi.set_ylabel("RSI", fontsize=8)

    if detailed and ax_macd is not None:
        macd_line, signal_line, histogram = macd(close)
        ax_macd.plot(x, macd_line, color="#3b82f6", linewidth=0.8, label="MACD")
        ax_macd.plot(x, signal_line, color="#f59e0b", linewidth=0.8, label="Sinyal")
        hist_values = histogram.fillna(0).to_numpy(float)
        ax_macd.bar(x, hist_values, color=np.where(hist_values >= 0, theme.up, theme.down), width=0.68, alpha=0.55)
        cross_up = (macd_line.shift(1) <= signal_line.shift(1)) & (macd_line > signal_line)
        cross_down = (macd_line.shift(1) >= signal_line.shift(1)) & (macd_line < signal_line)
        ax_macd.scatter(x[cross_up.fillna(False).to_numpy()], macd_line[cross_up.fillna(False)], marker="^", s=16, color=theme.up)
        ax_macd.scatter(x[cross_down.fillna(False).to_numpy()], macd_line[cross_down.fillna(False)], marker="v", s=16, color=theme.down)
        ax_macd.axhline(0, color=theme.grid, linewidth=0.5)
        ax_macd.set_ylabel("MACD", fontsize=8)
        ax_macd.legend(loc="upper left", fontsize=6, ncol=2, framealpha=0.3)

    for ax in (ax_price, ax_vol):
        plt.setp(ax.get_xticklabels(), visible=False)
    _format_trading_axis(ax_macd if ax_macd is not None else ax_rsi, df)
    fig.text(0.5, 0.012, "Teknik senaryo çalışmasıdır, yatırım tavsiyesi değildir.", ha="center", color=theme.muted, fontsize=7)
    fig.subplots_adjust(left=0.07, right=0.95, top=0.95, bottom=0.07)

    out_path = os.path.join(tempfile.gettempdir(), f"mergen_pro_chart_{symbol}_{uuid.uuid4().hex[:8]}.png")
    fig.savefig(out_path, dpi=settings.chart_dpi, facecolor=fig.get_facecolor())
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
    """Asama 5c profesyonel gunluk grafik: candlestick + EMA20/50/100/200 +
    Bollinger + gunluk/haftalik/aylik destek-direnc + cakisan bolgeler + giris/
    stop/hedef + senaryo bantlari + haber/anomali isaretleri + hacim/RSI/MACD.

    Mevcut `generate_price_chart` DEGISTIRILMEZ/KALDIRILMAZ; bu, ONA EK yeni
    ve daha kapsamli bir grafik fonksiyonudur. Herhangi bir asama basarisiz
    olursa (ornegin bir gostergenin hesaplanamamasi), o katman sessizce
    atlanir; grafik yine de uretilir.
    """
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


def generate_bist_trade_plan_chart(df: pd.DataFrame, plan) -> str:
    """Long/short giris, TP1-TP5 ve katmanli stop seviyelerini canli grafikte gosterir."""
    data = df.sort_values("timestamp").tail(140).reset_index(drop=True)
    if data.empty:
        raise ValueError("Grafik icin OHLCV verisi bos.")
    settings, theme = _chart_settings()
    fig, (ax, ax_vol) = plt.subplots(
        2, 1, figsize=(settings.chart_width, settings.chart_height),
        gridspec_kw={"height_ratios": [5, 1]}, sharex=True,
    )
    _style_axes(fig, (ax, ax_vol), theme)
    x = _draw_candles(ax, data, theme)
    close = data["close"].astype(float)
    for period, color in ((20, "#22d3ee"), (50, "#f59e0b"), (200, "#a78bfa")):
        if len(data) >= period:
            ax.plot(x, ema(close, period), color=color, linewidth=1.0, label=f"EMA{period}")
    ax.axhspan(plan.long.entry_low, plan.long.entry_high, color="#10b981", alpha=.18, label="LONG GIRIS")
    ax.axhspan(plan.short.entry_low, plan.short.entry_high, color="#f43f5e", alpha=.16, label="SHORT GIRIS")
    for value in plan.support_levels:
        ax.axhline(value, color="#34d399", linestyle="--", linewidth=.65, alpha=.55)
    for value in plan.resistance_levels:
        ax.axhline(value, color="#fb7185", linestyle="--", linewidth=.65, alpha=.55)
    for prefix, side, color in (("L", plan.long, "#2dd4bf"), ("S", plan.short, "#fb7185")):
        ax.axhline(side.trigger, color=color, linestyle="-.", linewidth=1.1)
        ax.text(len(data) - 1, side.trigger, f" {prefix} TETIK {side.trigger:.2f}", color=color, fontsize=6)
        for index, target in enumerate(side.targets, 1):
            ax.axhline(target, color=color, linestyle=":", linewidth=.6, alpha=.7)
            ax.text(0, target, f" {prefix}-TP{index} {target:.2f}", color=color, fontsize=5.5)
        for label, stop in (("A", side.stop_aggressive), ("S", side.stop_standard), ("K", side.stop_conservative)):
            ax.axhline(stop, color="#ef4444", linestyle=":", linewidth=.7, alpha=.65)
            ax.text(len(data) - 1, stop, f" {prefix}-SL-{label} {stop:.2f}", color="#ef4444", fontsize=5.3)
    ax.text(
        .01, .98,
        f"FIYAT {plan.current_price:.2f} TL | ATR %{plan.atr_percent:.2f} | RSI {plan.rsi:.1f}\n"
        f"LONG {plan.long.score}/100 | SHORT {plan.short.score}/100",
        transform=ax.transAxes, va="top", color=theme.foreground, fontsize=8,
        bbox={"boxstyle": "round", "facecolor": theme.panel, "edgecolor": theme.accent, "alpha": .92},
    )
    ax.set_title(f"MERGEN QUANT • {plan.symbol} • LONG / SHORT ISLEM HARITASI", fontweight="bold", loc="left")
    ax.legend(loc="upper right", fontsize=6, ncol=3, framealpha=.35)
    up = data["close"] >= data["open"]
    ax_vol.bar(x, data["volume"], color=np.where(up, theme.up, theme.down), width=.68, alpha=.75)
    ax_vol.plot(x, data["volume"].rolling(20).mean(), color="#fbbf24", linewidth=.8)
    _format_trading_axis(ax_vol, data)
    fig.tight_layout()
    out_path = os.path.join(tempfile.gettempdir(), f"bist_plan_{plan.symbol}_{uuid.uuid4().hex[:8]}.png")
    fig.savefig(out_path, dpi=settings.chart_dpi, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path


def generate_multi_timeframe_chart(frames: dict[str, pd.DataFrame], symbol: str) -> str:
    """5dk, 15dk, 1s ve 4s mumlarını tek, telefonda okunabilir dört panoda çizer."""
    settings, _ = _chart_settings()
    background, panel, grid = "#040910", "#08131e", "#203448"
    bullish, bearish, foreground = "#00f5a0", "#ff2d55", "#e8f3ff"
    theme = ChartTheme("montana", background, panel, foreground, grid, bullish, bearish,
                       bullish, bearish, "#00c8ff", "#8aa0b5")
    fig, axes = plt.subplots(2, 2, figsize=(14, 9), facecolor=background)
    from app.analysis.smart_money_engine import detect_smart_money
    for ax, label in zip(axes.flat, ("5 dk", "15 dk", "1 saat", "4 saat")):
        data = frames.get(label, pd.DataFrame()).sort_values("timestamp").tail(90).reset_index(drop=True)
        ax.set_facecolor(panel); ax.grid(True, color=grid, linewidth=.45, alpha=.48)
        for spine in ax.spines.values(): spine.set_color(grid)
        ax.tick_params(colors="#8aa0b5", labelsize=7); ax.yaxis.tick_right()
        if data.empty:
            ax.text(.5, .5, "Veri yok", transform=ax.transAxes, ha="center", color=foreground)
            continue
        x = _draw_candles(ax, data, theme, width=.68)
        close = data["close"].astype(float)
        if len(data) >= 20: ax.plot(x, ema(close, 20), color="#ffd43b", linewidth=1, label="EMA20")
        if len(data) >= 50: ax.plot(x, ema(close, 50), color="#00c8ff", linewidth=1, label="EMA50")
        smart = detect_smart_money(data)
        for zone in smart.fvg:
            color = "#00c8ff" if zone.direction == "bullish" else "#ff8a3d"
            ax.axhspan(zone.low, zone.high, xmin=max(zone.index / len(data), 0), xmax=1, color=color, alpha=.10)
            ax.text(zone.index, zone.high, "FVG", color=color, fontsize=6)
        for zone in smart.order_blocks:
            color = bullish if zone.direction == "bullish" else bearish
            ax.axhspan(zone.low, zone.high, xmin=max(zone.index / len(data), 0), xmax=1, color=color, alpha=.10)
            ax.text(zone.index, zone.low, "OB", color=color, fontsize=6)
        for event in smart.structure:
            color = bullish if event.direction == "bullish" else bearish
            ax.scatter(event.index, event.price, color=color, s=20, marker="^" if event.direction == "bullish" else "v")
            ax.text(event.index, event.price, event.kind, color=color, fontsize=6)
        direction = "YUKARI" if close.iloc[-1] > ema(close, 20).iloc[-1] else "AŞAĞI"
        color = bullish if direction == "YUKARI" else bearish
        ax.set_title(f"{label.upper()}  •  {direction}  •  {close.iloc[-1]:.2f} TL",
                     color=color, fontweight="bold", fontsize=10, loc="left")
        _format_trading_axis(ax, data, right_margin=5)
    fig.suptitle(f"MONTANA MELİH • {symbol.upper()} ÇOKLU ZAMAN HARİTASI",
                 color=foreground, fontsize=15, fontweight="bold", x=.03, ha="left")
    fig.text(.03, .015, "FVG • Order Block • BOS/MSS • EMA20/50 | Yatırım tavsiyesi değildir",
             color="#7890a6", fontsize=8)
    fig.subplots_adjust(left=.035, right=.96, top=.91, bottom=.07, hspace=.25, wspace=.08)
    out_path = os.path.join(tempfile.gettempdir(), f"montana_mtf_{symbol}_{uuid.uuid4().hex[:8]}.png")
    fig.savefig(out_path, dpi=max(settings.chart_dpi, 150), facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path


def generate_bist_trade_plan_chart(df: pd.DataFrame, plan) -> str:
    """Hacimsiz, tek panelli ve TradingView esintili BIST işlem haritası."""
    data = df.sort_values("timestamp").tail(160).reset_index(drop=True)
    if data.empty:
        raise ValueError("Grafik için OHLCV verisi boş.")

    settings, _ = _chart_settings()
    background, panel, grid = "#040910", "#08131e", "#203448"
    foreground, bullish, bearish = "#e8f3ff", "#00f5a0", "#ff2d55"
    fig, ax = plt.subplots(figsize=(14, 8), facecolor=background)
    ax.set_facecolor(panel)
    ax.grid(True, color=grid, linewidth=.55, alpha=.52)
    for spine in ax.spines.values():
        spine.set_color(grid)
    ax.tick_params(colors="#91a4b7", labelsize=8)
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")

    tv_theme = ChartTheme("tv", background, panel, foreground, grid, bullish, bearish,
                          bullish, bearish, "#00b8ff", "#91a4b7")
    x = _draw_candles(ax, data, tv_theme, width=.72)
    close = data["close"].astype(float)
    for period, color, width in ((20, "#ffd43b", 1.15), (50, "#00b8ff", 1.15), (200, "#b26bff", 1.05)):
        if len(data) >= period:
            ax.plot(x, ema(close, period), color=color, linewidth=width, label=f"EMA {period}", alpha=.95)

    ax.axhspan(plan.long.entry_low, plan.long.entry_high, color=bullish, alpha=.11)
    ax.axhspan(plan.short.entry_low, plan.short.entry_high, color=bearish, alpha=.10)
    ax.text(1, (plan.long.entry_low + plan.long.entry_high) / 2,
            f"  LONG {plan.long.entry_low:.2f}–{plan.long.entry_high:.2f}", color=bullish, fontsize=7, va="center")
    ax.text(1, (plan.short.entry_low + plan.short.entry_high) / 2,
            f"  SHORT {plan.short.entry_low:.2f}–{plan.short.entry_high:.2f}", color=bearish, fontsize=7, va="center")

    buy_price, sell_price = float(plan.long.trigger), float(plan.short.trigger)
    marker_x = max(len(data) - 22, 1)
    ax.annotate(f"AL  {buy_price:.2f} TL", xy=(marker_x, buy_price), xytext=(-8, -34),
                textcoords="offset points", color="#03120c", fontsize=9, fontweight="bold",
                bbox=dict(boxstyle="round,pad=.42", facecolor=bullish, edgecolor="#7dffd1", linewidth=1.2),
                arrowprops=dict(arrowstyle="-|>", color=bullish, linewidth=1.7), zorder=15)
    ax.annotate(f"SAT  {sell_price:.2f} TL", xy=(marker_x + 5, sell_price), xytext=(8, 34),
                textcoords="offset points", color="#fff4f6", fontsize=9, fontweight="bold",
                bbox=dict(boxstyle="round,pad=.42", facecolor=bearish, edgecolor="#ff8aa0", linewidth=1.2),
                arrowprops=dict(arrowstyle="-|>", color=bearish, linewidth=1.7), zorder=15)

    from app.analysis.smart_money_engine import detect_smart_money
    smart = detect_smart_money(data)
    for zone in smart.fvg:
        color = "#00b8ff" if zone.direction == "bullish" else "#ff8a3d"
        ax.axhspan(zone.low, zone.high, xmin=max(zone.index / len(data), 0), xmax=1, color=color, alpha=.045)
        ax.text(zone.index, (zone.low + zone.high) / 2, "FVG", color=color, fontsize=5.5, alpha=.8)
    for zone in smart.order_blocks:
        color = bullish if zone.direction == "bullish" else bearish
        ax.axhspan(zone.low, zone.high, xmin=max(zone.index / len(data), 0), xmax=1, color=color, alpha=.06)
        ax.text(zone.index, (zone.low + zone.high) / 2, "OB", color=color, fontsize=5.5, alpha=.85)
    for event in smart.structure:
        color = bullish if event.direction == "bullish" else bearish
        ax.scatter(event.index, event.price, marker="^" if event.direction == "bullish" else "v", s=18, color=color, zorder=9)
        ax.text(event.index, event.price, f" {event.kind}", color=color, fontsize=5.5, va="bottom")

    preferred = plan.long if plan.long.score >= plan.short.score else plan.short
    direction_color = bullish if preferred.direction == "LONG" else bearish
    ax.axhline(plan.current_price, color="#f8fafc", linewidth=1.0, alpha=.9)
    ax.annotate(f"SON {plan.current_price:.2f}", xy=(len(data) - 1, plan.current_price),
                xytext=(8, 0), textcoords="offset points", color="#f8fafc", fontsize=7, va="center")
    ax.axhline(preferred.stop_standard, color=bearish, linewidth=1.0, linestyle="--", alpha=.9)
    ax.text(len(data) - 1, preferred.stop_standard, f"  SL {preferred.stop_standard:.2f}", color=bearish, fontsize=7, va="center")
    for index, target in enumerate(preferred.targets, 1):
        ax.axhline(target, color=direction_color, linewidth=.75, linestyle=":" if index < 3 else "--", alpha=.72)
        ax.text(len(data) - 1, target, f"  TP{index} {target:.2f}", color=direction_color, fontsize=6.5, va="center")
    for level in plan.support_levels:
        ax.axhline(level, color=bullish, linewidth=.5, linestyle=":", alpha=.38)
    for level in plan.resistance_levels:
        ax.axhline(level, color=bearish, linewidth=.5, linestyle=":", alpha=.38)

    ax.set_title(f"{plan.symbol}  •  {preferred.direction} ÖNCELİKLİ  •  {preferred.score}/100",
                 color=foreground, fontsize=14, fontweight="bold", loc="left", pad=14)
    ax.text(.01, .94, f"RSI {plan.rsi:.1f}   ATR %{plan.atr_percent:.2f}   TREND {plan.trend}",
            transform=ax.transAxes, color="#91a4b7", fontsize=8)
    ax.legend(loc="upper right", fontsize=7, ncol=3, facecolor=panel, edgecolor=grid, labelcolor=foreground)
    _format_trading_axis(ax, data, right_margin=14)
    fig.text(.012, .012, "MONTANA MELİH HİSSE BOT • Teknik senaryo • Yatırım tavsiyesi değildir",
             color="#60758a", fontsize=7)
    fig.subplots_adjust(left=.035, right=.93, top=.91, bottom=.09)
    out_path = os.path.join(tempfile.gettempdir(), f"bist_tv_{plan.symbol}_{uuid.uuid4().hex[:8]}.png")
    fig.savefig(out_path, dpi=max(settings.chart_dpi, 150), facecolor=fig.get_facecolor())
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
    """15 dakikalık profesyonel grafik; gerçek mum, seans seviyeleri ve önbellek."""
    df = df_intraday.sort_values("timestamp").reset_index(drop=True)
    if df.empty:
        raise ValueError("Grafik için OHLCV verisi boş.")
    settings, theme = _chart_settings()
    context = {
        "support": daily_support,
        "resistance": daily_resistance,
        "previous_close": previous_close,
        "alarms": active_alarm_points,
        "info": info_box,
        "theme": theme.name,
    }
    key = _chart_cache_key(df, symbol, "15m", "professional_intraday", context)
    cache = _get_chart_cache()
    cached = cache.get(key)
    if cached:
        return cached

    close = df["close"].astype(float)
    fig = plt.figure(figsize=(settings.chart_width, max(settings.chart_height * 0.73, 7.0)))
    gs = fig.add_gridspec(3, 1, height_ratios=[4.2, 1.15, 1.0], hspace=0.06)
    ax_price = fig.add_subplot(gs[0])
    ax_vol = fig.add_subplot(gs[1], sharex=ax_price)
    ax_rsi = fig.add_subplot(gs[2], sharex=ax_price)
    _style_axes(fig, (ax_price, ax_vol, ax_rsi), theme)
    x = _draw_candles(ax_price, df, theme)
    label_items: list[tuple[float, str, str, int]] = []

    # Her seans için sıfırlanan VWAP.
    session = pd.to_datetime(df["timestamp"], utc=True).dt.date
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    weighted = typical_price * df["volume"]
    cumulative_volume = df["volume"].groupby(session).cumsum().replace(0, pd.NA)
    vwap = weighted.groupby(session).cumsum() / cumulative_volume
    ax_price.plot(x, vwap.astype(float), color="#ec4899", linewidth=1.05, label="VWAP")

    if len(df) >= 20:
        ax_price.plot(x, ema(close, 20), color="#f59e0b", linewidth=0.85, label="EMA20")
    if len(df) >= 50:
        ax_price.plot(x, ema(close, 50), color="#3b82f6", linewidth=0.85, label="EMA50")

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
        ax_price.axhline(level, color=theme.muted, linestyle="--", linewidth=0.6, alpha=0.65)
        label_items.append((level, f"{label} {level:.2f}", theme.muted, priority))

    if previous_close is not None:
        ax_price.axhline(previous_close, color="#8b5cf6", linestyle=":", linewidth=0.8, alpha=0.8)
        label_items.append((previous_close, f"Önceki kapanış {previous_close:.2f}", "#8b5cf6", 82))
        gap_low, gap_high = sorted((float(previous_close), day_open))
        if gap_high > gap_low:
            ax_price.axhspan(gap_low, gap_high, color="#8b5cf6", alpha=0.07)

    if daily_support is not None:
        ax_price.axhline(daily_support, color=theme.support, linestyle="--", linewidth=0.85, alpha=0.8)
        label_items.append((daily_support, f"Günlük destek {daily_support:.2f}", theme.support, 90))
    if daily_resistance is not None:
        ax_price.axhline(daily_resistance, color=theme.resistance, linestyle="--", linewidth=0.85, alpha=0.8)
        label_items.append((daily_resistance, f"Günlük direnç {daily_resistance:.2f}", theme.resistance, 90))

    _draw_anomaly_markers(ax_price, df, anomalies)
    timestamps = pd.to_datetime(df["timestamp"], utc=True)
    for point in active_alarm_points or []:
        timestamp, price = point[:2]
        target = pd.Timestamp(timestamp)
        target = target.tz_localize("UTC") if target.tzinfo is None else target.tz_convert("UTC")
        xpos = int(np.argmin(np.abs((timestamps - target).dt.total_seconds().to_numpy())))
        ax_price.scatter(xpos, price, marker="*", s=70, color="#f59e0b", edgecolors=theme.foreground, linewidths=0.4, zorder=8)

    if info_box:
        lines = [f"{name}: {value}" for name, value in info_box.items() if value is not None]
        if lines:
            ax_price.text(
                0.012, 0.985, "\n".join(lines[:12]), transform=ax_price.transAxes,
                fontsize=7.1, va="top", family="monospace", color=theme.foreground,
                bbox={"boxstyle": "round,pad=0.4", "facecolor": theme.panel, "alpha": 0.9, "edgecolor": theme.grid},
                zorder=10,
            )

    y_min, y_max = ax_price.get_ylim()
    for actual, display, label, color in _resolve_label_positions(label_items, y_min, y_max, max_labels=9):
        ax_price.annotate(
            label, xy=(x[-1], actual), xytext=(x[-1] + 1.0, display),
            fontsize=6.2, color=color, va="center",
            arrowprops={"arrowstyle": "-", "color": color, "lw": 0.45, "alpha": 0.7},
            bbox={"boxstyle": "round,pad=0.14", "facecolor": theme.panel, "edgecolor": color, "alpha": 0.85},
        )

    ax_price.set_title(f"MERGEN QUANT — {symbol.upper()} Gün İçi Teknik Analiz (15dk)", fontsize=12, fontweight="bold", loc="left")
    ax_price.set_ylabel("Fiyat", fontsize=8)
    ax_price.legend(loc="upper right", fontsize=6.5, ncol=3, framealpha=0.35)

    up = df["close"] >= df["open"]
    ax_vol.bar(x, df["volume"], color=np.where(up, theme.up, theme.down), width=0.68, alpha=0.78)
    volume_avg = df["volume"].rolling(20, min_periods=3).mean()
    ax_vol.plot(x, volume_avg, color="#f59e0b", linewidth=0.8)
    if len(df) >= 20 and volume_avg.iloc[-1] > 0:
        rvol = float(df["volume"].iloc[-1] / volume_avg.iloc[-1])
        ax_vol.text(0.99, 0.88, f"RVOL {rvol:.2f}x", transform=ax_vol.transAxes, ha="right", va="top", fontsize=7, color=theme.foreground)
    ax_vol.set_ylabel("Hacim", fontsize=8)

    if len(df) >= 15:
        rsi_series = rsi(close, 14)
        ax_rsi.plot(x, rsi_series, color="#8b5cf6", linewidth=0.9)
        ax_rsi.axhspan(70, 100, color=theme.down, alpha=0.07)
        ax_rsi.axhspan(0, 30, color=theme.up, alpha=0.07)
        ax_rsi.axhline(70, color=theme.down, linestyle="--", linewidth=0.55)
        ax_rsi.axhline(30, color=theme.up, linestyle="--", linewidth=0.55)
        ax_rsi.set_ylim(0, 100)
    ax_rsi.set_ylabel("RSI", fontsize=8)

    plt.setp(ax_price.get_xticklabels(), visible=False)
    plt.setp(ax_vol.get_xticklabels(), visible=False)
    _format_trading_axis(ax_rsi, df, label_format="%d.%m %H:%M")
    fig.text(0.5, 0.012, "Teknik senaryo çalışmasıdır, yatırım tavsiyesi değildir.", ha="center", color=theme.muted, fontsize=7)
    fig.subplots_adjust(left=0.07, right=0.95, top=0.94, bottom=0.09)

    out_path = os.path.join(tempfile.gettempdir(), f"mergen_intraday_chart_{symbol}_{uuid.uuid4().hex[:8]}.png")
    fig.savefig(out_path, dpi=settings.chart_dpi, facecolor=fig.get_facecolor())
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

    settings, theme = _chart_settings()
    context = {
        "timeframe": timeframe, "current_price": current_price, "user_target": user_target,
        "roadmap": [getattr(step, "mid", None) for step in getattr(roadmap, "steps", [])],
        "valuation": valuation_status, "speculation": speculation_risk,
        "theme": theme.name,
    }
    key = _chart_cache_key(data, symbol, timeframe, "long_term_log", context)
    cache = _get_chart_cache()
    cached = cache.get(key)
    if cached:
        return cached

    fig = plt.figure(figsize=(settings.chart_width, settings.chart_height * 0.78), facecolor=theme.background)
    grid = fig.add_gridspec(2, 1, height_ratios=[5, 1], hspace=0.05)
    ax = fig.add_subplot(grid[0])
    ax_vol = fig.add_subplot(grid[1], sharex=ax)
    _style_axes(fig, [ax, ax_vol], theme)
    x = _draw_candles(ax, data, theme, width=0.62)
    ax.set_yscale("log")

    close = data["close"].astype(float)
    normalized = timeframe.strip().lower()
    ema_periods = (10, 20, 40) if normalized in {"monthly", "aylik", "aylık", "1mo", "1m"} else (20, 50, 100)
    colors = (theme.accent, "#f59e0b", "#8b5cf6")
    for period, color in zip(ema_periods, colors):
        if len(data) >= period:
            ax.plot(x, ema(close, period), color=color, linewidth=0.9, label=f"EMA{period}")

    # Logaritmik regresyon kanalı.
    log_close = np.log(close.to_numpy())
    slope, intercept = np.polyfit(x, log_close, 1)
    trend = intercept + slope * x
    residual = float(np.std(log_close - trend))
    ax.plot(x, np.exp(trend), color=theme.foreground, linewidth=1.0, label="Log trend")
    ax.plot(x, np.exp(trend + 2 * residual), color=theme.muted, linestyle="--", linewidth=0.7)
    ax.plot(x, np.exp(trend - 2 * residual), color=theme.muted, linestyle="--", linewidth=0.7)
    ax.fill_between(x, np.exp(trend - 2 * residual), np.exp(trend + 2 * residual), color=theme.accent, alpha=0.035)

    historical_low = float(data["low"].min())
    historical_high = float(data["high"].max())
    price_range = historical_high - historical_low
    for ratio, color in ((0.382, theme.support), (0.5, theme.muted), (0.618, theme.resistance)):
        level = historical_high - price_range * ratio
        ax.axhline(level, color=color, linestyle=":", linewidth=0.65, alpha=0.65)
        ax.text(len(data) - 1, level, f" Fib {ratio:.3f}", fontsize=6, color=color, va="bottom")
    for ratio in (1.272, 1.618):
        level = historical_low + price_range * ratio
        ax.axhline(level, color=theme.accent, linestyle="--", linewidth=0.55, alpha=0.5)

    # Çok yıllık destek/direnç ve uygun hacim bölgeleri.
    for quantile, label, color in ((0.10, "Ana destek", theme.support), (0.90, "Ana direnç", theme.resistance)):
        level = float(close.quantile(quantile))
        ax.axhspan(level * 0.98, level * 1.02, color=color, alpha=0.06)
        ax.text(0, level, label, fontsize=6, color=color, va="center")
    typical = (data["high"] + data["low"] + data["close"]) / 3
    if float(data["volume"].sum()) > 0:
        bins = np.geomspace(historical_low, historical_high, min(25, max(8, len(data) // 4)))
        bucket = pd.cut(typical, bins=bins, include_lowest=True)
        profile = data.groupby(bucket, observed=True)["volume"].sum().sort_values(ascending=False).head(3)
        for interval in profile.index:
            ax.axhspan(float(interval.left), float(interval.right), color=theme.muted, alpha=0.045)

    if current_price is not None and current_price > 0:
        ax.axhline(current_price, color=theme.foreground, linewidth=1.15, label="Güncel fiyat")
    if user_target is not None and user_target > 0:
        ax.axhline(user_target, color="#e11d48", linestyle="-.", linewidth=1.15, label="Kullanıcı hedefi")
    for step in getattr(roadmap, "steps", []) or []:
        if getattr(step, "mid", None):
            ax.axhline(step.mid, color=theme.accent, linestyle=":", linewidth=0.75, alpha=0.75)
            ax.text(len(data) - 1, step.mid, f" Yol {step.sequence}", fontsize=6, color=theme.accent, va="center")

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
            ax.axvline(position, color=theme.muted, linestyle=":", linewidth=0.6)
            ax.text(position, historical_low * 1.02, getattr(event, "corporate_action_type", "Sermaye"), rotation=90, fontsize=5, color=theme.muted)

    details = {
        "Güncel fiyat": f"{current_price:.2f} TL" if current_price is not None else None,
        "Kullanıcı hedefi": f"{user_target:.2f} TL" if user_target is not None else None,
        "Gereken yüzde": f"{((user_target / current_price) - 1) * 100:+.2f}%" if user_target and current_price else None,
        "Gereken kat": f"{user_target / current_price:.2f}x" if user_target and current_price else None,
        "Uzun vadeli trend": "Yükseliş" if slope > 0 else "Düşüş",
        "Ana destek": f"{close.quantile(0.10):.2f} TL",
        "Ana direnç": f"{close.quantile(0.90):.2f} TL",
        "Temel değerleme": valuation_status or "Veri yetersiz",
        "Spekülasyon riski": speculation_risk or "Veri yetersiz",
        **(info_box or {}),
    }
    info_lines = [f"{key}: {value}" for key, value in details.items() if value is not None]
    ax.text(
        0.01, 0.98, "\n".join(info_lines), transform=ax.transAxes, va="top", ha="left",
        fontsize=6.5, color=theme.foreground,
        bbox={"boxstyle": "round", "facecolor": theme.panel, "alpha": 0.88, "edgecolor": theme.grid},
    )

    up = data["close"] >= data["open"]
    ax_vol.bar(x, data["volume"], color=[theme.up if value else theme.down for value in up], width=0.65, alpha=0.75)
    ax_vol.set_ylabel("Hacim", fontsize=7)
    ax.set_ylabel("Fiyat (log)", fontsize=7)
    label = "Aylık" if normalized in {"monthly", "aylik", "aylık", "1mo", "1m"} else "Haftalık"
    ax.set_title(f"{symbol.upper()} — {label} Logaritmik Uzun Vadeli Grafik", color=theme.foreground)
    ax.legend(loc="upper right", fontsize=6, ncol=3)
    _format_trading_axis(ax_vol, data, right_margin=3, label_format="%m.%Y")
    plt.setp(ax.get_xticklabels(), visible=False)
    fig.text(0.5, 0.012, "Teknik senaryo çalışmasıdır; kesin tahmin veya yatırım tavsiyesi değildir.", ha="center", fontsize=7, color=theme.muted)
    fig.subplots_adjust(left=0.07, right=0.96, top=0.94, bottom=0.09)
    out_path = os.path.join(tempfile.gettempdir(), f"mergen_long_{symbol}_{uuid.uuid4().hex[:8]}.png")
    fig.savefig(out_path, dpi=settings.chart_dpi, facecolor=fig.get_facecolor())
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
