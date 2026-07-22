from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config.settings import Settings
from app.data.gdelt_provider import GdeltUnavailableError, RawNewsArticle
from app.data.mock_provider import MockMarketDataProvider
from app.services.analysis_service_v3 import run_symbol_analysis_v3


def _v3_settings(**overrides) -> Settings:
    base = dict(telegram_bot_token="x", xu100_symbol="XU100", market_data_provider="mock", gdelt_enabled=True)
    base.update(overrides)
    return Settings(**base)


class _FakeNewsProvider:
    def __init__(self, articles=None, error: Exception | None = None):
        self._articles = articles or []
        self._error = error

    def fetch_articles(self, *args, **kwargs):
        if self._error:
            raise self._error
        return self._articles


def test_analysis_works_without_any_news_provider(db_session):
    """news_provider verilmediginde (None) analiz normal calismali, skor
    ETKILENMEMELI ve news.available False olmalidir."""
    provider = MockMarketDataProvider()
    settings = _v3_settings()
    outcome = run_symbol_analysis_v3(db_session, provider, "THYAO", settings, news_provider=None)
    assert outcome.news is not None
    assert outcome.news.available is False
    assert outcome.advanced_score.news_adjustment == 0.0


def test_analysis_works_when_no_news_found(db_session):
    """Sembol icin alias tanimliyken bile GDELT hicbir haber dondurmezse
    analiz akisi COKMEMELI ve skor DEGISMEMELIDIR."""
    provider = MockMarketDataProvider()
    settings = _v3_settings()
    news_provider = _FakeNewsProvider(articles=[])
    outcome = run_symbol_analysis_v3(db_session, provider, "THYAO", settings, news_provider=news_provider)
    assert outcome.news.available is False
    assert outcome.advanced_score.news_adjustment == 0.0


def test_analysis_continues_when_gdelt_raises(db_session):
    """GDELT saglayicisi hata verirse (zaman asimi vb.) teknik analiz DEVAM
    ETMELI; hicbir istisna disariya sizmamalidir."""
    provider = MockMarketDataProvider()
    settings = _v3_settings()
    news_provider = _FakeNewsProvider(error=GdeltUnavailableError("zaman asimi"))
    outcome = run_symbol_analysis_v3(db_session, provider, "THYAO", settings, news_provider=news_provider)
    assert outcome.news.available is False
    assert outcome.advanced_score.news_adjustment == 0.0
    assert outcome.signal is not None  # analiz basariyla tamamlandi


def test_news_alone_cannot_flip_signal_type(db_session):
    """Cok guclu POZITIF bir haber olsa dahi, DecisionEngine'in nihai karar
    sinifi haberden dolayi kendiliginden 'guclu al' seviyesine YUKSELMEZ;
    haber yalnizca mevcut karari (varsa) bir kademe temkinlilestirebilir/gozlemletir,
    tek basina AL/SAT olusturmaz."""
    provider = MockMarketDataProvider()
    settings = _v3_settings()

    now = datetime.now(timezone.utc)
    strongly_positive_articles = [
        RawNewsArticle(
            title="Şirket dev bir satın alma anlaşması imzaladı",
            source="reuters.com", url="https://reuters.com/x", published_at=now,
            language="tr", matched_alias="Turk Hava Yollari",
        )
        for _ in range(10)
    ]
    news_provider = _FakeNewsProvider(articles=strongly_positive_articles)

    outcome_without_news = run_symbol_analysis_v3(db_session, provider, "THYAO", settings, news_provider=None)
    outcome_with_news = run_symbol_analysis_v3(db_session, provider, "THYAO", settings, news_provider=news_provider)

    # Sinyal tipi (AL/SAT/TUT) haberden dolayi degismemeli; yalnizca toplam
    # skor sinirli bir miktar (en fazla +-3) degisebilir.
    assert outcome_without_news.signal.signal_type == outcome_with_news.signal.signal_type
    score_diff = outcome_with_news.advanced_score.total - outcome_without_news.advanced_score.total
    assert abs(score_diff) <= 3.0 + 0.01


def test_news_context_included_in_analysis_outcome(db_session):
    provider = MockMarketDataProvider()
    settings = _v3_settings()
    now = datetime.now(timezone.utc)
    articles = [
        RawNewsArticle(
            title="Şirket yeni bir ihale kazandı", source="reuters.com",
            url="https://reuters.com/y", published_at=now, language="tr",
            matched_alias="Turk Hava Yollari",
        )
    ]
    news_provider = _FakeNewsProvider(articles=articles)
    outcome = run_symbol_analysis_v3(db_session, provider, "THYAO", settings, news_provider=news_provider)

    assert outcome.news.available is True
    assert outcome.news.count_7d >= 1
    assert -3.0 <= outcome.news.score_contribution <= 3.0


def test_news_appended_to_short_and_detailed_messages(db_session):
    from app.services.sector_service import get_sector_info
    from app.telegram.message_templates_v3 import format_detailed_analysis, format_short_summary

    provider = MockMarketDataProvider()
    settings = _v3_settings()
    now = datetime.now(timezone.utc)
    articles = [
        RawNewsArticle(
            title="Şirket yeni bir ihale kazandı", source="reuters.com",
            url="https://reuters.com/z", published_at=now, language="tr",
            matched_alias="Turk Hava Yollari",
        )
    ]
    news_provider = _FakeNewsProvider(articles=articles)
    outcome = run_symbol_analysis_v3(db_session, provider, "THYAO", settings, news_provider=news_provider)

    short_text = format_short_summary(
        outcome.signal, "THYAO", outcome.mode, outcome.advanced_score,
        outcome.xu100_relative_strength, decision=outcome.decision, news=outcome.news,
    )
    assert "Haber" in short_text or "haber" in short_text

    sector_info = get_sector_info("THYAO")
    detailed_text = format_detailed_analysis(
        outcome.signal, "THYAO", outcome.mode, outcome.advanced_score,
        outcome.xu100_relative_strength, outcome.sector_relative_strength,
        sector_info.sector_name if sector_info else "Eslesmemis",
        outcome.intraday_quote, outcome.warnings,
        decision=outcome.decision, news=outcome.news,
    )
    assert "Haber" in detailed_text or "haber" in detailed_text


def test_news_not_appended_when_unavailable(db_session):
    """Haber yoksa mesajlara haber blogu hic EKLENMEMELI (gereksiz gurultu olmamali)."""
    from app.telegram.message_templates_v3 import format_short_summary

    provider = MockMarketDataProvider()
    settings = _v3_settings()
    outcome = run_symbol_analysis_v3(db_session, provider, "THYAO", settings, news_provider=None)

    short_text = format_short_summary(
        outcome.signal, "THYAO", outcome.mode, outcome.advanced_score,
        outcome.xu100_relative_strength, decision=outcome.decision, news=outcome.news,
    )
    assert "📰" not in short_text
