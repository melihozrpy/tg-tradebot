from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.backtest.engine_v5g import BacktestConfig, BacktestEngine, SignalInstruction, TransactionCostConfig
from app.main import app
from app.models.database import BacktestRun, Base, User, build_engine
from app.services.backtest_chart_service import delete_backtest_chart, generate_backtest_charts
from app.services.backtest_job_service import BacktestJobService
from app.telegram.bot import _build_evening_scan_scheduler, build_telegram_application


REQUIRED_TABLES = {
    "backtest_windows", "backtest_daily_equity", "backtest_metrics", "paper_accounts",
    "paper_trade_events", "signal_outcomes", "signal_feature_snapshots",
    "score_calibration_models", "score_calibration_bins", "signal_score_contributions",
    "validation_reports",
    "virtual_portfolios", "virtual_trades", "market_daily_report_logs",
}


def _alembic(target, revision):
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{target.as_posix()}"
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=os.path.abspath("."), env=env, capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.fixture(scope="module")
def migrated_databases(tmp_path_factory):
    root = tmp_path_factory.mktemp("stage5g_migrations")
    fresh = root / "fresh.db"
    existing = root / "existing.db"
    _alembic(fresh, "head")
    _alembic(existing, "0006_stage5e_long_term_targets_valuation")
    with sqlite3.connect(existing) as connection:
        connection.execute("insert into users (telegram_user_id,is_admin,total_capital,risk_per_trade_percent,maximum_daily_loss_percent,maximum_open_positions,kill_switch_active,created_at,updated_at) values (?,?,?,?,?,?,?,?,?)", (
            991122, 0, 100000, 0.75, 2, 5, 0, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(),
        ))
        connection.commit()
    _alembic(existing, "head")
    return fresh, existing


def test_55_fresh_migration_reaches_stage5g_head(migrated_databases):
    fresh, _ = migrated_databases
    with sqlite3.connect(fresh) as connection:
        names = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
        version = connection.execute("select version_num from alembic_version").fetchone()[0]
    assert REQUIRED_TABLES.issubset(names)
    assert version == "0009_smxm_reports_virtual_portfolios"


def test_56_existing_database_migration_reaches_stage5g_head(migrated_databases):
    _, existing = migrated_databases
    with sqlite3.connect(existing) as connection:
        version = connection.execute("select version_num from alembic_version").fetchone()[0]
    assert version == "0009_smxm_reports_virtual_portfolios"


def test_57_existing_user_record_is_preserved_by_migration(migrated_databases):
    _, existing = migrated_databases
    with sqlite3.connect(existing) as connection:
        count = connection.execute("select count(*) from users where telegram_user_id=991122").fetchone()[0]
    assert count == 1


def test_58_fastapi_smoke_passes():
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.json()["name"] == "MONTANA FİNANS ROBOTU"


def test_59_stage5g_telegram_commands_are_registered():
    application = build_telegram_application()
    commands = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            commands.update(getattr(handler, "commands", set()) or set())
    expected = {"backtest", "backtest_ozet", "sanal_portfoy", "sanal_performans", "sinyalbasari", "kalibrasyon", "neden"}
    assert expected.issubset(commands)


def test_60_scheduler_contains_paper_and_signal_tracking_job():
    settings = SimpleNamespace(
        close_scan_enabled=True, close_scan_time="18:20", timezone_name="Europe/Istanbul",
        conservative_execution=True, signal_expiry_trading_days=10,
        paper_trading_scan_minutes=15,
    )
    scheduler = _build_evening_scan_scheduler(settings)
    assert scheduler is not None
    assert "stage5g_paper_and_signal_tracking" in {job.id for job in scheduler.get_jobs()}


@pytest.mark.asyncio
async def test_61_background_backtest_does_not_block_event_loop(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory(); user = User(telegram_user_id=123123); db.add(user); db.commit(); db.refresh(user); user_id = user.id; db.close()
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    bars = pd.DataFrame([
        {"timestamp": start + timedelta(days=i), "open": 100, "high": 101, "low": 99, "close": 100,
         "volume": 1000, "is_complete": True, "data_quality": "VALID", "price_mode": "adjusted"}
        for i in range(6)
    ])
    def loader():
        time.sleep(0.2)
        return bars
    service = BacktestJobService(factory, timeout_seconds=10)
    before = time.perf_counter()
    run_id = await service.start(
        user_id=user_id, symbol="THYAO", timeframe="1d", start_date=start,
        end_date=start + timedelta(days=6), bars_loader=loader,
        signal_provider=lambda _: SignalInstruction(),
        config=BacktestConfig(minimum_history_bars=1, transaction_costs=TransactionCostConfig(0, 0, 0, 0, 0)),
    )
    elapsed = time.perf_counter() - before
    assert elapsed < 0.15
    await service.wait(run_id)
    db = factory(); record = db.query(BacktestRun).filter_by(run_id=run_id).one(); db.close()
    assert record.run_status == "COMPLETED"


def test_62_chart_generation_and_temporary_cleanup_work():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    bars = pd.DataFrame([
        {"timestamp": start + timedelta(days=i), "open": 100 + i, "high": 101 + i,
         "low": 99 + i, "close": 100 + i, "volume": 1000,
         "is_complete": True, "data_quality": "VALID", "price_mode": "adjusted"}
        for i in range(40)
    ])
    result = BacktestEngine(BacktestConfig(
        minimum_history_bars=1, transaction_costs=TransactionCostConfig(0, 0, 0, 0, 0),
    )).run(bars, "THYAO", lambda _: SignalInstruction())
    paths = generate_backtest_charts(result)
    assert paths and all(path.exists() and path.stat().st_size > 0 for path in paths)
    assert all(delete_backtest_chart(path) for path in paths)
    assert all(not path.exists() for path in paths)
