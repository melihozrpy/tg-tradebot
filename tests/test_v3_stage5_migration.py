from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_STAGE5_TABLES = [
    "timeframe_levels",
    "confluence_zones",
    "price_scenarios",
    "breakout_scenarios",
    "relative_strength_periods",
    "enhanced_alert_events",
]


def _run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["TELEGRAM_BOT_TOKEN"] = "x"

    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_stage5_migration_creates_new_tables_on_fresh_db(tmp_path):
    db_path = tmp_path / "fresh_stage5.db"

    result = _run_alembic(f"sqlite:///{db_path}", "upgrade", "head")
    assert result.returncode == 0, result.stderr

    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        tables = inspect(engine).get_table_names()
        for table_name in _STAGE5_TABLES:
            assert table_name in tables, f"eksik tablo: {table_name}"
    finally:
        engine.dispose()


def test_stage5_migration_preserves_existing_stage4_data(tmp_path):
    """Stage 4 seviyesinde (0003'e kadar) mevcut bir veritabanina Stage 5
    migration'i uygulaninca eski kullanici/portfoy/sinyal verisi kaybolmaz
    ve yeni tablolar eklenir."""
    db_path = tmp_path / "stage4_existing.db"
    db_url = f"sqlite:///{db_path}"

    result = _run_alembic(db_url, "upgrade", "0003_stage4_news_ai")
    assert result.returncode == 0, result.stderr

    from sqlalchemy.orm import sessionmaker

    from app.models.database import PortfolioPosition, User, build_engine

    engine = build_engine(db_url)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        user = User(telegram_user_id=777, total_capital=50000.0)
        session.add(user)
        session.commit()
        session.add(
            PortfolioPosition(
                user_id=user.id,
                symbol="THYAO",
                lot=100,
                average_cost=300.0,
            )
        )
        session.commit()
    finally:
        session.close()
        engine.dispose()

    result = _run_alembic(db_url, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    engine2 = build_engine(db_url)
    try:
        tables = inspect(engine2).get_table_names()
        for table_name in _STAGE5_TABLES:
            assert table_name in tables

        session_factory2 = sessionmaker(bind=engine2)
        session2 = session_factory2()
        try:
            user = session2.query(User).filter(User.telegram_user_id == 777).first()
            assert user is not None
            assert user.total_capital == 50000.0

            position = session2.query(PortfolioPosition).filter(PortfolioPosition.user_id == user.id).first()
            assert position is not None
            assert position.symbol == "THYAO"
        finally:
            session2.close()
    finally:
        engine2.dispose()


def test_stage5_migration_idempotent_on_rerun(tmp_path):
    """Migration tekrar calistirildiginda veritabanini bozmaz."""
    db_path = tmp_path / "rerun.db"
    db_url = f"sqlite:///{db_path}"

    result1 = _run_alembic(db_url, "upgrade", "head")
    assert result1.returncode == 0, result1.stderr

    result2 = _run_alembic(db_url, "upgrade", "head")
    assert result2.returncode == 0, result2.stderr

    from sqlalchemy import create_engine

    engine = create_engine(db_url)
    try:
        tables = inspect(engine).get_table_names()
        for table_name in _STAGE5_TABLES:
            assert table_name in tables
    finally:
        engine.dispose()
