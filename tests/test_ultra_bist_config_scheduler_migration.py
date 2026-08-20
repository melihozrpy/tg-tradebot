from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config.settings import Settings, get_settings
from app.telegram.bot import _build_evening_scan_scheduler, build_telegram_application


PROJECT_ROOT = Path(__file__).resolve().parent.parent
NEW_TABLES = {
    "signal_targets",
    "signal_event_deliveries",
    "signal_transition_error_audits",
    "market_session_events",
    "user_price_alerts",
    "price_alert_triggers",
    "price_alert_deliveries",
    "alarm_import_jobs",
    "alarm_import_rows",
    "user_alarm_settings",
    "ema_cross_state",
    "rsi_alert_state",
    "news_cache",
    "staged_entry_plans",
    "staged_entry_events",
    "scheduled_message_deliveries",
    "scheduled_trade_ideas",
    "kap_monitor_events",
}


def _upgrade(database: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{database.as_posix()}",
            "TELEGRAM_BOT_TOKEN": "migration-test-token",
            "MARKET_DATA_PROVIDER": "mock",
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )


def test_ultra_bist_settings_defaults_and_optional_blank_value():
    settings = Settings(_env_file=None, max_market_data_staleness_seconds="")
    assert settings.long_only is True
    assert settings.signal_monitor_enabled is True
    assert settings.allow_delayed_data_for_live_trigger is False
    assert settings.max_market_data_staleness_seconds is None
    assert (
        Settings(_env_file=None, technical_screener_chat_id="").technical_screener_chat_id
        is None
    )
    assert (
        settings.default_tp1_allocation
        + settings.default_tp2_allocation
        + settings.default_tp3_allocation
        == 100
    )
    assert settings.user_price_alerts_enabled is True
    assert settings.user_price_alert_default_repeat_seconds >= settings.user_price_alert_min_repeat_seconds


def test_ultra_bist_settings_fail_fast_for_unsafe_combinations():
    with pytest.raises(ValidationError, match="toplami tam olarak 100"):
        Settings(_env_file=None, default_tp3_allocation=20)
    with pytest.raises(ValidationError, match="MIN_REPEAT_SECONDS"):
        Settings(
            _env_file=None,
            user_price_alert_default_repeat_seconds=30,
            user_price_alert_min_repeat_seconds=60,
        )
    with pytest.raises(ValidationError, match="MAX_MARKET_DATA_STALENESS_SECONDS"):
        Settings(_env_file=None, max_market_data_staleness_seconds=0)


def test_fundamental_provider_names_are_normalized_and_validated():
    defaults = Settings(_env_file=None)
    assert defaults.fundamental_provider == "auto"
    assert defaults.fundamental_allow_yahoo_fallback is True
    assert Settings(_env_file=None, fundamental_provider="FiNtAbLeS_McP").fundamental_provider == "fintables_mcp"
    assert Settings(_env_file=None, market_data_provider="LICENSED_REST").market_data_provider == "licensed_rest"
    with pytest.raises(ValidationError, match="FUNDAMENTAL_PROVIDER"):
        Settings(_env_file=None, fundamental_provider="web_scrape")


def test_close_scan_false_keeps_price_alarm_monitor_and_delivery_jobs():
    settings = SimpleNamespace(
        close_scan_enabled=False,
        close_scan_time="invalid",
        timezone_name="Europe/Istanbul",
        conservative_execution=True,
        signal_expiry_trading_days=10,
        intraday_anomaly_scan_enabled=False,
        user_price_alerts_enabled=True,
        user_price_alert_poll_seconds=17,
        user_price_alert_delivery_poll_seconds=3,
    )
    scheduler = _build_evening_scan_scheduler(settings)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert "evening_close_scan" not in jobs
    assert {"user_price_alert_monitor", "user_price_alert_delivery"} <= jobs.keys()
    assert int(jobs["user_price_alert_monitor"].trigger.interval.total_seconds()) == 17
    assert int(jobs["user_price_alert_delivery"].trigger.interval.total_seconds()) == 3


def test_signal_monitor_and_delivery_are_independent_scheduler_jobs():
    settings = SimpleNamespace(
        close_scan_enabled=False,
        close_scan_time="invalid",
        timezone_name="Europe/Istanbul",
        conservative_execution=True,
        signal_expiry_trading_days=10,
        intraday_anomaly_scan_enabled=False,
        user_price_alerts_enabled=False,
        signal_monitor_enabled=True,
        signal_monitor_interval_seconds=7,
    )
    scheduler = _build_evening_scan_scheduler(settings)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert {"bist_signal_monitor", "bist_signal_delivery"} <= jobs.keys()
    assert int(jobs["bist_signal_monitor"].trigger.interval.total_seconds()) == 7


def test_new_alarm_handlers_replace_conflicting_legacy_registrations():
    get_settings.cache_clear()
    try:
        application = build_telegram_application()
    finally:
        get_settings.cache_clear()
    counts: dict[str, int] = {}
    for group in application.handlers.values():
        for handler in group:
            for command in getattr(handler, "commands", set()) or set():
                counts[command] = counts.get(command, 0) + 1
    for command in ("alarm_kur", "alarmlar", "alarm_sil", "alarm_durdur", "alarm_detay"):
        assert counts[command] == 1
    assert {"toplu_alarm", "alarm_foto", "alarm_dosya", "alarm_yardim"} <= counts.keys()


def test_0008_fresh_database_migration_is_complete_and_idempotent(tmp_path):
    database = tmp_path / "fresh-0008.db"
    first = _upgrade(database)
    assert first.returncode == 0, first.stderr
    second = _upgrade(database)
    assert second.returncode == 0, second.stderr
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
        revision = connection.execute("select version_num from alembic_version").fetchone()[0]
        signal_columns = {row[1] for row in connection.execute("pragma table_info(signals)")}
    assert NEW_TABLES <= tables
    assert revision == "0012_scheduled_ideas_kap_monitor"
    assert {"planned_entry_price", "current_stop_price", "row_version", "source"} <= signal_columns
    with sqlite3.connect(database) as connection:
        user_types = {
            row[1]: row[2].upper()
            for row in connection.execute("pragma table_info(users)")
        }
        delivery_types = {
            row[1]: row[2].upper()
            for row in connection.execute("pragma table_info(signal_event_deliveries)")
        }
        delivery_indexes = {
            row[1] for row in connection.execute("pragma index_list(signal_event_deliveries)")
        }
    assert user_types["telegram_user_id"] == "BIGINT"
    assert delivery_types["telegram_user_id"] == "BIGINT"
    assert delivery_types["chat_id"] == "BIGINT"
    assert "ix_signal_event_delivery_recovery" in delivery_indexes


def test_0008_upgrades_legacy_schema_additively_and_preserves_signal(tmp_path):
    database = tmp_path / "legacy-0007.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            create table alembic_version (version_num varchar(64) not null primary key);
            insert into alembic_version values ('0007_stage5g_backtest_paper_validation');
            create table users (id integer primary key);
            insert into users (id) values (1);
            create table signals (id integer primary key, symbol varchar(16) not null);
            insert into signals (id, symbol) values (7, 'THYAO');
            create table signal_events (
                id integer primary key,
                signal_id integer not null references signals(id)
            );
            """
        )
        connection.commit()

    upgraded = _upgrade(database)
    assert upgraded.returncode == 0, upgraded.stderr
    with sqlite3.connect(database) as connection:
        signal_columns = {row[1] for row in connection.execute("pragma table_info(signals)")}
        event_columns = {row[1] for row in connection.execute("pragma table_info(signal_events)")}
        preserved = connection.execute("select symbol from signals where id=7").fetchone()[0]
        side, row_version = connection.execute(
            "select side, row_version from signals where id=7"
        ).fetchone()
        tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
    assert preserved == "THYAO"
    assert (side, row_version) == ("BUY", 1)
    assert {"user_id", "actual_entry_price", "current_stop_price", "row_version", "source"} <= signal_columns
    assert {"event_type", "execution_price", "unique_dedup_key"} <= event_columns
    assert NEW_TABLES <= tables
