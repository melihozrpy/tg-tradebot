from __future__ import annotations

"""Auditable scheduled BIST idea cards and two-day outcome follow-up.

The service intentionally stores *plans*, not imaginary fills.  A later
report only measures a plan if its retest zone was touched in completed daily
bars; an untouched zone is reported as ``TETIKLENMEDI`` instead of being
counted as a gain or a loss.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy.orm import Session

from app.analysis.screener_engine import DailyTopPick, DailyTopPicksRunResult
from app.data.base_provider import BaseMarketDataProvider
from app.models.database import ScheduledTradeIdea


@dataclass(frozen=True)
class IdeaPerformance:
    symbol: str
    issued_at: datetime
    entry_low: float
    entry_high: float
    stop_price: float
    tp1_price: float
    status: str
    evaluation_price: float | None
    return_percent: float | None
    note: str


def select_scheduled_ideas(report: DailyTopPicksRunResult, maximum: int = 2) -> tuple[DailyTopPick, ...]:
    """Return at most two already-qualified daily plans without fabricating a backup."""

    maximum = max(1, min(2, int(maximum)))
    return tuple(report.picks[:maximum])


def persist_scheduled_ideas(
    db: Session,
    *,
    report: DailyTopPicksRunResult,
    slot: str,
    maximum: int = 2,
) -> tuple[ScheduledTradeIdea, ...]:
    """Persist the original plan once per slot/day for later verification."""

    local = report.created_at.astimezone(ZoneInfo("Europe/Istanbul"))
    run_key = f"scheduled-ideas:{slot}:{local:%Y%m%d}"
    rows: list[ScheduledTradeIdea] = []
    for pick in select_scheduled_ideas(report, maximum):
        row = (
            db.query(ScheduledTradeIdea)
            .filter(ScheduledTradeIdea.run_key == run_key, ScheduledTradeIdea.symbol == pick.symbol)
            .first()
        )
        if row is None:
            row = ScheduledTradeIdea(
                run_key=run_key,
                slot=slot,
                symbol=pick.symbol,
                score=pick.score,
                observed_price=pick.price,
                entry_low=pick.entry_low,
                entry_high=pick.entry_high,
                stop_price=pick.stop,
                tp1_price=pick.tp1,
                tp2_price=pick.tp2,
                planned_rr=pick.rr,
                pattern_name=pick.pattern.name,
                technical_reasons_json=json.dumps(pick.reasons, ensure_ascii=False),
                fundamental_status=pick.fundamental_status,
                fundamental_score=pick.fundamental_score,
                issued_at=report.created_at,
            )
            db.add(row)
            rows.append(row)
        else:
            rows.append(row)
    db.commit()
    return tuple(rows)


def format_scheduled_ideas_report(
    report: DailyTopPicksRunResult,
    *,
    slot: str,
    timezone_name: str = "Europe/Istanbul",
    maximum: int = 2,
) -> str:
    """Render the requested compact card with explicit, conditional language."""

    local = report.created_at.astimezone(ZoneInfo(timezone_name))
    slot_title = "AÇILIŞ ÖNCESİ" if slot == "morning" else "KAPANIŞ ÖNCESİ"
    picks = select_scheduled_ideas(report, maximum)
    lines = [
        f"┏━━ 📍 {slot_title} • 2 HİSSE PLANI ━━┓",
        f"🕒 {local:%d.%m.%Y %H:%M} TSİ  •  {report.scanned} hisse tarandı",
        "🛡 Filtre: doğrulanmış formasyon + çoklu teknik teyit + retest bölgesi + RR ≥ 1:2.",
        "⛔ Son fiyattan giriş yok; plan yalnız bölgeye dönüş ve kapanış/hacim teyidiyle aktiftir.",
    ]
    if not picks:
        lines.extend(
            [
                "",
                "🟡 Bu standartları birlikte geçen aday çıkmadı.",
                "Zorla iki isim yazılmadı; yeni kapanışlar oluştuğunda tarama yeniden yapılır.",
            ]
        )
        return "\n".join(lines)

    for rank, pick in enumerate(picks, start=1):
        reasons = " • ".join(pick.reasons[:5]) or "Doğrulanmış teknik gerekçe yetersiz"
        fundamental = (
            f"{pick.fundamental_status} {pick.fundamental_score}/100"
            if pick.fundamental_score is not None
            else "doğrulanmadı"
        )
        lines.extend(
            [
                "",
                f"{rank}) 📊 HİSSE: {pick.symbol}  •  Güven: A+ aday ({pick.score}/100)",
                f"📍 Giriş bölgesi: {pick.entry_low:.2f}–{pick.entry_high:.2f} TL",
                f"🎯 Hedefler: TP1 {pick.tp1:.2f}  |  TP2 {pick.tp2:.2f} TL",
                f"🛑 Geçersizlik/stop: {pick.stop:.2f} TL  •  ⚖️ RR 1:{pick.rr:.1f}",
                "⏱️ Zaman: Günlük yön + 1 saat yapı; girişte 15dk kapanış/hacim teyidi.",
                f"📐 Yapı: {pick.pattern.name} — {pick.pattern.detail}",
                f"✅ Uyum: {reasons}",
                f"🏢 Temel kontrol: {fundamental}",
                f"⏳ Tetik: {pick.confirmation_instruction}",
            ]
        )
    lines.extend(
        [
            "",
            "ℹ️ Hedef, mevcut direnç/formasyon yapısından hesaplanır; getiri ya da yön garantisi değildir.",
            "⚠️ Emirden önce güncel fiyat, KAP ve derinlik kontrol edilmelidir.",
        ]
    )
    return "\n".join(lines)[:4096]


def _completed_daily_bars(provider: BaseMarketDataProvider, row: ScheduledTradeIdea) -> pd.DataFrame:
    start = row.issued_at - timedelta(days=2)
    end = datetime.now(timezone.utc)
    frame = provider.get_ohlcv(row.symbol, "1d", start, end)
    if frame is None or frame.empty:
        return pd.DataFrame()
    result = frame.copy()
    result.index = pd.to_datetime(result.index, utc=True)
    issued = row.issued_at if row.issued_at.tzinfo else row.issued_at.replace(tzinfo=timezone.utc)
    return result.loc[result.index >= issued]


def _evaluate_row(row: ScheduledTradeIdea, frame: pd.DataFrame) -> IdeaPerformance:
    issued = row.issued_at if row.issued_at.tzinfo else row.issued_at.replace(tzinfo=timezone.utc)
    if frame.empty:
        return IdeaPerformance(
            row.symbol, issued, row.entry_low, row.entry_high, row.stop_price, row.tp1_price,
            "VERI_YOK", None, None, "Kapanmış günlük bar alınamadı; sonuç yazılmadı.",
        )
    touched = frame[(frame["low"] <= row.entry_high) & (frame["high"] >= row.entry_low)]
    latest_close = float(frame.iloc[-1]["close"])
    if touched.empty:
        return IdeaPerformance(
            row.symbol, issued, row.entry_low, row.entry_high, row.stop_price, row.tp1_price,
            "TETIKLENMEDI", latest_close, None,
            "Planlanan retest bölgesine temas yok; işlem varsayılmadı.",
        )
    after_entry = frame.loc[touched.index[0]:]
    first = after_entry.iloc[0]
    if float(first["low"]) <= row.stop_price and float(first["high"]) >= row.tp1_price:
        return IdeaPerformance(
            row.symbol, issued, row.entry_low, row.entry_high, row.stop_price, row.tp1_price,
            "BELIRSIZ", latest_close, None,
            "Aynı günlük barda stop ve hedef aralığı görüldü; intrabar sırası bilinmediği için getiri yazılmadı.",
        )
    entry = (row.entry_low + row.entry_high) / 2.0
    if (after_entry["low"] <= row.stop_price).any():
        return IdeaPerformance(
            row.symbol, issued, row.entry_low, row.entry_high, row.stop_price, row.tp1_price,
            "STOP", row.stop_price, (row.stop_price / entry - 1) * 100,
            "Retest sonrası stop seviyesi tamamlanmış günlük barda görüldü.",
        )
    if (after_entry["high"] >= row.tp1_price).any():
        return IdeaPerformance(
            row.symbol, issued, row.entry_low, row.entry_high, row.stop_price, row.tp1_price,
            "TP1", row.tp1_price, (row.tp1_price / entry - 1) * 100,
            "Retest sonrası ilk hedef tamamlanmış günlük barda görüldü.",
        )
    return IdeaPerformance(
        row.symbol, issued, row.entry_low, row.entry_high, row.stop_price, row.tp1_price,
        "ACIK", latest_close, (latest_close / entry - 1) * 100,
        "Plan tetiklendi ancak henüz TP1 veya stop tamamlanmış günlük barda görülmedi.",
    )


def evaluate_due_ideas(
    db: Session,
    *,
    provider: BaseMarketDataProvider,
    minimum_age_days: int = 2,
) -> tuple[IdeaPerformance, ...]:
    """Evaluate due plans conservatively and persist the exact outcome status."""

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(minimum_age_days)))
    rows = (
        db.query(ScheduledTradeIdea)
        .filter(ScheduledTradeIdea.issued_at <= cutoff)
        .filter(ScheduledTradeIdea.evaluation_status.in_(("PENDING", "ACIK", "VERI_YOK", "BELIRSIZ")))
        .order_by(ScheduledTradeIdea.issued_at.asc())
        .all()
    )
    results: list[IdeaPerformance] = []
    for row in rows:
        try:
            performance = _evaluate_row(row, _completed_daily_bars(provider, row))
        except Exception:
            performance = IdeaPerformance(
                row.symbol, row.issued_at, row.entry_low, row.entry_high, row.stop_price, row.tp1_price,
                "VERI_YOK", None, None, "Fiyat serisi doğrulanamadı; sonuç yazılmadı.",
            )
        row.evaluation_status = performance.status
        row.evaluation_price = performance.evaluation_price
        row.return_percent = performance.return_percent
        row.note = performance.note
        row.evaluated_at = datetime.now(timezone.utc)
        results.append(performance)
    db.commit()
    return tuple(results)


def format_idea_performance_report(
    items: Iterable[IdeaPerformance],
    *,
    timezone_name: str = "Europe/Istanbul",
) -> str:
    rows = list(items)
    local = datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name))
    lines = [
        "┏━━ 📊 2 GÜNLÜK PLAN TAKİP RAPORU ━━┓",
        f"🕒 {local:%d.%m.%Y %H:%M} TSİ",
        "Yalnız retest bölgesi tetiklenen planlar ölçülür; temas etmeyen plana kâr/zarar yazılmaz.",
    ]
    if not rows:
        lines.append("\n🟡 Değerlendirme yaşı gelen kayıt yok.")
        return "\n".join(lines)
    measurable = [item for item in rows if item.return_percent is not None]
    for item in rows[:12]:
        entry = f"{item.entry_low:.2f}–{item.entry_high:.2f}"
        result = f"%{item.return_percent:+.2f}" if item.return_percent is not None else "—"
        value = f"{item.evaluation_price:.2f}" if item.evaluation_price is not None else "—"
        lines.extend(
            [
                "",
                f"• {item.symbol}  |  Durum: {item.status}",
                f"  Plan giriş: {entry}  •  TP1: {item.tp1_price:.2f}  •  Stop: {item.stop_price:.2f}",
                f"  Değerlendirme fiyatı: {value}  •  Sonuç: {result}",
                f"  Not: {item.note}",
            ]
        )
    if measurable:
        wins = sum(1 for item in measurable if item.return_percent and item.return_percent > 0)
        average = sum(float(item.return_percent or 0) for item in measurable) / len(measurable)
        lines.extend(
            [
                "",
                f"📌 Ölçülebilen plan: {len(measurable)}  •  Pozitif: {wins}/{len(measurable)}  •  Ortalama: %{average:+.2f}",
                "Bu gerçekleşmiş kapanmış-bar takibidir; komisyon, kayma ve intraday sıra varsayımları dahil değildir.",
            ]
        )
    else:
        lines.append("\n📌 Ölçülebilen tetiklenmiş plan yok; başarı oranı uydurulmadı.")
    return "\n".join(lines)[:4096]
