from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.analysis.target_roadmap_engine import build_target_roadmap
from app.models.database import Base, TargetTrackingRecord
from app.services.target_tracking_service import (
    STATUS_INVALID,
    STATUS_PARTIAL,
    STATUS_REACHED,
    compute_target_performance,
    persist_roadmap_steps,
    save_target_tracking,
    update_target_records,
)


ROOT = Path(__file__).resolve().parent.parent
TABLES = {
    "long_term_scenarios", "user_price_targets", "target_roadmap_steps",
    "valuation_snapshots", "corporate_action_events", "target_realism_snapshots",
    "target_tracking_records", "target_performance_summaries",
}


def _alembic(url: str, *args: str):
    env = os.environ.copy()
    env.update({"DATABASE_URL": url, "MARKET_DATA_PROVIDER": "mock", "TELEGRAM_BOT_TOKEN": "test"})
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
        text=True, capture_output=True, timeout=60, check=False,
    )


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _save(db, **overrides):
    data = {
        "symbol": "SVGYO", "current_price": 10, "target_low": 14,
        "target_high": 15, "target_type": "Uzun vadeli ana hedef",
        "time_horizon": "Uzun", "confidence": 65,
        "technical_reasons": ["direnç"], "fundamental_status": "Veri yetersiz",
        "invalidation_level": 8, "data_timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    data.update(overrides)
    return save_target_tracking(db, **data)


def test_stage5e_fresh_migration_and_idempotency(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    assert _alembic(url, "upgrade", "head").returncode == 0
    assert _alembic(url, "upgrade", "head").returncode == 0
    engine = create_engine(url)
    assert TABLES <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_stage5e_upgrade_preserves_existing_user_data(tmp_path):
    url = f"sqlite:///{tmp_path / 'existing.db'}"
    assert _alembic(url, "upgrade", "0005_stage5d_reliability_alerts_charts").returncode == 0
    from app.models.database import User
    engine = create_engine(url)
    db = sessionmaker(bind=engine)()
    db.add(User(telegram_user_id=987654, total_capital=12345))
    db.commit(); db.close(); engine.dispose()
    result = _alembic(url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    engine = create_engine(url); db = sessionmaker(bind=engine)()
    assert db.query(User).filter_by(telegram_user_id=987654).one().total_capital == 12345
    db.close(); engine.dispose()


def test_target_record_is_created():
    db = _session()
    row, created = _save(db)
    assert created is True
    assert row.status == "Aktif"


def test_duplicate_target_record_is_prevented():
    db = _session()
    first, first_created = _save(db)
    second, second_created = _save(db)
    assert first_created is True and second_created is False
    assert first.id == second.id
    assert db.query(TargetTrackingRecord).count() == 1


def test_target_reach_is_recorded():
    db = _session(); row, _ = _save(db)
    update_target_records(
        db, "SVGYO", bar_high=15.2, bar_low=10, bar_close=15,
        timestamp=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    assert db.get(TargetTrackingRecord, row.id).status == STATUS_REACHED


def test_partial_and_invalidation_states_are_recorded():
    db = _session(); row, _ = _save(db)
    update_target_records(
        db, "SVGYO", bar_high=14.5, bar_low=9, bar_close=14,
        timestamp=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )
    assert db.get(TargetTrackingRecord, row.id).status == STATUS_PARTIAL
    update_target_records(
        db, "SVGYO", bar_high=12, bar_low=7.5, bar_close=8,
        timestamp=datetime(2026, 1, 16, tzinfo=timezone.utc),
    )
    assert db.get(TargetTrackingRecord, row.id).status == STATUS_INVALID


def test_target_success_rate_is_calculated_correctly():
    db = _session()
    _save(db, target_low=14, target_high=15, target_type="H1")
    _save(db, target_low=18, target_high=19, target_type="H2")
    update_target_records(
        db, "SVGYO", bar_high=15.5, bar_low=9, bar_close=15,
        timestamp=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    report = compute_target_performance(db, "SVGYO")
    assert report.total_targets == 2
    assert report.reached_targets == 1
    assert report.success_rate == 50.0


def test_roadmap_steps_are_persisted_without_duplicates():
    db = _session(); row, _ = _save(db)
    roadmap = build_target_roadmap(10, 20, intermediate_levels=[12, 15, 18])
    first = persist_roadmap_steps(db, row, roadmap)
    second = persist_roadmap_steps(db, row, roadmap)
    assert len(first) == len(roadmap.steps)
    assert second == []
