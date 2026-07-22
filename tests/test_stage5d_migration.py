from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STAGE5D_TABLES = {
    "data_quality_snapshots",
    "provider_health_events",
    "provider_circuit_breakers",
    "enhanced_alarm_rules",
    "enhanced_alarm_trigger_events",
    "chart_cache_metadata",
}


def _alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["TELEGRAM_BOT_TOKEN"] = "migration-test-token"
    env["MARKET_DATA_PROVIDER"] = "mock"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_stage5d_fresh_migration_and_idempotent_rerun(tmp_path):
    from sqlalchemy import create_engine

    db_url = f"sqlite:///{tmp_path / 'fresh-stage5d.db'}"
    first = _alembic(db_url, "upgrade", "head")
    assert first.returncode == 0, first.stderr
    second = _alembic(db_url, "upgrade", "head")
    assert second.returncode == 0, second.stderr

    engine = create_engine(db_url)
    try:
        assert STAGE5D_TABLES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_stage5d_upgrade_preserves_stage5c_users_positions_signals_and_alerts(tmp_path):
    from sqlalchemy.orm import sessionmaker

    from app.models.database import (
        PortfolioPosition,
        PriceAlert,
        Signal,
        SignalStateEnum,
        SignalTypeEnum,
        User,
        build_engine,
    )

    db_url = f"sqlite:///{tmp_path / 'existing-stage5c.db'}"
    stage5c = _alembic(db_url, "upgrade", "0004_stage5_mergen_levels")
    assert stage5c.returncode == 0, stage5c.stderr

    engine = build_engine(db_url)
    session = sessionmaker(bind=engine)()
    try:
        user = User(telegram_user_id=550055, total_capital=123456.0)
        session.add(user)
        session.flush()
        session.add(PortfolioPosition(user_id=user.id, symbol="THYAO", lot=25, average_cost=250.0))
        session.add(PriceAlert(user_id=user.id, symbol="THYAO", alert_type="ust", threshold_value=300.0))
        session.add(
            Signal(
                symbol="THYAO",
                timeframe="1d",
                signal_type=SignalTypeEnum.WATCH,
                state=SignalStateEnum.CREATED,
                score=55.0,
                confidence="orta",
                strategy_version="stage5c",
                data_timestamp=datetime.now(timezone.utc),
                provider="migration-test",
                idempotency_key="stage5c-preservation-signal",
            )
        )
        session.commit()
    finally:
        session.close()
        engine.dispose()

    upgraded = _alembic(db_url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr

    engine = build_engine(db_url)
    session = sessionmaker(bind=engine)()
    try:
        assert STAGE5D_TABLES <= set(inspect(engine).get_table_names())
        user = session.query(User).filter_by(telegram_user_id=550055).one()
        assert user.total_capital == 123456.0
        assert session.query(PortfolioPosition).filter_by(user_id=user.id, symbol="THYAO").count() == 1
        assert session.query(PriceAlert).filter_by(user_id=user.id, symbol="THYAO").count() == 1
        assert session.query(Signal).filter_by(idempotency_key="stage5c-preservation-signal").count() == 1
    finally:
        session.close()
        engine.dispose()

