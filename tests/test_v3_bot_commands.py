from __future__ import annotations

from app.telegram.bot import build_telegram_application


def _all_registered_commands(application) -> set[str]:
    commands: set[str] = set()
    for group_handlers in application.handlers.values():
        for handler in group_handlers:
            cmds = getattr(handler, "commands", None)
            if cmds:
                commands.update(cmds)
    return commands


def test_new_v3_1_commands_are_registered():
    application = build_telegram_application()
    commands = _all_registered_commands(application)
    for expected in ("gunici", "zaman_dilimleri", "likidite"):
        assert expected in commands, f"/{expected} komutu bot'a kayitli degil"


def test_stage3_anomaly_commands_are_registered():
    application = build_telegram_application()
    commands = _all_registered_commands(application)
    for expected in ("anomali", "anomaliler"):
        assert expected in commands, f"/{expected} komutu bot'a kayitli degil"


def test_existing_v3_commands_still_registered():
    """Yeni komutlar eklenirken mevcut komutlar kaldirilmamis olmali."""
    application = build_telegram_application()
    commands = _all_registered_commands(application)
    for expected in ("analiz", "analiz_detay", "tara", "sinyaller", "performans"):
        assert expected in commands


def test_stage5_seviyeler_command_is_registered():
    application = build_telegram_application()
    commands = _all_registered_commands(application)
    assert "seviyeler" in commands


def test_simple_alarm_company_and_kap_commands_are_registered():
    application = build_telegram_application()
    commands = _all_registered_commands(application)
    assert {
        "alarm", "alarm_test", "sirket", "kap", "komutlar", "basitalsat", "viop", "viopislem",
        "borsacopilot", "viopcopilot", "varant",
    }.issubset(commands)


def test_ultra_signal_lifecycle_commands_are_registered_once():
    application = build_telegram_application()
    commands = _all_registered_commands(application)
    assert {
        "takip",
        "takip_birak",
        "sinyal_iptal",
        "stop_girise",
        "pozisyon_kapat",
        "aktif_pozisyonlar",
    }.issubset(commands)
