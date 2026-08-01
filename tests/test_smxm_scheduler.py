from datetime import datetime
from zoneinfo import ZoneInfo

from app.config.settings import Settings
from app.telegram.bot import _build_evening_scan_scheduler


def test_smxm_scheduler_registers_istanbul_0900_and_2100_jobs():
    settings = Settings(
        morning_report_enabled=True,
        morning_report_time="09:00",
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
    assert str(morning.trigger.fields[5]) == "9"
    assert str(morning.trigger.fields[6]) == "0"
    assert str(evening.trigger.fields[5]) == "21"
    assert str(evening.trigger.fields[6]) == "0"
    assert str(morning.trigger.timezone) == "Europe/Istanbul"
    assert str(evening.trigger.timezone) == "Europe/Istanbul"
    reference = datetime(2026, 8, 2, 7, 0, tzinfo=ZoneInfo("Europe/Istanbul"))
    morning_fire = morning.trigger.get_next_fire_time(None, reference)
    evening_fire = evening.trigger.get_next_fire_time(None, reference)
    assert (morning_fire.hour, morning_fire.minute, str(morning_fire.tzinfo)) == (9, 0, "Europe/Istanbul")
    assert (evening_fire.hour, evening_fire.minute, str(evening_fire.tzinfo)) == (21, 0, "Europe/Istanbul")
    assert scheduler.get_job("daily_market_brief") is None
