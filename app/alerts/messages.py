from __future__ import annotations

from decimal import Decimal

from app.alerts.enums import AlarmCondition

_SYMBOLS = {
    AlarmCondition.PRICE_GTE.value: "≥",
    AlarmCondition.PRICE_LTE.value: "≤",
    AlarmCondition.CROSS_UP.value: "↗ kesişim",
    AlarmCondition.CROSS_DOWN.value: "↘ kesişim",
    AlarmCondition.PRICE_NEAR.value: "≈",
    AlarmCondition.PERCENT_UP_FROM_BASE.value: "% yukarı",
    AlarmCondition.PERCENT_DOWN_FROM_BASE.value: "% aşağı",
}


def money(value) -> str:
    number = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    return f"{number:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def condition_text(alert) -> str:
    if alert.condition_type in {
        AlarmCondition.PERCENT_UP_FROM_BASE.value,
        AlarmCondition.PERCENT_DOWN_FROM_BASE.value,
    }:
        direction = "+" if alert.condition_type == AlarmCondition.PERCENT_UP_FROM_BASE.value else "-"
        return (
            f"{money(alert.base_price)} TL'den {direction}%{money(alert.percentage_value)} "
            f"→ {money(alert.target_price)} TL"
        )
    return f"{_SYMBOLS.get(alert.condition_type, alert.condition_type)} {money(alert.target_price)} TL"


def format_trigger_message(alert, trigger) -> str:
    delay = max(0, int(trigger.freshness_seconds or 0))
    repeat_text = (
        "Tek seferlik; bu bildirimden sonra tamamlanır"
        if alert.mode == "ONE_SHOT"
        else f"{alert.repeat_interval_seconds} saniyede bir, durdurulana kadar"
    )
    return (
        "🚨 FİYAT ALARMI TETİKLENDİ\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📌 Hisse: {alert.symbol}\n"
        f"🎯 Koşul: {condition_text(alert)}\n"
        f"💰 Güncel fiyat: {money(trigger.triggered_price)} TL\n"
        f"🕒 Veri zamanı: {trigger.data_timestamp:%d.%m.%Y %H:%M:%S}\n"
        f"📡 Kaynak: {trigger.provider}\n"
        f"⏱️ Gecikme: {delay} saniye\n"
        f"🔁 Tekrar: {repeat_text}\n"
        f"🔖 Referans: {alert.public_id}\n\n"
        "Telefon sesi Telegram ve cihaz bildirim ayarlarına bağlıdır."
    )


def format_alarm_summary(alert) -> str:
    return (
        f"🔔 {alert.symbol} • {condition_text(alert)}\n"
        f"Durum: {alert.status} | Mod: {alert.mode}\n"
        f"Tekrar: {alert.repeat_interval_seconds} sn | Referans: {alert.public_id}"
    )
