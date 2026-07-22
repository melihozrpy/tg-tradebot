from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_alembic(
    database_url: str,
    *args: str,
) -> subprocess.CompletedProcess[str]:
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


def test_migration_upgrades_fresh_database(tmp_path):
    db_path = tmp_path / "fresh.db"

    result = _run_alembic(
        f"sqlite:///{db_path}",
        "upgrade",
        "head",
    )

    assert result.returncode == 0, result.stderr

    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{db_path}")

    try:
        inspector = inspect(engine)
        tables = inspector.get_table_names()

        assert "signals" in tables
        assert "scans" in tables
        assert "user_settings" in tables
        assert "alembic_version" in tables
    finally:
        engine.dispose()


def test_migration_preserves_existing_v2_data(tmp_path):
    db_path = tmp_path / "v2_existing.db"
    db_url = f"sqlite:///{db_path}"

    # V2 senaryosu:
    # Alembic kaydı olmadan eski veritabanını oluştur ve örnek veri ekle.
    from app.models.database import Base, User, build_engine
    from sqlalchemy.orm import sessionmaker

    engine = build_engine(db_url)
    Base.metadata.create_all(bind=engine)

    session_factory = sessionmaker(bind=engine)
    session = session_factory()

    try:
        session.add(
            User(
                telegram_user_id=4242,
                total_capital=99999.0,
            )
        )
        session.commit()
    finally:
        session.close()
        engine.dispose()

    # Mevcut V2 veritabanına V3 migration'larını uygula.
    result = _run_alembic(
        db_url,
        "upgrade",
        "head",
    )

    assert result.returncode == 0, result.stderr

    # Eski verinin korunduğunu ve yeni V3 tablolarının eklendiğini doğrula.
    engine2 = build_engine(db_url)
    session_factory2 = sessionmaker(bind=engine2)
    session2 = session_factory2()

    try:
        user = (
            session2.query(User)
            .filter(User.telegram_user_id == 4242)
            .first()
        )

        assert user is not None
        assert user.total_capital == 99999.0

        inspector = inspect(engine2)
        tables = inspector.get_table_names()

        assert "scans" in tables
        assert "alembic_version" in tables
    finally:
        session2.close()
        engine2.dispose()