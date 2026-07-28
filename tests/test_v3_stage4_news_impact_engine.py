from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.analysis.news_impact_engine import (
    CATEGORY_NEW_BUSINESS,
    CATEGORY_UNCERTAIN,
    MIN_COMPANY_MATCH_CONFIDENCE,
    NEWS_MAX_SCORE_CONTRIBUTION,
    assess_article,
    classify_category,
    summarize_impact,
)


def test_classify_category_keyword_match():
    assert classify_category("Şirket yeni bir ihale kazandı") == CATEGORY_NEW_BUSINESS


def test_classify_category_unknown_defaults_to_uncertain():
    assert classify_category("Bugün hava çok güzel") == CATEGORY_UNCERTAIN


def test_weak_company_match_does_not_count_toward_score():
    """Dusuk sirket eslesme guvenli haber counts_toward_score=False olmalidir
    ve bu nedenle ozet skoruna dahil edilmemelidir."""
    weak = assess_article(
        title="Şirket yeni ihale kazandı", source="reuters.com",
        company_match_confidence=20.0,  # MIN_COMPANY_MATCH_CONFIDENCE'nin altinda
        published_at=datetime.now(timezone.utc),
    )
    assert weak.counts_toward_score is False

    strong = assess_article(
        title="Şirket yeni ihale kazandı", source="reuters.com",
        company_match_confidence=90.0,
        published_at=datetime.now(timezone.utc),
    )
    assert strong.counts_toward_score is True

    summary = summarize_impact([weak, strong])
    # Yalnizca guclu eslesmeli haber agirliga katilmali.
    assert summary.article_count == 2
    assert summary.impact_score == strong.impact_score


def test_summarize_impact_all_weak_matches_returns_no_score():
    weak1 = assess_article("Şirket yeni ihale kazandı", "unknownsource.com", 10.0, datetime.now(timezone.utc))
    weak2 = assess_article("Şirket dava açıldı", "unknownsource.com", 15.0, datetime.now(timezone.utc))
    summary = summarize_impact([weak1, weak2])
    assert summary.available is True
    assert summary.impact_score is None
    assert summary.score_contribution == 0.0


def test_no_articles_returns_neutral_not_negative():
    """Haber YOKLUGU asla negatif puan olusturmamali (bolum 2 kurali)."""
    summary = summarize_impact([])
    assert summary.available is False
    assert summary.impact_score is None
    assert summary.score_contribution == 0.0


def test_score_contribution_is_always_bounded():
    """Haber etkisi tek basina, sinirsiz bir AL/SAT sinyaline donusememeli;
    skora katkisi her zaman [-3, +3] araliginda kalmalidir."""
    extreme_positive = [
        assess_article("Şirket satın alma anlaşması imzaladı", "reuters.com", 95.0, datetime.now(timezone.utc))
        for _ in range(5)
    ]
    summary = summarize_impact(extreme_positive)
    assert -NEWS_MAX_SCORE_CONTRIBUTION <= summary.score_contribution <= NEWS_MAX_SCORE_CONTRIBUTION

    extreme_negative = [
        assess_article("Şirket faaliyet durdurdu, üretim durdu", "reuters.com", 95.0, datetime.now(timezone.utc))
        for _ in range(5)
    ]
    summary_neg = summarize_impact(extreme_negative)
    assert -NEWS_MAX_SCORE_CONTRIBUTION <= summary_neg.score_contribution <= NEWS_MAX_SCORE_CONTRIBUTION
    assert summary_neg.score_contribution < 0


def test_older_news_has_less_weight_than_fresh_news():
    fresh = assess_article("Şirket yeni ihale kazandı", "reuters.com", 90.0, datetime.now(timezone.utc))
    old = assess_article(
        "Şirket yeni ihale kazandı", "reuters.com", 90.0,
        datetime.now(timezone.utc) - timedelta(days=30),
    )
    assert abs(fresh.impact_score) >= abs(old.impact_score)


def test_missing_company_match_confidence_threshold_constant_is_reasonable():
    assert 0 < MIN_COMPANY_MATCH_CONFIDENCE < 100
