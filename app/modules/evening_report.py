from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Sequence
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy.orm import Session

from app.analysis.smart_money_engine import SmartMoneyResult, detect_smart_money
from app.models.database import MarketDailyReportLog
from app.modules.chart_engine import NewsTimelineItem, ReportChartSpec
from app.modules.morning_report import EconomicCalendarEvent, fetch_economic_calendar

logger = logging.getLogger("mergen_quant.modules.evening_report")


@dataclass(frozen=True)
class BiasComparison:
    predicted: str | None
    realised: str
    consistent: bool | None
    note: str


@dataclass(frozen=True)
class InstrumentEveningAnalysis:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    change_percent: float
    comparison: BiasComparison
    probable_effect_analysis: str
    smart_money: SmartMoneyResult


@dataclass(frozen=True)
class EveningReport:
    generated_at: datetime
    report_date: date
    instruments: tuple[InstrumentEveningAnalysis, ...]
    calendar_events: tuple[EconomicCalendarEvent, ...]
    consistency_percent: float | None
    failures: tuple[tuple[str, str], ...] = ()


def _completed_rows(df: pd.DataFrame) -> pd.DataFrame:
    data = df.sort_values("timestamp").reset_index(drop=True).copy()
    if "is_complete" in data.columns:
        mask = data["is_complete"].map(
            lambda value: str(value).strip().casefold() in {"true", "1"}
        )
        data = data.loc[mask].reset_index(drop=True)
    return data


def _report_day(now: datetime, timezone_name: str) -> datetime:
    local = now.astimezone(ZoneInfo(timezone_name))
    return datetime.combine(local.date(), time.min, tzinfo=ZoneInfo(timezone_name)).astimezone(timezone.utc)


def _realised_direction(open_price: float, close_price: float, change_percent: float) -> str:
    intraday = ((close_price / open_price) - 1.0) * 100.0 if open_price else change_percent
    effective = (intraday + change_percent) / 2.0
    if effective >= 0.20:
        return "bullish"
    if effective <= -0.20:
        return "bearish"
    return "range"


def _compare_bias(predicted: str | None, realised: str, change: float) -> BiasComparison:
    if not predicted:
        return BiasComparison(None, realised, None, "Sabah bias kaydı bulunamadı.")
    consistent = predicted == realised or (predicted == "range" and abs(change) < 0.40)
    return BiasComparison(
        predicted,
        realised,
        consistent,
        f"Sabah {predicted} → gerçekleşen %{change:+.2f} • {'Tutarlı' if consistent else 'Tutarsız'}",
    )


def _events_for_symbol(
    events: Sequence[EconomicCalendarEvent], symbol: str
) -> list[EconomicCalendarEvent]:
    normalized = symbol.upper().removesuffix(".IS")
    return [event for event in events if normalized in event.affected_instruments]


def _deterministic_effect_text(
    symbol: str,
    change_percent: float,
    events: Sequence[EconomicCalendarEvent],
) -> str:
    if not events:
        return (
            f"{symbol} %{change_percent:+.2f} hareket etti; eşleşen doğrulanmış takvim olayı "
            "bulunmadığı için haber kaynaklı açıklama yapılmadı."
        )
    important = [event.title for event in events if event.impact in {"high", "medium"}]
    names = ", ".join(important[:3]) or ", ".join(event.title for event in events[:2])
    return (
        f"{symbol} hareketi aynı günkü {names} akışıyla birlikte değerlendirilmelidir. "
        "Zamanlama bir ilişki ihtimali gösterir; tek başına nedensellik kanıtlamaz."
    )


def _groq_effect_text(
    db: Session | None,
    settings,
    symbol: str,
    open_price: float,
    close_price: float,
    change_percent: float,
    events: Sequence[EconomicCalendarEvent],
) -> str:
    fallback = _deterministic_effect_text(symbol, change_percent, events)
    if db is None or not getattr(settings, "groq_enabled", False) or not events:
        return fallback
    try:
        from app.services.groq_service import GroqExplainer, KIND_NEWS

        payload = {
            "task": "olası_etki_analizi",
            "movement": {
                "instrument": symbol,
                "open": open_price,
                "close": close_price,
                "change_percent": change_percent,
            },
            "events": [
                {
                    "time": event.event_time.isoformat() if event.event_time else None,
                    "title": event.title,
                    "impact": event.impact,
                    "actual": event.actual,
                    "forecast": event.forecast,
                    "previous": event.previous,
                }
                for event in events[:5]
            ],
            "instruction": (
                "Hareket ile haberler arasında kesin nedensellik kurma; yalnızca olası ve temkinli "
                "bir açıklama yaz."
            ),
        }
        text, is_fallback = GroqExplainer(settings).explain(db, symbol, KIND_NEWS, payload)
        return fallback if is_fallback else text
    except Exception as exc:  # noqa: BLE001 - LLM raporu asla çökertmez
        logger.warning("Groq olası etki analizi üretilemedi symbol=%s error=%s", symbol, type(exc).__name__)
        return fallback


def _morning_row(db: Session | None, day: datetime, symbol: str) -> MarketDailyReportLog | None:
    if db is None:
        return None
    return (
        db.query(MarketDailyReportLog)
        .filter_by(report_date=day, report_type="morning", symbol=symbol)
        .one_or_none()
    )


def _persist_evening(
    db: Session,
    report: EveningReport,
    timezone_name: str,
) -> None:
    day = _report_day(report.generated_at, timezone_name)
    for item in report.instruments:
        row = (
            db.query(MarketDailyReportLog)
            .filter_by(report_date=day, report_type="evening", symbol=item.symbol)
            .one_or_none()
        )
        if row is None:
            row = MarketDailyReportLog(report_date=day, report_type="evening", symbol=item.symbol)
            db.add(row)
        row.predicted_direction = item.comparison.predicted
        row.actual_direction = item.comparison.realised
        row.open_price = item.open
        row.high_price = item.high
        row.low_price = item.low
        row.close_price = item.close
        row.daily_change_percent = item.change_percent
        row.consistent = item.comparison.consistent
        row.news_json = json.dumps(
            [asdict(event) for event in _events_for_symbol(report.calendar_events, item.symbol)],
            ensure_ascii=False,
            default=str,
        )
        row.report_json = json.dumps(
            {
                "comparison": asdict(item.comparison),
                "probable_effect_analysis": item.probable_effect_analysis,
            },
            ensure_ascii=False,
        )
    db.commit()


def build_evening_report(
    provider,
    settings,
    instruments: Sequence[str],
    *,
    db: Session | None = None,
    now: datetime | None = None,
    calendar_events: Sequence[EconomicCalendarEvent] | None = None,
) -> EveningReport:
    generated_at = now or datetime.now(timezone.utc)
    events = list(calendar_events) if calendar_events is not None else fetch_economic_calendar(
        settings, instruments, now=generated_at
    )
    day = _report_day(generated_at, settings.timezone_name)
    end = generated_at
    start = end - timedelta(days=140)
    analyses: list[InstrumentEveningAnalysis] = []
    failures: list[tuple[str, str]] = []
    for raw_symbol in instruments:
        symbol = raw_symbol.upper().removesuffix(".IS")
        try:
            data = _completed_rows(provider.get_ohlcv(symbol, "1d", start, end))
            if len(data) < 25:
                raise ValueError("en az 25 tamamlanmış günlük mum gerekir")
            row = data.iloc[-1]
            previous = float(data.iloc[-2]["close"])
            opened = float(row["open"])
            closed = float(row["close"])
            change = ((closed / previous) - 1.0) * 100.0 if previous else 0.0
            realised = _realised_direction(opened, closed, change)
            morning = _morning_row(db, day, symbol)
            comparison = _compare_bias(
                str(morning.predicted_direction) if morning and morning.predicted_direction else None,
                realised,
                change,
            )
            symbol_events = _events_for_symbol(events, symbol)
            analyses.append(
                InstrumentEveningAnalysis(
                    symbol=symbol,
                    timestamp=pd.Timestamp(row["timestamp"]).to_pydatetime(),
                    open=opened,
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=closed,
                    change_percent=change,
                    comparison=comparison,
                    probable_effect_analysis=_groq_effect_text(
                        db, settings, symbol, opened, closed, change, symbol_events
                    ),
                    smart_money=detect_smart_money(data),
                )
            )
        except Exception as exc:  # noqa: BLE001 - tek sembol raporu durdurmaz
            logger.warning("Akşam raporu sembolü atlandı symbol=%s error=%s", symbol, type(exc).__name__)
            failures.append((symbol, f"{type(exc).__name__}: {str(exc)[:180]}"))
    if not analyses:
        raise ValueError("Akşam raporu için hiçbir enstrümanda doğrulanabilir OHLCV alınamadı.")
    compared = [item.comparison.consistent for item in analyses if item.comparison.consistent is not None]
    consistency = round(sum(bool(value) for value in compared) / len(compared) * 100.0, 1) if compared else None
    report = EveningReport(
        generated_at=generated_at,
        report_date=generated_at.astimezone(ZoneInfo(settings.timezone_name)).date(),
        instruments=tuple(analyses),
        calendar_events=tuple(events),
        consistency_percent=consistency,
        failures=tuple(failures),
    )
    if db is not None:
        _persist_evening(db, report, settings.timezone_name)
    return report


def build_evening_chart_spec(report: EveningReport, symbol: str | None = None) -> ReportChartSpec:
    item = next(
        (entry for entry in report.instruments if entry.symbol == (symbol or "").upper()),
        report.instruments[0],
    )
    timeline = tuple(
        NewsTimelineItem(
            event.event_time.strftime("%H:%M") if event.event_time else "--:--",
            event.title,
            event.impact,
        )
        for event in _events_for_symbol(report.calendar_events, item.symbol)[:4]
    )
    score = 50.0 + max(-25.0, min(25.0, item.change_percent * 5.0))
    return ReportChartSpec(
        instrument=item.symbol,
        timeframe="1D",
        report_kind="evening",
        direction=item.comparison.realised,
        sentiment_score=score,
        change_percent=item.change_percent,
        news_timeline=timeline,
        date_label=report.report_date.strftime("%d.%m.%Y"),
    )


def format_evening_report(report: EveningReport) -> str:
    consistency = (
        f"%{report.consistency_percent:.1f}" if report.consistency_percent is not None else "karşılaştırma yok"
    )
    lines = [
        "🌙 MONTANA MELİH • 21:00 KAPANIŞ RAPORU",
        f"📅 {report.report_date:%d.%m.%Y}",
        f"🎯 Sabah tahmini tutarlılığı: {consistency}",
    ]
    for item in report.instruments[:10]:
        icon = "✅" if item.comparison.consistent else "❌" if item.comparison.consistent is False else "➖"
        lines.extend(
            [
                "",
                f"📊 {item.symbol} • {item.close:.2f} • %{item.change_percent:+.2f}",
                f"A/Y/D/K: {item.open:.2f} / {item.high:.2f} / {item.low:.2f} / {item.close:.2f}",
                f"{icon} {item.comparison.note}",
                f"🧠 Olası etki: {item.probable_effect_analysis}",
            ]
        )
    if len(report.instruments) > 10:
        lines.append(f"\n… {len(report.instruments) - 10} enstrüman daha işlendi.")
    important = [event for event in report.calendar_events if event.impact in {"high", "medium"}]
    lines.extend(["", "📰 GÜN İÇİ HABER / VERİ ZAMAN ÇİZELGESİ"])
    if not important:
        lines.append("Doğrulanmış yüksek/orta etkili etkinlik alınamadı.")
    for event in important[:6]:
        stamp = event.event_time.strftime("%H:%M") if event.event_time else "--:--"
        actual = event.actual or "-"
        lines.append(
            f"• {stamp} {event.country} {event.title} • Açıklanan {actual} / Beklenti {event.forecast or '-'} / Önceki {event.previous or '-'}"
        )
    if report.failures:
        lines.append(f"\n⚠️ Veri alınamayan: {len(report.failures)} sembol")
    lines.append("\nℹ️ Haber-hareket eşleşmesi olası açıklamadır; kesin nedensellik veya yatırım tavsiyesi değildir.")
    return "\n".join(lines)[:4096]
