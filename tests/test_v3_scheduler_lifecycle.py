from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram.ext import Application

from app.telegram.bot import _build_evening_scan_scheduler, _register_scheduler_lifecycle


def _fake_settings(close_scan_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        close_scan_enabled=close_scan_enabled,
        close_scan_time="18:20",
        timezone_name="Europe/Istanbul",
        conservative_execution=True,
        signal_expiry_trading_days=10,
    )


def _build_app() -> Application:
    return Application.builder().token("test-token").build()


def test_close_scan_disabled_keeps_independent_scheduler_jobs():
    settings = _fake_settings(close_scan_enabled=False)
    scheduler = _build_evening_scan_scheduler(settings)
    assert scheduler is not None
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert "evening_close_scan" not in job_ids
    assert {"user_price_alert_monitor", "user_price_alert_delivery"} <= job_ids


def test_scheduler_includes_intraday_anomaly_job_by_default():
    """V3.2 (Asama 3): ayni scheduler uzerinde hem aksam taramasi hem de gun
    ici anomali taramasi jobu bulunmali (eski test konfigurasyonlari bu yeni
    ayari icermese bile getattr varsayilani ile calismali)."""
    settings = _fake_settings(close_scan_enabled=True)
    scheduler = _build_evening_scan_scheduler(settings)
    assert scheduler is not None
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert {
        "evening_close_scan",
        "user_price_alert_monitor",
        "user_price_alert_delivery",
    } <= job_ids
    assert len(scheduler.get_jobs()) == 4


def test_scheduler_is_not_started_before_event_loop_runs():
    """Scheduler nesnesi olusturulurken (event loop yokken) start() cagrilmamali;
    aksi halde 'RuntimeError: no running event loop' hatasi olusur."""
    application = _build_app()
    settings = _fake_settings(close_scan_enabled=True)
    _register_scheduler_lifecycle(application, settings)

    scheduler = application.bot_data["scheduler"]
    assert scheduler is not None
    assert application.bot_data["scheduler_started"] is False
    assert scheduler.running is False


def test_scheduler_starts_once_event_loop_is_running():
    """post_init callback'i calisan bir event loop icinde scheduler.start()
    cagirabilmeli, herhangi bir RuntimeError firlatmamali."""
    application = _build_app()
    settings = _fake_settings(close_scan_enabled=True)
    _register_scheduler_lifecycle(application, settings)

    async def _run():
        assert application.post_init is not None
        assert application.post_shutdown is not None
        await application.post_init(application)
        assert application.bot_data["scheduler_started"] is True
        assert application.bot_data["scheduler"].running is True

        # Ikinci cagri scheduler'i tekrar baslatmamali (idempotent olmali).
        await application.post_init(application)
        assert application.bot_data["scheduler_started"] is True

        await application.post_shutdown(application)
        assert application.bot_data["scheduler_started"] is False

    asyncio.run(_run())


def test_scheduler_lifecycle_is_registered_when_only_close_scan_is_disabled():
    application = _build_app()
    settings = _fake_settings(close_scan_enabled=False)
    _register_scheduler_lifecycle(application, settings)

    scheduler = application.bot_data["scheduler"]
    assert scheduler is not None
    assert "user_price_alert_monitor" in {job.id for job in scheduler.get_jobs()}
    assert application.post_init is not None
    assert application.post_shutdown is not None
