from app.config.settings import Settings
from app.telegram.bot import _build_evening_scan_scheduler


def test_smxm_scheduler_registers_istanbul_0800_and_2100_jobs():
    settings = Settings(
        morning_report_enabled=True,
        morning_report_time="08:00",
        evening_market_report_enabled=True,
        evening_market_report_time="21:00",
        close_scan_enabled=False,
        daily_brief_enabled=True,
        enhanced_alarm_scan_enabled=False,
        signal_monitor_enabled=False,
        user_price_alerts_enabled=False,
    )
    scheduler = _build_evening_scan_scheduler(settings, application=None)
    morning = scheduler.get_job("smxm_morning_report")
    evening = scheduler.get_job("smxm_evening_report")
    assert morning is not None
    assert evening is not None
    assert str(morning.trigger.fields[5]) == "8"
    assert str(morning.trigger.fields[6]) == "0"
    assert str(evening.trigger.fields[5]) == "21"
    assert str(evening.trigger.fields[6]) == "0"
    assert scheduler.get_job("daily_market_brief") is None
