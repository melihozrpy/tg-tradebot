from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.analysis.news_impact_engine import (
    NewsImpactAssessment,
    NewsImpactSummary,
    assess_article,
    summarize_impact,
)
from app.config.settings import Settings, load_company_aliases
from app.data.gdelt_provider import GdeltNewsProvider, GdeltUnavailableError, RawNewsArticle, compute_dedup_key
from app.models.database import NewsArticle, NewsEvent, NewsImpactSnapshot, ProviderHealthLog

logger = logging.getLogger("mergen_quant.news")

WINDOW_24H = "24h"
WINDOW_7D = "7d"

TOP_EVENTS_LIMIT = 3


class CompanyAliasNotFoundError(Exception):
    """Sembol icin company_aliases.yaml'da kayit yoksa firlatilir; bu durumda
    GDELT'e HICBIR istek atilmaz (uydurma isimle arama yapilmaz)."""


@dataclass
class SymbolNewsScanOutcome:
    symbol: str
    new_articles: list[NewsArticle] = field(default_factory=list)
    duplicate_count: int = 0
    summary_24h: Optional[NewsImpactSummary] = None
    summary_7d: Optional[NewsImpactSummary] = None
    gdelt_error: Optional[str] = None


def get_company_alias_info(symbol: str, path: Optional[str] = None) -> Optional[dict]:
    """Sembol icin sirket adi + alternatif isim listesini doner.
    Kayit yoksa None doner (uydurma isim ASLA uretilmez)."""
    aliases_config = load_company_aliases(path)
    entry = aliases_config.get("symbols", {}).get(symbol.upper())
    if not entry:
        return None
    return {
        "company_name": entry.get("company_name", ""),
        "aliases": entry.get("aliases", []) or [],
    }


def _company_match_confidence(matched_alias: str, company_name: str, aliases: list[str]) -> float:
    matched_lower = (matched_alias or "").strip().lower()
    if matched_lower == company_name.strip().lower():
        return 90.0
    if matched_lower in [a.strip().lower() for a in aliases]:
        return 75.0
    return 35.0


def record_provider_health(db: Session, provider: str, status: str, detail: str = "") -> None:
    row = ProviderHealthLog(provider=provider, status=status, detail=detail[:2000] if detail else None)
    db.add(row)
    db.commit()


def scan_symbol_news(
    db: Session,
    symbol: str,
    gdelt_provider: GdeltNewsProvider,
    settings: Settings,
    lookback_days: int = 7,
) -> SymbolNewsScanOutcome:
    """Bir sembol icin GDELT'ten haber cekip, tekillestirip veritabanina
    kaydeder ve 24s/7g haber etkisi ozetlerini hesaplar.

    - GDELT_ENABLED=false ise veya sirket eslestirmesi yoksa: GDELT'e HICBIR
      istek atilmaz, teknik analiz/analiz akisi ETKILENMEZ (bos sonuc doner).
    - GDELT hata verirse: teknik analiz DEVAM EDER; hata provider_health_logs'a
      kaydedilir ve outcome.gdelt_error doldurulur.
    """
    symbol = symbol.upper()

    if not settings.gdelt_enabled:
        return SymbolNewsScanOutcome(symbol=symbol, gdelt_error=None)

    alias_info = get_company_alias_info(symbol, settings.company_aliases_path)
    if alias_info is None:
        return SymbolNewsScanOutcome(symbol=symbol, gdelt_error=None)

    try:
        raw_articles = gdelt_provider.fetch_articles(
            symbol, alias_info["company_name"], alias_info["aliases"], lookback_days=lookback_days,
        )
        record_provider_health(db, "gdelt", "ok", f"{len(raw_articles)} haber alindi.")
    except GdeltUnavailableError as exc:
        logger.warning("GDELT haber taramasi basarisiz symbol=%s: %s", symbol, exc)
        record_provider_health(db, "gdelt", "error", str(exc))
        return SymbolNewsScanOutcome(symbol=symbol, gdelt_error=str(exc))

    new_articles, duplicate_count = _persist_articles(db, symbol, raw_articles, alias_info)

    summary_24h = compute_news_impact_snapshot(db, symbol, WINDOW_24H, since_hours=24)
    summary_7d = compute_news_impact_snapshot(db, symbol, WINDOW_7D, since_hours=24 * 7)

    return SymbolNewsScanOutcome(
        symbol=symbol,
        new_articles=new_articles,
        duplicate_count=duplicate_count,
        summary_24h=summary_24h,
        summary_7d=summary_7d,
        gdelt_error=None,
    )


def _persist_articles(
    db: Session, symbol: str, raw_articles: list[RawNewsArticle], alias_info: dict,
) -> tuple[list[NewsArticle], int]:
    """Haberleri dedup_key'e gore tekillestirerek kaydeder. Ayni dedup_key
    (ayni haber, farkli kaynak) icin yeni satir ACILMAZ; mevcut satirin
    `duplicate_source_count` degeri artirilir (bolum 1: kopyalari birlestir)."""
    new_rows: list[NewsArticle] = []
    duplicate_count = 0

    for raw in raw_articles:
        dedup_key = compute_dedup_key(raw.title, raw.published_at)
        existing = db.query(NewsArticle).filter(NewsArticle.dedup_key == dedup_key).first()
        if existing is not None:
            existing.duplicate_source_count = (existing.duplicate_source_count or 1) + 1
            duplicate_count += 1
            continue

        confidence = _company_match_confidence(raw.matched_alias, alias_info["company_name"], alias_info["aliases"])
        article = NewsArticle(
            symbol=symbol,
            title=raw.title,
            source=raw.source,
            url=raw.url,
            published_at=raw.published_at,
            language=raw.language,
            company_match_confidence=confidence,
            matched_alias=raw.matched_alias,
            dedup_key=dedup_key,
            duplicate_source_count=1,
            provider="gdelt",
        )
        db.add(article)
        db.flush()

        assessment = assess_article(raw.title, raw.source, confidence, raw.published_at)
        event = NewsEvent(
            article_id=article.id,
            symbol=symbol,
            category=assessment.category,
            impact_score=assessment.impact_score,
            confidence_score=assessment.confidence_score,
            source_confidence=assessment.source_confidence,
            company_match_confidence=assessment.company_match_confidence,
            news_age_hours=assessment.news_age_hours,
            rationale=assessment.rationale,
        )
        db.add(event)
        new_rows.append(article)

    if new_rows or duplicate_count:
        db.commit()
        for row in new_rows:
            db.refresh(row)

    return new_rows, duplicate_count


def _assessments_for_window(db: Session, symbol: str, since_hours: int) -> list[NewsImpactAssessment]:
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    rows = (
        db.query(NewsEvent, NewsArticle)
        .join(NewsArticle, NewsEvent.article_id == NewsArticle.id)
        .filter(NewsEvent.symbol == symbol.upper())
        .filter((NewsArticle.published_at >= since) | (NewsArticle.published_at.is_(None) & (NewsEvent.created_at >= since)))
        .all()
    )
    assessments: list[NewsImpactAssessment] = []
    for event, article in rows:
        assessments.append(
            NewsImpactAssessment(
                category=event.category,
                impact_score=event.impact_score,
                confidence_score=event.confidence_score,
                source_confidence=event.source_confidence,
                company_match_confidence=event.company_match_confidence,
                news_age_hours=event.news_age_hours,
                rationale=event.rationale,
                counts_toward_score=event.company_match_confidence >= 40.0,
            )
        )
    return assessments


def compute_news_impact_snapshot(db: Session, symbol: str, window_label: str, since_hours: int) -> NewsImpactSummary:
    assessments = _assessments_for_window(db, symbol, since_hours)
    summary = summarize_impact(assessments)

    top_events_payload = [
        {
            "category": a.category,
            "category_label": a.category_label_tr,
            "impact_score": a.impact_score,
            "confidence_score": a.confidence_score,
            "rationale": a.rationale,
        }
        for a in summary.top_assessments
    ]

    snapshot = NewsImpactSnapshot(
        symbol=symbol.upper(),
        window_label=window_label,
        article_count=summary.article_count,
        impact_score=summary.impact_score,
        confidence_score=summary.confidence_score,
        top_events_json=json.dumps(top_events_payload, ensure_ascii=False),
    )
    db.add(snapshot)
    db.commit()

    return summary


def get_recent_articles(db: Session, symbol: str, limit: int = 10) -> list[NewsArticle]:
    return (
        db.query(NewsArticle)
        .filter(NewsArticle.symbol == symbol.upper())
        .order_by(NewsArticle.published_at.desc().nullslast(), NewsArticle.created_at.desc())
        .limit(limit)
        .all()
    )


def get_recent_events_with_articles(db: Session, symbol: str, limit: int = 10) -> list[tuple[NewsEvent, NewsArticle]]:
    return (
        db.query(NewsEvent, NewsArticle)
        .join(NewsArticle, NewsEvent.article_id == NewsArticle.id)
        .filter(NewsEvent.symbol == symbol.upper())
        .order_by(NewsArticle.published_at.desc().nullslast())
        .limit(limit)
        .all()
    )


@dataclass
class NewsAnalysisContext:
    """/analiz ve /analiz_detay mesajlarina eklenecek kompakt haber ozeti."""

    available: bool
    count_24h: int
    count_7d: int
    impact_score: Optional[float]
    confidence_score: Optional[float]
    score_contribution: float
    top_events: list[NewsImpactAssessment] = field(default_factory=list)
    note: str = ""


def build_news_context_for_analysis(
    db: Session,
    provider: Optional[GdeltNewsProvider],
    symbol: str,
    settings: Settings,
) -> NewsAnalysisContext:
    """/analiz akisindan cagrilan, HATA VERMEYEN yardimci: GDELT kapaliysa,
    sirket eslestirmesi yoksa veya GDELT hata verirse teknik analizi
    ETKILEMEDEN 'haber yok' baglami doner (skor katkisi 0.0)."""
    if not settings.gdelt_enabled or provider is None:
        return NewsAnalysisContext(
            available=False, count_24h=0, count_7d=0, impact_score=None,
            confidence_score=None, score_contribution=0.0, note="GDELT haber radarı devre dışı.",
        )

    try:
        outcome = scan_symbol_news(db, symbol, provider, settings)
    except Exception as exc:  # noqa: BLE001 - haber taramasi analiz akisini ASLA cokertmemeli
        logger.warning("Haber baglami hesaplanamadi symbol=%s: %s", symbol, exc)
        return NewsAnalysisContext(
            available=False, count_24h=0, count_7d=0, impact_score=None,
            confidence_score=None, score_contribution=0.0, note="Haber verisi alınamadı.",
        )

    if outcome.gdelt_error:
        return NewsAnalysisContext(
            available=False, count_24h=0, count_7d=0, impact_score=None,
            confidence_score=None, score_contribution=0.0, note="Haber verisi alınamadı; teknik analiz etkilenmedi.",
        )

    summary_7d = outcome.summary_7d
    summary_24h = outcome.summary_24h
    if summary_7d is None or not summary_7d.available:
        return NewsAnalysisContext(
            available=False, count_24h=0, count_7d=0, impact_score=None,
            confidence_score=None, score_contribution=0.0, note="Haber bulunamadı.",
        )

    return NewsAnalysisContext(
        available=True,
        count_24h=summary_24h.article_count if summary_24h else 0,
        count_7d=summary_7d.article_count,
        impact_score=summary_7d.impact_score,
        confidence_score=summary_7d.confidence_score,
        score_contribution=summary_7d.score_contribution,
        top_events=summary_7d.top_assessments[:TOP_EVENTS_LIMIT],
        note=summary_7d.note,
    )
