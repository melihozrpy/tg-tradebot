from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect

from app.models.database import (
    GroqExplanation,
    NewsArticle,
    NewsEvent,
    NewsImpactSnapshot,
    ProviderHealthLog,
    User,
    build_engine,
)
from app.utils.logging_filters import mask_sensitive_text

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["TELEGRAM_BOT_TOKEN"] = "x"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True, timeout=60, check=False,
    )


def test_stage4_tables_exist_after_fresh_migration(tmp_path):
    db_path = tmp_path / "fresh_stage4.db"
    result = _run_alembic(f"sqlite:///{db_path}", "upgrade", "head")
    assert result.returncode == 0, result.stderr

    from sqlalchemy import create_engine
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        tables = inspect(engine).get_table_names()
        for expected in (
            "news_articles", "news_events", "news_impact_snapshots",
            "groq_explanations", "provider_health_logs",
        ):
            assert expected in tables
    finally:
        engine.dispose()


def test_stage4_migration_preserves_existing_data(tmp_path):
    """Eski (V2/V3) veritabanina Asama 4 migrationu uygulandiginda mevcut
    kullanici verisi KAYBOLMAMALI."""
    db_path = tmp_path / "existing_stage4.db"
    db_url = f"sqlite:///{db_path}"

    from app.models.database import Base
    from sqlalchemy.orm import sessionmaker

    engine = build_engine(db_url)
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        session.add(User(telegram_user_id=777, total_capital=55555.0))
        session.commit()
    finally:
        session.close()
        engine.dispose()

    result = _run_alembic(db_url, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    engine2 = build_engine(db_url)
    session_factory2 = sessionmaker(bind=engine2)
    session2 = session_factory2()
    try:
        user = session2.query(User).filter(User.telegram_user_id == 777).first()
        assert user is not None
        assert user.total_capital == 55555.0

        tables = inspect(engine2).get_table_names()
        assert "news_articles" in tables
        assert "groq_explanations" in tables
    finally:
        session2.close()
        engine2.dispose()


def test_news_and_groq_models_roundtrip(db_session):
    article = NewsArticle(
        symbol="THYAO", title="Test haberi", source="reuters.com",
        url="https://reuters.com/x", company_match_confidence=90.0,
        dedup_key="deadbeef", provider="gdelt",
    )
    db_session.add(article)
    db_session.flush()

    event = NewsEvent(
        article_id=article.id, symbol="THYAO", category="finansal_sonuc",
        impact_score=10.0, confidence_score=80.0, source_confidence=80.0,
        company_match_confidence=90.0, rationale="test gerekce",
    )
    db_session.add(event)

    snapshot = NewsImpactSnapshot(symbol="THYAO", window_label="7d", article_count=1, impact_score=10.0, confidence_score=80.0)
    db_session.add(snapshot)

    explanation = GroqExplanation(symbol="THYAO", kind="teknik", cache_key="abc123", response_text="test", is_fallback=True)
    db_session.add(explanation)

    health = ProviderHealthLog(provider="gdelt", status="ok", detail="test")
    db_session.add(health)

    db_session.commit()

    assert db_session.query(NewsArticle).count() == 1
    assert db_session.query(NewsEvent).count() == 1
    assert db_session.query(NewsImpactSnapshot).count() == 1
    assert db_session.query(GroqExplanation).count() == 1
    assert db_session.query(ProviderHealthLog).count() == 1


def test_groq_api_key_masked_in_logs(caplog):
    secret = "sk-groq-" + "abcdefghijklmnop"
    message = f"Authorization: Bearer {secret}"
    masked = mask_sensitive_text(message)
    assert secret not in masked


def test_generic_api_key_label_masked():
    secret = "sk-groq-" + "abcdefghijklmnop"
    message = f"groq_api_key: {secret}"
    masked = mask_sensitive_text(message)
    assert secret not in masked
