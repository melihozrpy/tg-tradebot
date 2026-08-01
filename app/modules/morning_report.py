from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Sequence
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
from sqlalchemy.orm import Session

from app.analysis.bist_trade_plan import BistTradePlan, DirectionPlan, build_bist_trade_plan
from app.analysis.indicator_engine import atr, compute_technical_snapshot
from app.analysis.smart_money_engine import SmartMoneyResult, detect_smart_money
from app.analysis.quality_zone_engine import format_quality_zone_scenario
from app.models.database import MarketDailyReportLog, NewsArticle, NewsEvent
from app.modules.chart_engine import ChecklistVisual, ReportChartSpec
from app.services.market_breadth_service import MarketBreadthResult

logger = logging.getLogger("mergen_quant.modules.morning_report")


@dataclass(frozen=True)
class EconomicCalendarEvent:
    event_time: datetime | None
    country: str
    title: str
    impact: str  # high | medium | low
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None
    affected_instruments: tuple[str, ...] = ()
    probable_effect: str = ""


@dataclass(frozen=True)
class ChecklistItem:
    key: str
    label: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class YesterdaySummary:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    change_percent: float
    atr: float
    adr: float
    trend: str


@dataclass(frozen=True)
class InstrumentMorningAnalysis:
    symbol: str
    yesterday: YesterdaySummary
    predicted_direction: str
    checklist: tuple[ChecklistItem, ...]
    checklist_score: int
    setup_label: str
    trade_plan: BistTradePlan
    smart_money: SmartMoneyResult
    sweep_levels: tuple[tuple[float, str], ...] = ()


@dataclass(frozen=True)
class MarketConfidence:
    score: float
    label: str
    components: tuple[str, ...]
    data_coverage: tuple[str, ...]


@dataclass(frozen=True)
class MorningReport:
    generated_at: datetime
    report_date: date
    instruments: tuple[InstrumentMorningAnalysis, ...]
    confidence: MarketConfidence
    calendar_events: tuple[EconomicCalendarEvent, ...]
    failures: tuple[tuple[str, str], ...] = ()
    breadth: MarketBreadthResult | None = None
    index_symbol: str = "XU100"

    @property
    def index_analysis(self) -> InstrumentMorningAnalysis:
        normalized = self.index_symbol.upper().removesuffix(".IS").removeprefix("^")
        item = next((row for row in self.instruments if row.symbol == normalized), None)
        if item is None:
            raise ValueError(f"{normalized} endeks analizi raporda bulunamadı; hisseye otomatik geçiş yapılmadı.")
        return item


_BIST_RE = re.compile(r"^[A-Z0-9]{4,6}$")


def _clean_text(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node is not None else ""


def _impact_from_row(row) -> str:
    raw = " ".join(
        filter(
            None,
            [
                str(row.get("data-impact") or ""),
                str(row.get("class") or ""),
                _clean_text(row.select_one(".sentiment")),
            ],
        )
    ).casefold()
    icon_count = len(row.select(".grayFullBullishIcon, [class*='BullishIcon'], [aria-label*='star']"))
    if icon_count >= 3 or any(word in raw for word in ("high", "yüksek", "bull3")):
        return "high"
    if icon_count == 2 or any(word in raw for word in ("medium", "orta", "bull2")):
        return "medium"
    return "low"


def _parse_event_time(raw: str, report_date: date, timezone_name: str) -> datetime | None:
    value = " ".join((raw or "").split())
    zone = ZoneInfo(timezone_name)
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=zone)
        except ValueError:
            pass
    match = re.search(r"(?P<hour>\d{1,2}):(?P<minute>\d{2})", value)
    if match:
        return datetime.combine(
            report_date,
            time(int(match.group("hour")), int(match.group("minute"))),
            tzinfo=zone,
        )
    return None


def _affected_instruments(country: str, title: str, instruments: Sequence[str]) -> tuple[str, ...]:
    country_norm = country.strip().upper()
    text = title.casefold()
    output: list[str] = []
    for raw_symbol in instruments:
        symbol = raw_symbol.upper().removesuffix(".IS")
        is_bist = bool(_BIST_RE.fullmatch(symbol)) or symbol == "XU100"
        affected = False
        if country_norm in {"TR", "TRY", "TÜRKIYE", "TÜRKİYE"}:
            affected = is_bist or symbol in {"USDTRY", "EURTRY"}
        elif country_norm in {"US", "ABD"}:
            affected = symbol in {"EURUSD", "XAUUSD", "XAGUSD", "US100", "VIX", "DXY"}
            if any(key in text for key in ("fed", "faiz", "tarım dışı", "nfp", "tüfe", "cpi")):
                affected = affected or is_bist
        elif country_norm in {"EU", "DE", "FR", "IT", "ES"}:
            affected = symbol in {"EURUSD", "DXY"}
        elif country_norm in {"UK", "GB"}:
            affected = symbol in {"GBPUSD", "DXY"}
        if affected:
            output.append(symbol)
    return tuple(dict.fromkeys(output))


def parse_economic_calendar_html(
    html: str,
    *,
    report_date: date,
    timezone_name: str,
    instruments: Sequence[str],
) -> list[EconomicCalendarEvent]:
    """Investing'in eski ve yeni takvim satırlarını aynı anda destekler."""

    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover - Docker bağımlılığı doğrulanır
        raise RuntimeError("Ekonomik takvim için beautifulsoup4 kurulmalıdır.") from exc

    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select(
        "tr.js-event-item, tr[data-event-datetime], [data-test='economic-calendar-row'], "
        "article[data-event-datetime]"
    )
    events: list[EconomicCalendarEvent] = []
    for row in rows:
        title = _clean_text(
            row.select_one(".event, .event-title, [data-test='event-title'], td:nth-of-type(4)")
        )
        if not title:
            continue
        country = _clean_text(
            row.select_one(".flagCur, .country, [data-test='country'], td:nth-of-type(2)")
        )
        time_text = str(row.get("data-event-datetime") or "") or _clean_text(
            row.select_one(".time, [data-test='event-time'], td:first-of-type")
        )
        actual = _clean_text(row.select_one(".act, [data-test='actual'], td:nth-last-of-type(3)")) or None
        forecast = _clean_text(row.select_one(".fore, [data-test='forecast'], td:nth-last-of-type(2)")) or None
        previous = _clean_text(row.select_one(".prev, [data-test='previous'], td:nth-last-of-type(1)")) or None
        event_time = _parse_event_time(time_text, report_date, timezone_name)
        if event_time is not None and event_time.date() != report_date:
            continue
        events.append(
            EconomicCalendarEvent(
                event_time=event_time,
                country=country or "--",
                title=title,
                impact=_impact_from_row(row),
                actual=actual,
                forecast=forecast,
                previous=previous,
                affected_instruments=_affected_instruments(country, title, instruments),
            )
        )
    events.sort(key=lambda item: item.event_time or datetime.max.replace(tzinfo=ZoneInfo(timezone_name)))
    return events


def fetch_economic_calendar(
    settings,
    instruments: Sequence[str],
    *,
    now: datetime | None = None,
    client: httpx.Client | None = None,
) -> list[EconomicCalendarEvent]:
    if not getattr(settings, "economic_calendar_enabled", True):
        return []
    zone = ZoneInfo(settings.timezone_name)
    current = (now or datetime.now(timezone.utc)).astimezone(zone)
    owns_client = client is None
    http = client or httpx.Client(
        timeout=settings.economic_calendar_timeout_seconds,
        follow_redirects=True,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; MergenQuant/3.0; +market-report)",
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.7",
        },
    )
    try:
        response = http.get(settings.economic_calendar_url)
        response.raise_for_status()
        events = parse_economic_calendar_html(
            response.text,
            report_date=current.date(),
            timezone_name=settings.timezone_name,
            instruments=instruments,
        )
        return events[: int(settings.report_max_news_events)]
    except Exception as exc:  # noqa: BLE001 - rapor teknik bölümle devam eder
        logger.warning("Ekonomik takvim alınamadı: %s", type(exc).__name__)
        return []
    finally:
        if owns_client:
            http.close()


def _enrich_calendar_events_with_groq(
    events: Sequence[EconomicCalendarEvent],
    *,
    db: Session | None,
    settings,
) -> list[EconomicCalendarEvent]:
    if db is None or not getattr(settings, "groq_enabled", False):
        return list(events)
    from app.services.groq_service import GroqExplainer, KIND_NEWS

    explainer = GroqExplainer(settings)
    output: list[EconomicCalendarEvent] = []
    interpreted = 0
    for event in events:
        if event.impact not in {"high", "medium"} or interpreted >= 5:
            output.append(event)
            continue
        payload = {
            "task": "ekonomik_takvim_olasi_etki",
            "country": event.country,
            "title": event.title,
            "impact": event.impact,
            "actual": event.actual,
            "forecast": event.forecast,
            "previous": event.previous,
            "affected_instruments": event.affected_instruments,
            "instruction": "Kesin yön iddiası kurmadan olası piyasa etkisini 1-2 cümlede açıkla.",
        }
        try:
            text, is_fallback = explainer.explain(db, "MARKET", KIND_NEWS, payload)
            output.append(replace(event, probable_effect="" if is_fallback else text))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Takvim Groq yorumu alınamadı: %s", type(exc).__name__)
            output.append(event)
        interpreted += 1
    return output


def _completed_rows(df: pd.DataFrame) -> pd.DataFrame:
    data = df.sort_values("timestamp").reset_index(drop=True).copy()
    if "is_complete" in data.columns:
        complete = data["is_complete"].map(
            lambda value: str(value).strip().casefold() in {"true", "1"}
        )
        data = data.loc[complete].reset_index(drop=True)
    return data


def _detect_sweep(data: pd.DataFrame) -> tuple[str | None, tuple[tuple[float, str], ...]]:
    if len(data) < 25:
        return None, ()
    prior = data.iloc[-21:-1]
    latest = data.iloc[-1]
    prior_high = float(prior["high"].max())
    prior_low = float(prior["low"].min())
    levels: list[tuple[float, str]] = []
    direction: str | None = None
    if float(latest["low"]) < prior_low and float(latest["close"]) > prior_low:
        direction = "bullish"
        levels.append((prior_low, "satış likiditesi alındı"))
    if float(latest["high"]) > prior_high and float(latest["close"]) < prior_high:
        direction = "bearish"
        levels.append((prior_high, "alış likiditesi alındı"))
    return direction, tuple(levels)


def _trend_label(snapshot) -> str:
    if snapshot.trend_direction == "up":
        return "bullish"
    if snapshot.trend_direction == "down":
        return "bearish"
    return "range"


def _pick_direction_plan(plan: BistTradePlan, direction: str) -> DirectionPlan:
    if direction == "bearish":
        return plan.short
    return plan.long


def analyze_morning_instrument(
    symbol: str,
    df: pd.DataFrame,
    *,
    calendar_events: Sequence[EconomicCalendarEvent] = (),
) -> InstrumentMorningAnalysis:
    data = _completed_rows(df)
    if len(data) < 60:
        raise ValueError(f"{symbol} için en az 60 tamamlanmış günlük mum gerekir.")
    snapshot = compute_technical_snapshot(data, symbol, "1d")
    smart = detect_smart_money(data)
    plan = build_bist_trade_plan(data, symbol)
    trend = _trend_label(snapshot)
    direction = (
        "bullish" if plan.preferred_direction == "LONG"
        else "bearish" if plan.preferred_direction == "SHORT"
        else trend
    )
    direction_plan = _pick_direction_plan(plan, direction)
    sweep_direction, sweep_levels = _detect_sweep(data)
    structure_confirmation = next(
        (event for event in reversed(smart.structure) if event.direction == direction), None
    )
    zones_available = any(zone.direction == direction for zone in (*smart.order_blocks, *smart.fvg))
    matching_high_risk = [
        event
        for event in calendar_events
        if event.impact == "high" and symbol.upper().removesuffix(".IS") in event.affected_instruments
    ]
    rr_candidates = [value for value in direction_plan.risk_multiples if value >= 2.0]
    checklist = (
        ChecklistItem(
            "daily_bias", "Daily bias", trend != "range" and direction == trend,
            f"Trend {trend}, ADX {snapshot.adx:.1f}",
        ),
        ChecklistItem(
            "zones", "HTF/LTF zone", zones_available,
            "OB/FVG bulundu" if zones_available else "Yönle uyumlu aktif OB/FVG yok",
        ),
        ChecklistItem(
            "sweep_confirmation", "Sweep + BOS/MSS",
            sweep_direction == direction and structure_confirmation is not None,
            (
                f"{structure_confirmation.kind} + likidite süpürmesi"
                if sweep_direction == direction and structure_confirmation is not None
                else "Sweep ve yapı teyidi birlikte oluşmadı"
            ),
        ),
        ChecklistItem(
            "a_plus", "A+ plan uyumu", direction_plan.score >= 78,
            f"İşlem kalitesi {direction_plan.score}/100",
        ),
        ChecklistItem(
            "news", "Haber riski", not matching_high_risk,
            "Yüksek etkili eşleşen veri yok" if not matching_high_risk else f"{len(matching_high_risk)} yüksek etkili olay var",
        ),
        ChecklistItem(
            "rr", "Minimum 1:2 RR", bool(rr_candidates),
            f"Uygun hedef {rr_candidates[0]:.2f}R" if rr_candidates else "2R hedef üretilemedi",
        ),
    )
    passed = sum(item.passed for item in checklist)
    last = data.iloc[-1]
    previous = float(data.iloc[-2]["close"])
    close = float(last["close"])
    atr_value = float(atr(data, 14).iloc[-1])
    adr = float((data.tail(14)["high"] - data.tail(14)["low"]).mean())
    timestamp = pd.Timestamp(last["timestamp"]).to_pydatetime()
    return InstrumentMorningAnalysis(
        symbol=symbol.upper().removesuffix(".IS"),
        yesterday=YesterdaySummary(
            timestamp=timestamp,
            open=float(last["open"]),
            high=float(last["high"]),
            low=float(last["low"]),
            close=close,
            change_percent=((close / previous) - 1.0) * 100.0 if previous else 0.0,
            atr=atr_value,
            adr=adr,
            trend=trend,
        ),
        predicted_direction=direction,
        checklist=checklist,
        checklist_score=passed,
        setup_label="A+ setup adayı" if passed >= 5 else "Teyit bekle" if passed >= 3 else "Pas geç",
        trade_plan=plan,
        smart_money=smart,
        sweep_levels=sweep_levels,
    )


def _daily_change(df: pd.DataFrame) -> float | None:
    data = _completed_rows(df)
    if len(data) < 2:
        return None
    previous = float(data.iloc[-2]["close"])
    current = float(data.iloc[-1]["close"])
    return ((current / previous) - 1.0) * 100.0 if previous else None


def _database_news_sentiment(db: Session | None, settings, since: datetime) -> tuple[int, int]:
    if db is None:
        return 0, 0
    rows = (
        db.query(NewsArticle.title)
        .select_from(NewsEvent)
        .join(NewsArticle, NewsEvent.article_id == NewsArticle.id)
        .filter((NewsArticle.published_at >= since) | (NewsEvent.created_at >= since))
        .order_by(NewsArticle.published_at.desc().nullslast())
        .limit(40)
        .all()
    )
    titles = [str(item[0]) for item in rows if item[0]]
    if not titles:
        return 0, 0
    from app.services.groq_service import GroqExplainer

    labels, _is_fallback = GroqExplainer(settings).classify_news_sentiment(db, titles)
    positive = sum(label == "positive" for label in labels)
    negative = sum(label == "negative" for label in labels)
    return positive, negative


def compute_market_confidence(
    primary: InstrumentMorningAnalysis,
    *,
    vix_change: float | None,
    dxy_change: float | None,
    positive_news: int,
    negative_news: int,
) -> MarketConfidence:
    score = 50.0
    components: list[str] = []
    coverage: list[str] = ["XU100 teknik yapı"]
    direction = primary.predicted_direction
    if direction == "bullish":
        score += 14
        components.append("Endeks teknik yönü +14")
    elif direction == "bearish":
        score -= 14
        components.append("Endeks teknik yönü -14")
    else:
        components.append("Endeks yatay 0")
    score += (primary.checklist_score - 3) * 3
    components.append(f"SMXM {primary.checklist_score}/6: {(primary.checklist_score - 3) * 3:+d}")

    atr_adr = primary.yesterday.atr / primary.yesterday.adr if primary.yesterday.adr > 0 else None
    if atr_adr is not None:
        coverage.append("ATR/ADR")
        if atr_adr > 1.20:
            score -= 8
            components.append(f"ATR/ADR {atr_adr:.2f}: -8")
        elif atr_adr < 0.85:
            score += 4
            components.append(f"ATR/ADR {atr_adr:.2f}: +4")
        else:
            components.append(f"ATR/ADR {atr_adr:.2f}: 0")

    if vix_change is not None:
        coverage.append("VIX")
        effect = -12 if vix_change >= 5 else -6 if vix_change > 1 else 6 if vix_change < -1 else 0
        score += effect
        components.append(f"VIX %{vix_change:+.2f}: {effect:+d}")
    else:
        components.append("VIX verisi yok: skora katılmadı")
    if dxy_change is not None:
        coverage.append("DXY")
        effect = -6 if dxy_change > 0.5 else 4 if dxy_change < -0.5 else 0
        score += effect
        components.append(f"DXY %{dxy_change:+.2f}: {effect:+d}")
    else:
        components.append("DXY verisi yok: skora katılmadı")

    total_news = positive_news + negative_news
    if total_news:
        coverage.append("24s haber duyarlılığı")
        news_effect = round((positive_news - negative_news) / total_news * 8)
        score += news_effect
        components.append(f"Haber {positive_news}+/{negative_news}-: {news_effect:+d}")
    else:
        components.append("Haber örneklemi yok: skora katılmadı")

    score = round(max(0.0, min(100.0, score)), 1)
    label = "Risk-On" if score >= 65 else "Risk-Off" if score < 40 else "Nötr"
    return MarketConfidence(score, label, tuple(components), tuple(coverage))


def _report_day(now: datetime, timezone_name: str) -> datetime:
    local = now.astimezone(ZoneInfo(timezone_name))
    return datetime.combine(local.date(), time.min, tzinfo=ZoneInfo(timezone_name)).astimezone(timezone.utc)


def _persist_morning(db: Session, report: MorningReport, timezone_name: str) -> None:
    day = _report_day(report.generated_at, timezone_name)
    for item in report.instruments:
        row = (
            db.query(MarketDailyReportLog)
            .filter_by(report_date=day, report_type="morning", symbol=item.symbol)
            .one_or_none()
        )
        if row is None:
            row = MarketDailyReportLog(report_date=day, report_type="morning", symbol=item.symbol)
            db.add(row)
        row.predicted_direction = item.predicted_direction
        row.confidence_score = report.confidence.score
        row.checklist_passed = item.checklist_score
        row.checklist_total = len(item.checklist)
        row.open_price = item.yesterday.open
        row.high_price = item.yesterday.high
        row.low_price = item.yesterday.low
        row.close_price = item.yesterday.close
        row.daily_change_percent = item.yesterday.change_percent
        row.news_json = json.dumps([asdict(event) for event in report.calendar_events], ensure_ascii=False, default=str)
        row.report_json = json.dumps(
            {
                "setup_label": item.setup_label,
                "checklist": [asdict(entry) for entry in item.checklist],
                "confidence_components": report.confidence.components,
            },
            ensure_ascii=False,
        )
    db.commit()


def build_morning_report(
    provider,
    settings,
    instruments: Sequence[str],
    *,
    db: Session | None = None,
    now: datetime | None = None,
    calendar_events: Sequence[EconomicCalendarEvent] | None = None,
    breadth: MarketBreadthResult | None = None,
) -> MorningReport:
    generated_at = now or datetime.now(timezone.utc)
    events = list(calendar_events) if calendar_events is not None else fetch_economic_calendar(
        settings, instruments, now=generated_at
    )
    events = _enrich_calendar_events_with_groq(events, db=db, settings=settings)
    analyses: list[InstrumentMorningAnalysis] = []
    failures: list[tuple[str, str]] = []
    frames: dict[str, pd.DataFrame] = {}
    end = generated_at
    start = end - timedelta(days=520)
    for raw_symbol in instruments:
        symbol = raw_symbol.upper().removesuffix(".IS")
        try:
            frame = provider.get_ohlcv(symbol, "1d", start, end)
            frames[symbol] = frame
            analyses.append(analyze_morning_instrument(symbol, frame, calendar_events=events))
        except Exception as exc:  # noqa: BLE001 - bir sembol tüm raporu bozmaz
            logger.warning("Sabah raporu sembolü atlandı symbol=%s error=%s", symbol, type(exc).__name__)
            failures.append((symbol, f"{type(exc).__name__}: {str(exc)[:180]}"))
    if not analyses:
        raise ValueError("Sabah raporu için hiçbir enstrümanda doğrulanabilir OHLCV alınamadı.")

    primary_symbol = str(settings.xu100_symbol).upper().removesuffix(".IS").removeprefix("^")
    primary = next((item for item in analyses if item.symbol == primary_symbol), None)
    if primary is None:
        reason = next((reason for symbol, reason in failures if symbol == primary_symbol), "veri alınamadı")
        raise ValueError(
            f"{primary_symbol} endeks verisi doğrulanamadı ({reason}); rapor THYAO veya başka hisseye düşürülmedi."
        )
    vix_change = dxy_change = None
    for setting_name, output_name in (("vix_symbol", "vix"), ("dxy_symbol", "dxy")):
        risk_symbol = getattr(settings, setting_name)
        try:
            change = _daily_change(provider.get_ohlcv(risk_symbol, "1d", end - timedelta(days=20), end))
            if output_name == "vix":
                vix_change = change
            else:
                dxy_change = change
        except Exception as exc:  # noqa: BLE001 - eksik risk göstergesi raporu durdurmaz
            logger.info("Risk göstergesi alınamadı symbol=%s error=%s", risk_symbol, type(exc).__name__)
    positive, negative = _database_news_sentiment(
        db, settings, generated_at - timedelta(hours=24)
    )
    confidence = compute_market_confidence(
        primary,
        vix_change=vix_change,
        dxy_change=dxy_change,
        positive_news=positive,
        negative_news=negative,
    )
    report = MorningReport(
        generated_at=generated_at,
        report_date=generated_at.astimezone(ZoneInfo(settings.timezone_name)).date(),
        instruments=tuple(analyses),
        confidence=confidence,
        calendar_events=tuple(events),
        failures=tuple(failures),
        breadth=breadth,
        index_symbol=primary_symbol,
    )
    if db is not None:
        _persist_morning(db, report, settings.timezone_name)
    return report


def build_morning_chart_spec(report: MorningReport, symbol: str | None = None) -> ReportChartSpec:
    requested = (symbol or report.index_symbol).upper().removesuffix(".IS").removeprefix("^")
    item = next((entry for entry in report.instruments if entry.symbol == requested), None)
    if item is None:
        raise ValueError(f"{requested} raporda bulunamadı; grafik başka hisseyle üretilmedi.")
    plan = _pick_direction_plan(item.trade_plan, item.predicted_direction)
    quality_zone = item.trade_plan.quality_zone
    if quality_zone is not None:
        quality_targets = tuple(
            value for value in (quality_zone.target_1, quality_zone.target_2) if value is not None
        )
        quality_rr = max(
            (value for value in (quality_zone.rr_1, quality_zone.rr_2) if value is not None),
            default=0.0,
        )
        return ReportChartSpec(
            instrument=item.symbol,
            timeframe="1D",
            report_kind="morning",
            direction=quality_zone.direction,
            sentiment_score=report.confidence.score,
            checklist=tuple(ChecklistVisual(entry.label, entry.passed) for entry in item.checklist),
            entry_low=quality_zone.zone_low,
            entry_high=quality_zone.zone_high,
            entry_price=quality_zone.entry,
            stop=quality_zone.invalidation,
            targets=quality_targets,
            rr=quality_rr,
            liquidity_levels=item.sweep_levels,
            date_label=report.report_date.strftime("%d.%m.%Y"),
        )
    rr = next((value for value in plan.risk_multiples if value >= 2), plan.risk_multiples[-1])
    target_count = max(1, min(len(plan.targets), plan.risk_multiples.index(rr) + 1))
    return ReportChartSpec(
        instrument=item.symbol,
        timeframe="1D",
        report_kind="morning",
        direction=item.predicted_direction,
        sentiment_score=report.confidence.score,
        checklist=tuple(ChecklistVisual(entry.label, entry.passed) for entry in item.checklist),
        entry_low=plan.entry_low,
        entry_high=plan.entry_high,
        stop=plan.stop_standard,
        targets=plan.targets[:target_count],
        rr=rr,
        liquidity_levels=item.sweep_levels,
        date_label=report.report_date.strftime("%d.%m.%Y"),
    )


def format_morning_report(report: MorningReport) -> str:
    direction_map = {"bullish": "YUKARI", "bearish": "AŞAĞI", "range": "YATAY"}
    item = report.index_analysis
    summary = item.yesterday
    plan = _pick_direction_plan(item.trade_plan, item.predicted_direction)
    quality_zone = item.trade_plan.quality_zone
    lines = [
        "🌅 MONTANA FİNANS ROBOTU • 09:00 XU100 AÇILIŞ RAPORU",
        f"📅 {report.report_date:%d.%m.%Y}",
        "",
        f"🛡️ Piyasa güveni: {report.confidence.score:.0f}/100 • {report.confidence.label}",
        f"🧭 Bugünün koşullu yönü: {direction_map.get(item.predicted_direction, item.predicted_direction.upper())}",
        "",
        "📊 XU100 • DÜNÜN KANITI",
        f"• Kapanış {summary.close:.2f} • %{summary.change_percent:+.2f}",
        f"• Açılış/Yüksek/Düşük: {summary.open:.2f} / {summary.high:.2f} / {summary.low:.2f}",
        f"• ATR {summary.atr:.2f} • ADR {summary.adr:.2f} • Yapı: {summary.trend}",
        "",
        f"🧩 SMXM CHECKLIST • {item.checklist_score}/6 • {item.setup_label}",
    ]
    lines.extend(
        f"{'✅' if check.passed else '❌'} {check.label}: {check.detail[:110]}"
        for check in item.checklist
    )
    if quality_zone is not None:
        lines.extend(["", format_quality_zone_scenario(quality_zone)])
    else:
        lines.extend(
            [
                "",
                f"🎯 {plan.direction} PLANI • kalite {plan.score}/100 • {plan.status}",
                "• Doğrulanmış OB/FVG yok; güncel fiyattan işlem açılmaz ve yeni bölge beklenir.",
            ]
        )
    breadth = report.breadth
    if breadth and breadth.available:
        lines.extend(
            [
                "",
                "🌐 571 HİSSE • PİYASA İÇ YAPISI",
                f"• Kapsam: {breadth.scanned}/{breadth.universe_size} (%{breadth.coverage_ratio:.1f})",
                f"• Puan: {breadth.breadth_score}/100 • {breadth.regime}",
                f"• Yükselen/Düşen/Yatay: {breadth.advancers}/{breadth.decliners}/{breadth.unchanged}",
                f"• EMA20/50/200 üstü: %{breadth.above_ema20_ratio:.1f} / %{breadth.above_ema50_ratio:.1f} / "
                + (f"%{breadth.above_ema200_ratio:.1f}" if breadth.above_ema200_ratio is not None else "veri yetersiz"),
                f"• Long {breadth.long_count} • Short/Risk {breadth.short_count} • Nötr {breadth.neutral_count}",
                f"🔮 Açılış çerçevesi: {breadth.tomorrow_bias}",
            ]
        )
        if breadth.long_candidates:
            lines.append("🟢 Güçlü long izleme: " + ", ".join(f"{x.symbol}({x.score})" for x in breadth.long_candidates[:6]))
        if breadth.short_candidates:
            lines.append("🔴 Zayıf/short-risk: " + ", ".join(f"{x.symbol}({x.score})" for x in breadth.short_candidates[:6]))
    elif breadth is not None:
        lines.extend(["", f"⚠️ 571 hisse taraması doğrulanamadı: {breadth.note}"])
    lines.extend(["", "🗓️ BUGÜNÜN ÖNEMLİ TAKVİMİ"])
    important = [event for event in report.calendar_events if event.impact in {"high", "medium"}]
    if not important:
        lines.append("Doğrulanmış yüksek/orta etkili etkinlik alınamadı.")
    for event in important[:6]:
        icon = "🔴" if event.impact == "high" else "🟠"
        stamp = event.event_time.strftime("%H:%M") if event.event_time else "--:--"
        affected = ", ".join(event.affected_instruments[:5]) or "eşleme yok"
        lines.append(f"{icon} {stamp} {event.country} • {event.title}\n   Etki: {affected}")
        if event.probable_effect:
            lines.append(f"   🧠 {event.probable_effect[:240]}")
    if report.failures:
        lines.append(f"\n⚠️ Veri alınamayan: {len(report.failures)} sembol (/veri_durumu ile kontrol et)")
    lines.append(
        "\nℹ️ Yön tahmini; XU100 kapanışı, 571 hisse genişliği ve doğrulanmış teknik kanıtların koşullu birleşimidir. "
        "Açılış boşluğu ve ilk 15–30 dakikalık teyit görülmeden işlem sinyali sayılmaz; yatırım tavsiyesi değildir."
    )
    return "\n".join(lines)[:4096]
