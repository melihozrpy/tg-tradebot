from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config.settings import Settings, load_company_aliases
from app.data.gdelt_provider import (
    GdeltNewsProvider,
    GdeltUnavailableError,
    RawNewsArticle,
    compute_dedup_key,
)
from app.services.news_service import (
    get_company_alias_info,
    scan_symbol_news,
)


def test_company_alias_lookup_known_symbol():
    info = get_company_alias_info("THYAO")
    assert info is not None
    assert info["company_name"] == "Turk Hava Yollari"
    assert "Turkish Airlines" in info["aliases"]


def test_company_alias_lookup_unknown_symbol_returns_none():
    """Sembol icin alias kaydi yoksa None doner; ASLA uydurma isim uretilmez."""
    info = get_company_alias_info("XYZFAKE")
    assert info is None


def test_load_company_aliases_missing_file_returns_empty(tmp_path):
    data = load_company_aliases(str(tmp_path / "does_not_exist.yaml"))
    assert data == {"symbols": {}}


def test_scan_symbol_news_skips_gdelt_when_no_alias(db_session):
    """Sirket eslestirmesi olmayan bir sembol icin GDELT'e hicbir istek atilmaz."""
    settings = Settings(gdelt_enabled=True)

    class ExplodingProvider:
        def fetch_articles(self, *args, **kwargs):
            raise AssertionError("GDELT'e istek atilmamali (alias yok)")

    outcome = scan_symbol_news(db_session, "XYZFAKE", ExplodingProvider(), settings)
    assert outcome.gdelt_error is None
    assert outcome.new_articles == []


def test_scan_symbol_news_disabled_by_settings(db_session):
    settings = Settings(gdelt_enabled=False)

    class ExplodingProvider:
        def fetch_articles(self, *args, **kwargs):
            raise AssertionError("GDELT_ENABLED=false iken istek atilmamali")

    outcome = scan_symbol_news(db_session, "THYAO", ExplodingProvider(), settings)
    assert outcome.gdelt_error is None
    assert outcome.new_articles == []


def _mock_gdelt_transport(articles_payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"articles": articles_payload})
    return httpx.MockTransport(handler)


def test_gdelt_provider_parses_and_dedupes_across_sources():
    """Ayni haberin farkli kaynaklardaki KOPYALARI tek bir satirda birlestirilir."""
    now = datetime.now(timezone.utc)
    seendate = now.strftime("%Y%m%dT%H%M%SZ")
    payload = [
        {"title": "THY yeni ucak siparisi verdi", "domain": "reuters.com", "url": "https://reuters.com/a", "seendate": seendate, "language": "Turkish"},
        {"title": "THY yeni uçak siparişi verdi!", "domain": "dunya.com", "url": "https://dunya.com/b", "seendate": seendate, "language": "Turkish"},
    ]
    transport = _mock_gdelt_transport(payload)
    client = httpx.Client(transport=transport)
    provider = GdeltNewsProvider(client=client, cache_ttl_minutes=30)

    raw_articles = provider.fetch_articles("THYAO", "Turk Hava Yollari", ["Turkish Airlines", "THY"])
    assert len(raw_articles) == 2  # ham veri seviyesinde henuz iki ayri kayit

    key1 = compute_dedup_key(raw_articles[0].title, raw_articles[0].published_at)
    key2 = compute_dedup_key(raw_articles[1].title, raw_articles[1].published_at)
    # Normallestirme (kucuk harf + noktalama temizligi) sayesinde AYNI gunun
    # AYNI haberi icin dedup anahtari ayni olmalidir.
    assert key1 == key2


def test_persist_articles_merges_duplicate_source_count(db_session):
    from app.models.database import NewsArticle

    now = datetime.now(timezone.utc)
    raw_articles = [
        RawNewsArticle(title="THY yeni ucak siparisi verdi", source="reuters.com", url="https://reuters.com/a", published_at=now, language="tr", matched_alias="THY"),
        RawNewsArticle(title="THY yeni uçak siparişi verdi!", source="dunya.com", url="https://dunya.com/b", published_at=now, language="tr", matched_alias="THY"),
    ]

    class FakeProvider:
        def fetch_articles(self, *args, **kwargs):
            return raw_articles

    settings = Settings(gdelt_enabled=True)
    outcome = scan_symbol_news(db_session, "THYAO", FakeProvider(), settings)

    assert len(outcome.new_articles) == 1
    assert outcome.duplicate_count == 1

    stored = db_session.query(NewsArticle).filter(NewsArticle.symbol == "THYAO").all()
    assert len(stored) == 1
    assert stored[0].duplicate_source_count == 2


def test_gdelt_unavailable_after_retries_raises_and_does_not_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    provider = GdeltNewsProvider(client=client, max_retries=1, min_request_interval_seconds=0)

    with pytest.raises(GdeltUnavailableError):
        provider.fetch_articles("THYAO", "Turk Hava Yollari", ["THY"])


def test_scan_symbol_news_continues_when_gdelt_fails(db_session):
    """GDELT hata verse bile scan_symbol_news istisna FIRLATMAZ; hata bilgisini dondurur."""
    class FailingProvider:
        def fetch_articles(self, *args, **kwargs):
            raise GdeltUnavailableError("zaman asimi")

    settings = Settings(gdelt_enabled=True)
    outcome = scan_symbol_news(db_session, "THYAO", FailingProvider(), settings)
    assert outcome.gdelt_error is not None
    assert outcome.new_articles == []
