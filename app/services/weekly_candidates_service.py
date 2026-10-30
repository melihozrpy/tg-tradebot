from __future__ import annotations

"""Weekly, retest-first BIST candidate workflow.

This module is intentionally a thin orchestration layer over the strict
daily-data screener.  It does not manufacture a second candidate, financial
data, sector label, or price level when the source data cannot support one.
"""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.patches import Rectangle

from app.analysis.indicator_engine import ema
from app.analysis.screener_engine import DailyTopPicksRunResult, run_daily_top_picks_scan
from app.config.instruments import universe_symbols
from app.data.provider_factory import build_market_data_provider
from app.fundamentals.factory import build_fundamental_provider
from app.services.scheduled_idea_service import IdeaPerformance
from app.services.vivid_chart_style import MONO_FONT, SANS_FONT, VIVID, save_terminal_png, style_axes


DISCLAIMER = "⚠️ Eğitim amaçlı genel analizdir, yatırım tavsiyesi değildir."


def scan_weekly_candidates(settings) -> DailyTopPicksRunResult:
    """Find zero, one, or two verified weekly candidates.

    The underlying scan already rejects extended, thin and structurally weak
    names.  Its limit is pinned to two here so stale environment variables
    cannot turn the weekly review into a watch-list dump.
    """

    scoped_settings = settings.model_copy(
        update={
            "daily_top_picks_max_results": 2,
            "daily_top_picks_minimum_confirmations": max(
                5, min(8, int(getattr(settings, "weekly_candidates_minimum_confirmations", 7)))
            ),
            "daily_top_picks_minimum_ten_confirmations": max(
                5, min(10, int(getattr(settings, "weekly_candidates_minimum_ten_confirmations", 7)))
            ),
            "daily_top_picks_require_fundamental": bool(
                getattr(settings, "weekly_candidates_require_fundamental", True)
            ),
        }
    )
    result = run_daily_top_picks_scan(
        symbols=universe_symbols(settings.bist_universe_json_path),
        provider_factory=lambda: build_market_data_provider(settings),
        fundamental_provider_factory=lambda: build_fundamental_provider(settings),
        settings=scoped_settings,
    )
    threshold = int(getattr(settings, "weekly_candidates_minimum_score", 75))
    return replace(result, picks=tuple(pick for pick in result.picks if pick.score >= threshold)[:2])


def _price(value: float) -> str:
    return f"{float(value):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _week_range(created_at: datetime, timezone_name: str) -> str:
    local = created_at.astimezone(ZoneInfo(timezone_name))
    start = local.date()
    end = start.fromordinal(start.toordinal() + 4)
    return f"{start:%d.%m}–{end:%d.%m.%Y}"


def format_weekly_candidates_report(
    report: DailyTopPicksRunResult,
    *,
    timezone_name: str = "Europe/Istanbul",
) -> str:
    """Format the canonical Monday candidate message from verified scan data."""

    local = report.created_at.astimezone(ZoneInfo(timezone_name))
    picks = tuple(pick for pick in report.picks if pick.score >= 75)[:2]
    if not picks:
        return "\n".join(
            [
                "<b>📌 HAFTANIN ADAYLARI</b>",
                f"<i>Veri zamanı: {local:%d.%m.%Y %H:%M} TSİ</i>",
                "Bu hafta eşiği geçen aday yok. Gerekçe: likidite, retest, maliyet sonrası R/R veya doğrulanmış temel kalite koşullarının tamamı birlikte oluşmadı.",
                DISCLAIMER,
            ]
        )

    lines = [
        f"<b>📌 HAFTANIN ADAYLARI — {_week_range(report.created_at, timezone_name)}</b>",
        f"<i>Veri zamanı: {local:%d.%m.%Y %H:%M} TSİ · {report.scanned} hisse tarandı</i>",
        "Piyasa: adaylar yalnız yüksek likidite, retest alanı, yapı ve temel doğrulama filtrelerinden sonra listelenir.",
    ]
    for rank, pick in enumerate(picks, start=1):
        reasons = " · ".join(pick.reasons[:3]) or "Doğrulanmış teknik gerekçe eksik"
        risks = "Bilanço/KAP, temettü, genel kurul, PPK ve TÜFE takvimi işlem öncesi yeniden kontrol edilmeli."
        lines.extend(
            [
                "",
                f"<b>{rank}) {pick.symbol}</b> · Uyum <code>{pick.score}/100</code>",
                f"Alım bölgesi: <code>{_price(pick.entry_low)}–{_price(pick.entry_high)} TL</code> · Teyit: bölgeye dönüşte günlük/15dk kapanış ve hacim.",
                f"Stop: <code>{_price(pick.stop)} TL</code> · H1 <code>{_price(pick.tp1)} TL</code> · H2 <code>{_price(pick.tp2)} TL</code> · R/R <code>1:{pick.rr:.1f}</code>",
                f"Neden: {reasons}.",
                f"Geçerlilik: hafta sonuna kadar; tetik oluşmazsa plan iptal. Riskler: {risks}",
            ]
        )
    lines.extend(
        [
            "",
            "Çıkış planı: H1'de kısmi değerlendirme, H2'de kalan; stop günlük kapanışla kırılırsa plan geçersizdir.",
            DISCLAIMER,
        ]
    )
    return "\n".join(lines)[:3500]


def weekly_delivery_key(report: DailyTopPicksRunResult, timezone_name: str) -> str:
    local = report.created_at.astimezone(ZoneInfo(timezone_name))
    iso = local.isocalendar()
    return f"weekly-candidates:{iso.year}-W{iso.week:02d}"


def _candles(axis, frame: pd.DataFrame) -> None:
    data = frame.tail(40).reset_index(drop=True)
    for index, row in data.iterrows():
        open_, high, low, close = (float(row[key]) for key in ("open", "high", "low", "close"))
        color = VIVID.bull if close >= open_ else VIVID.bear
        axis.vlines(index, low, high, color=color, linewidth=0.85, zorder=3)
        axis.add_patch(
            Rectangle(
                (index - 0.32, min(open_, close)),
                0.64,
                max(abs(close - open_), max(close * 0.00015, 1e-8)),
                facecolor=color,
                edgecolor=color,
                linewidth=0.45,
                zorder=4,
            )
        )
    close = data["close"].astype(float)
    if len(close) >= 20:
        axis.plot(range(len(data)), ema(close, 20), color=VIVID.amber, linewidth=1.0, label="EMA20")
    if len(close) >= 35:
        axis.plot(range(len(data)), ema(close, min(50, len(close) - 1)), color=VIVID.blue, linewidth=1.0, label="EMA50")
    axis.set_xlim(-1, len(data) + 1)
    axis.set_xticks([0, max(0, len(data) // 2), len(data) - 1])
    axis.set_xticklabels([pd.Timestamp(data.iloc[index]["timestamp"]).strftime("%d %b") for index in [0, max(0, len(data) // 2), len(data) - 1]], fontsize=7)
    axis.yaxis.tick_right()
    axis.set_ylabel("TL", color=VIVID.muted, fontsize=7, fontfamily=MONO_FONT)


def render_weekly_candidates_card(report: DailyTopPicksRunResult, settings) -> str | None:
    """Render the exact two-candidate 1600×900 Monday visual, when possible."""

    picks = tuple(pick for pick in report.picks if pick.score >= 75)[:2]
    if not picks:
        return None
    provider = build_market_data_provider(settings)
    end = datetime.now(timezone.utc)
    frames: list[tuple[object, pd.DataFrame]] = []
    for pick in picks:
        frame = provider.get_ohlcv(pick.symbol, "1d", end - pd.Timedelta(days=130), end)
        if frame is None or frame.empty:
            continue
        data = frame.copy().reset_index().rename(columns={frame.index.name or "index": "timestamp"})
        if "timestamp" not in data.columns:
            data["timestamp"] = frame.index
        if not {"open", "high", "low", "close"}.issubset(data.columns):
            continue
        frames.append((pick, data))
    if not frames:
        return None

    fig = plt.figure(figsize=(16, 9), facecolor=VIVID.background)
    local = report.created_at.astimezone(ZoneInfo(settings.timezone_name))
    fig.text(0.05, 0.94, f"HAFTANIN ADAYLARI  •  {_week_range(report.created_at, settings.timezone_name)}", color=VIVID.text, fontsize=19, fontweight="bold", fontfamily=SANS_FONT)
    fig.text(0.05, 0.905, f"XU100 / BIST100  •  veri zamanı {local:%d.%m.%Y %H:%M} TSİ  •  EN FAZLA 2 ADAY", color=VIVID.muted, fontsize=8.5, fontfamily=MONO_FONT)
    positions = ([0.055, 0.22, 0.41, 0.60], [0.535, 0.22, 0.41, 0.60]) if len(frames) > 1 else ([0.295, 0.22, 0.41, 0.60],)
    for rank, ((pick, data), position) in enumerate(zip(frames, positions), start=1):
        axis = fig.add_axes(position, facecolor=VIVID.panel)
        style_axes(axis)
        _candles(axis, data)
        zone_color = VIVID.amber
        axis.axhspan(pick.entry_low, pick.entry_high, color=zone_color, alpha=0.10, zorder=1)
        axis.axhline(pick.stop, color=VIVID.bear, linestyle=(0, (5, 4)), linewidth=0.9)
        axis.axhline(pick.tp1, color=VIVID.bull, linestyle=(0, (4, 4)), linewidth=0.9)
        axis.axhline(pick.tp2, color=VIVID.bull, linestyle=(0, (2, 4)), linewidth=0.75, alpha=0.75)
        axis.set_title(f"{rank}) {pick.symbol}  •  UYUM {pick.score}/100", color=VIVID.text, fontsize=11, fontweight="bold", loc="left", pad=10)
        axis.text(0.02, 0.03, f"ALIM {_price(pick.entry_low)}–{_price(pick.entry_high)}\nSTOP {_price(pick.stop)} · H1 {_price(pick.tp1)} · H2 {_price(pick.tp2)}\nR/R 1:{pick.rr:.1f}", transform=axis.transAxes, color=VIVID.text, fontsize=7.4, fontfamily=MONO_FONT, va="bottom", bbox={"boxstyle": "round,pad=0.35", "facecolor": VIVID.panel_alt, "edgecolor": VIVID.border, "alpha": 0.95})
    fig.text(0.05, 0.12, "Tetik: alım bölgesine dönüşte kapanış + hacim teyidi. Güncel fiyat otomatik giriş değildir.", color=VIVID.muted, fontsize=8.4, fontfamily=SANS_FONT)
    fig.text(0.05, 0.075, "MONTANA FİNANS ROBOTU · SMXM Analiz Sistemi", color=VIVID.faint, fontsize=7.6, fontfamily=MONO_FONT)
    path = Path(settings.report_chart_output_dir) / f"weekly_candidates_{report.created_at:%Y%m%d}_{report.created_at:%H%M}.png"
    try:
        return save_terminal_png(fig, path)
    finally:
        plt.close(fig)


def format_weekly_results_report(
    items: tuple[IdeaPerformance, ...] | list[IdeaPerformance],
    *,
    timezone_name: str = "Europe/Istanbul",
) -> str:
    """Friday's factual outcome summary; untouched zones are not trades."""

    local = datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name))
    rows = list(items)
    lines = [
        "<b>📊 HAFTALIK SONUÇ RAPORU</b>",
        f"<i>{local:%d.%m.%Y %H:%M} TSİ</i>",
    ]
    if not rows:
        lines.extend(["Bu hafta değerlendirme yaşı gelen tetiklenmiş plan yok.", DISCLAIMER])
        return "\n".join(lines)
    measurable = [row for row in rows if row.return_percent is not None]
    for row in rows[:2]:
        result = "—" if row.return_percent is None else f"%{row.return_percent:+.1f}"
        lines.append(f"<b>{row.symbol}</b> · {row.status} · sonuç {result} · {row.note}")
    if measurable:
        wins = sum(1 for row in measurable if float(row.return_percent or 0) > 0)
        average = sum(float(row.return_percent or 0) for row in measurable) / len(measurable)
        lines.append(f"Ölçülebilen plan: {len(measurable)} · pozitif {wins}/{len(measurable)} · ortalama %{average:+.1f}")
    else:
        lines.append("Planların retest bölgesine temas etmediği veya intrabar sırası doğrulanamadığı için başarı oranı üretilmedi.")
    lines.append(DISCLAIMER)
    return "\n".join(lines)[:3500]
