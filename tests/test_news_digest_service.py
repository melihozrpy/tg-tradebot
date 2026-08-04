from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config.settings import Settings
from app.data.gdelt_provider import RawNewsArticle
from app.models.database import Base
from app.services.news_digest_service import build_news_digest, format_news_digest


class _Gdelt:
    def __init__(self):
        self.calls = 0

    def fetch_articles(self, symbol, company_name, aliases, lookback_days=7, max_records=50):
        self.calls += 1
        return [
            RawNewsArticle(
                title="Türk Hava Yolları yeni uçuş hattını duyurdu",
                source="ornekhaber.test",
                url="https://ornekhaber.test/thy-yeni-hat",
                published_at=datetime.now(timezone.utc),
                language="Turkish",
                matched_alias="Turk Hava Yollari",
            )
        ]


class _Kap:
    def get_latest_disclosures(self, symbol):
        return [
            {
                "title": "Özel durum açıklaması",
                "published_at": datetime.now(timezone.utc),
                "source_url": "https://www.kap.org.tr/tr/Bildirim/1",
            }
        ]


def test_news_command_digest_uses_db_cache_and_source_links() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    gdelt = _Gdelt()
    settings = Settings(
        gdelt_enabled=True,
        groq_enabled=False,
        news_digest_cache_minutes=15,
        news_digest_lookback_hours=48,
        news_scrape_urls="",
    )
    try:
        first = build_news_digest(
            db,
            symbol="THYAO",
            settings=settings,
            gdelt_provider=gdelt,
            kap_provider=_Kap(),
        )
        second = build_news_digest(
            db,
            symbol="THYAO",
            settings=settings,
            gdelt_provider=gdelt,
            kap_provider=_Kap(),
        )
        text = format_news_digest(second)
        assert gdelt.calls == 1
        assert first.from_cache is False
        assert second.from_cache is True
        assert "https://ornekhaber.test/thy-yeni-hat" in text
        assert "https://www.kap.org.tr/tr/Bildirim/1" in text
        assert "15 dk cache" in text
    finally:
        db.close()
