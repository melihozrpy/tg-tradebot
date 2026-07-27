from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.alerts.enums import AlarmMode, AlarmStatus, DeliveryStatus
from app.alerts.evaluator import evaluate_price_alarm
from app.alerts.repository import due_alerts
from app.alerts.schemas import PriceObservation
from app.models.database import PriceAlertDelivery, PriceAlertTrigger, User, UserAlarmSetting, UserPriceAlert
from app.services.current_price_service import resolve_current_price

logger = logging.getLogger("mergen_quant.user_price_alerts.monitor")


def _aware(value: datetime | None, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _observation(result, now: datetime) -> PriceObservation | None:
    if result.current_price is None:
        return None
    data_time = _aware(result.current_price_timestamp, now)
    return PriceObservation(
        symbol=result.symbol, price=Decimal(str(result.current_price)),
        provider=result.current_price_source, data_timestamp=data_time, retrieved_at=now,
        freshness_seconds=max(0, int((now - data_time).total_seconds())),
        is_live=result.is_live_price, fallback_used=result.fallback_used,
    )


def _enqueue_first(db: Session, alert: UserPriceAlert, observation: PriceObservation, now: datetime) -> bool:
    trigger_key = f"{alert.public_id}:{alert.trigger_count + 1}:{observation.data_timestamp.isoformat()}"
    trigger = PriceAlertTrigger(
        alert_id=alert.id, trigger_sequence=alert.trigger_count + 1,
        triggered_price=observation.price, target_price_snapshot=alert.target_price,
        condition_type_snapshot=alert.condition_type, detected_at=now,
        data_timestamp=observation.data_timestamp, provider=observation.provider,
        freshness_seconds=observation.freshness_seconds, idempotency_key=trigger_key,
    )
    db.add(trigger)
    try:
        db.flush()
    except IntegrityError:
        db.rollback(); return False
    delivery = PriceAlertDelivery(
        trigger_id=trigger.id, alert_id=alert.id, telegram_user_id=alert.telegram_user_id,
        chat_id=alert.chat_id, scheduled_for=now, status=DeliveryStatus.PENDING.value,
        idempotency_key=f"{trigger_key}:delivery:1",
    )
    db.add(delivery)
    alert.status = AlarmStatus.TRIGGERED.value
    alert.last_triggered_at = now
    alert.trigger_count += 1
    alert.next_delivery_at = now
    db.commit()
    return True


def _enqueue_repeat(db: Session, alert: UserPriceAlert, now: datetime) -> bool:
    trigger = db.query(PriceAlertTrigger).filter_by(alert_id=alert.id, status="OPEN").order_by(
        PriceAlertTrigger.id.desc()).first()
    if trigger is None:
        return False
    pending = db.query(PriceAlertDelivery.id).filter(
        PriceAlertDelivery.trigger_id == trigger.id,
        PriceAlertDelivery.status.in_([
            DeliveryStatus.PENDING.value,
            DeliveryStatus.SENDING.value,
            DeliveryStatus.RETRY.value,
        ]),
    ).first()
    if pending is not None:
        return False
    sequence = db.query(PriceAlertDelivery).filter_by(trigger_id=trigger.id).count() + 1
    row = PriceAlertDelivery(
        trigger_id=trigger.id, alert_id=alert.id, telegram_user_id=alert.telegram_user_id,
        chat_id=alert.chat_id, scheduled_for=now, status=DeliveryStatus.PENDING.value,
        idempotency_key=f"{trigger.idempotency_key}:delivery:{sequence}",
    )
    db.add(row)
    try:
        db.commit(); return True
    except IntegrityError:
        db.rollback(); return False


def _in_quiet_hours(setting: UserAlarmSetting | None, now: datetime) -> bool:
    if setting is None or not setting.quiet_hours_enabled:
        return False
    try:
        from zoneinfo import ZoneInfo

        local = now.astimezone(ZoneInfo(setting.timezone or "Europe/Istanbul"))
        start_h, start_m = map(int, (setting.quiet_hours_start or "").split(":"))
        end_h, end_m = map(int, (setting.quiet_hours_end or "").split(":"))
        current = local.hour * 60 + local.minute
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        return start <= current < end if start < end else current >= start or current < end
    except (TypeError, ValueError, KeyError):
        return False


def run_alarm_monitor_cycle(db: Session, provider, settings, *, now: datetime | None = None) -> dict:
    """Aktif alarmları sembole göre gruplayıp her sembolü döngüde bir kez çözer."""
    now = now or datetime.now(timezone.utc)
    alerts = due_alerts(db, now)
    user_ids = {alert.user_id for alert in alerts}
    users = {row.id: row for row in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
    user_settings = {
        row.user_id: row for row in db.query(UserAlarmSetting).filter(UserAlarmSetting.user_id.in_(user_ids)).all()
    } if user_ids else {}
    try:
        market_open = bool(provider.is_market_open())
    except Exception:
        logger.warning("Alarm monitoru piyasa durumunu doğrulayamadı; seans alarmları atlandı.")
        market_open = False
    grouped: dict[str, list[UserPriceAlert]] = defaultdict(list)
    skipped = 0
    for alert in alerts:
        owner = users.get(alert.user_id)
        setting = user_settings.get(alert.user_id)
        if owner is not None and owner.kill_switch_active:
            skipped += 1
            continue
        if _in_quiet_hours(setting, now):
            skipped += 1
            continue
        market_hours_only = alert.market_hours_only
        if setting is not None:
            market_hours_only = bool(setting.market_hours_only)
        if market_hours_only and not market_open:
            skipped += 1
            continue
        grouped[alert.normalized_symbol].append(alert)
    fetched = triggered = rejected = repeated = errors = 0
    for symbol, symbol_alerts in grouped.items():
        try:
            result = resolve_current_price(
                provider, symbol, now=now, timezone_name=settings.timezone_name,
            )
            fetched += 1
            observation = _observation(result, now)
        except Exception as exc:  # tek sembol diğerlerini durdurmaz
            logger.warning("Alarm fiyatı alınamadı symbol=%s error=%s", symbol, type(exc).__name__)
            errors += 1; continue
        if observation is None:
            rejected += len(symbol_alerts); continue
        for alert in symbol_alerts:
            if alert.status == AlarmStatus.TRIGGERED.value:
                if alert.next_delivery_at is not None:
                    due = _aware(alert.next_delivery_at, now) <= now
                    if due and _enqueue_repeat(db, alert, now): repeated += 1
                continue
            if alert.status == AlarmStatus.SNOOZED.value:
                until = _aware(alert.snoozed_until, now)
                if until > now:
                    continue
                alert.status = AlarmStatus.TRIGGERED.value; alert.snoozed_until = None
                if _enqueue_repeat(db, alert, now): repeated += 1
                continue
            decision = evaluate_price_alarm(
                alert, observation,
                stale_after_seconds=settings.user_price_alert_stale_after_seconds,
                production=settings.app_env.casefold() in {"production", "prod"},
            )
            old_last = alert.last_observed_price
            same_timestamp = alert.last_price_timestamp is not None and _aware(alert.last_price_timestamp, now) == observation.data_timestamp
            if decision.rejection_reason:
                rejected += 1
            elif decision.should_rearm and alert.mode in {AlarmMode.PERSISTENT.value, AlarmMode.RECURRING_CROSS.value}:
                alert.status = AlarmStatus.ACTIVE.value; alert.acknowledged_at = None
            elif decision.triggered and alert.status == AlarmStatus.ACTIVE.value and not same_timestamp:
                if _enqueue_first(db, alert, observation, now): triggered += 1
            if not same_timestamp:
                alert.previous_valid_price = old_last
                alert.previous_price_timestamp = alert.last_price_timestamp
                alert.last_observed_price = observation.price
                alert.last_price_timestamp = observation.data_timestamp
            alert.last_provider = observation.provider
            alert.last_freshness_seconds = observation.freshness_seconds
            alert.last_evaluated_at = now
        db.commit()
    return {"cycle_id": uuid4().hex[:12], "alerts": len(alerts), "symbols_fetched": fetched,
            "triggered": triggered, "repeats_queued": repeated, "rejected": rejected,
            "skipped": skipped, "errors": errors}
