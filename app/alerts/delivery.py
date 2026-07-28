from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from app.alerts.enums import AlarmMode, AlarmStatus, DeliveryStatus, SoundMode
from app.alerts.messages import format_trigger_message
from app.models.database import PriceAlertDelivery, PriceAlertTrigger, UserPriceAlert
from app.services.alarm_sound_service import generate_alarm_wav

logger = logging.getLogger("mergen_quant.user_price_alerts.delivery")
_SENDING_LEASE = timedelta(minutes=5)


def _buttons(reference: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔕 Alarmı Durdur", callback_data=f"upa_stop_{reference}"),
         InlineKeyboardButton("⏰ 5 Dakika Ertele", callback_data=f"upa_snooze5_{reference}")],
        [InlineKeyboardButton("🛑 Tamamen Kapat", callback_data=f"upa_delete_{reference}"),
         InlineKeyboardButton("📊 Analiz Et", callback_data=f"upa_analysis_{reference}")],
    ])


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: Telegram teslimatı başarısız"[:300]


def _claim_alarm_delivery(
    db: Session,
    delivery_id: int,
    now: datetime,
) -> PriceAlertDelivery | None:
    """Atomically claim a due outbox row across multiple bot replicas."""

    claimed = db.query(PriceAlertDelivery).filter(
        PriceAlertDelivery.id == delivery_id,
        PriceAlertDelivery.status.in_([DeliveryStatus.PENDING.value, DeliveryStatus.RETRY.value]),
        PriceAlertDelivery.scheduled_for <= now,
    ).update(
        {
            PriceAlertDelivery.status: DeliveryStatus.SENDING.value,
            PriceAlertDelivery.attempted_at: now,
            PriceAlertDelivery.attempt_count: PriceAlertDelivery.attempt_count + 1,
        },
        synchronize_session=False,
    )
    db.commit()
    if claimed != 1:
        return None
    db.expire_all()
    return db.get(PriceAlertDelivery, delivery_id)


async def deliver_alarm_outbox(application, db: Session, settings, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    db.query(PriceAlertDelivery).filter(
        PriceAlertDelivery.status == DeliveryStatus.SENDING.value,
        or_(
            PriceAlertDelivery.attempted_at.is_(None),
            PriceAlertDelivery.attempted_at <= now - _SENDING_LEASE,
        ),
    ).update({
        "status": DeliveryStatus.RETRY.value,
        "scheduled_for": now,
        "next_retry_at": now,
        "error_code": "RECOVERED_STUCK_DELIVERY",
    }, synchronize_session=False)
    db.commit()
    limit = int(settings.user_price_alert_max_global_deliveries_per_minute)
    row_ids = [
        row[0]
        for row in db.query(PriceAlertDelivery.id).filter(
            PriceAlertDelivery.status.in_([DeliveryStatus.PENDING.value, DeliveryStatus.RETRY.value]),
            PriceAlertDelivery.scheduled_for <= now,
        ).order_by(PriceAlertDelivery.scheduled_for, PriceAlertDelivery.id).limit(limit).all()
    ]
    user_counts: dict[int, int] = defaultdict(int)
    sent = retried = failed = skipped = claimed_count = 0
    for row_id in row_ids:
        row = _claim_alarm_delivery(db, row_id, now)
        if row is None:
            continue
        claimed_count += 1
        if user_counts[row.telegram_user_id] >= settings.user_price_alert_max_deliveries_per_minute_per_user:
            row.status = DeliveryStatus.RETRY.value
            row.scheduled_for = now + timedelta(minutes=1)
            row.next_retry_at = row.scheduled_for
            db.commit()
            skipped += 1
            continue
        alert = db.get(UserPriceAlert, row.alert_id)
        trigger = db.get(PriceAlertTrigger, row.trigger_id)
        if alert is None or trigger is None or alert.status not in {AlarmStatus.TRIGGERED.value, AlarmStatus.SNOOZED.value}:
            row.status = DeliveryStatus.CANCELLED.value; db.commit(); continue
        if alert.status == AlarmStatus.SNOOZED.value:
            resume_at = alert.snoozed_until or (now + timedelta(minutes=1))
            if resume_at.tzinfo is None:
                resume_at = resume_at.replace(tzinfo=timezone.utc)
            row.status = DeliveryStatus.RETRY.value
            row.scheduled_for = max(now + timedelta(seconds=1), resume_at)
            row.next_retry_at = row.scheduled_for
            db.commit()
            skipped += 1
            continue
        try:
            message = await application.bot.send_message(
                chat_id=row.chat_id, text=format_trigger_message(alert, trigger),
                reply_markup=_buttons(alert.public_id), disable_notification=False,
            )
            should_audio = alert.sound_mode == SoundMode.PERIODIC.value or (
                alert.sound_mode == SoundMode.FIRST_TRIGGER.value
                and db.query(PriceAlertDelivery).filter_by(
                    trigger_id=trigger.id, status=DeliveryStatus.SENT.value,
                ).count() == 0
            )
            if settings.user_price_alert_audio_enabled and should_audio:
                try:
                    path = generate_alarm_wav(alert.sound_name)
                    with open(path, "rb") as audio:
                        await application.bot.send_audio(chat_id=row.chat_id, audio=audio, title="Montana fiyat alarmı")
                except (OSError, ValueError, RuntimeError, TelegramError) as exc:
                    # Text has already been accepted by Telegram. A local WAV
                    # generation/open failure must not retry and duplicate it.
                    row.error_code = "AUDIO_DELIVERY_ERROR"
                    row.error_message_sanitized = _safe_error(exc)
            row.status = DeliveryStatus.SENT.value; row.sent_at = now
            row.telegram_message_id = getattr(message, "message_id", None)
            if alert.mode == AlarmMode.ONE_SHOT.value:
                alert.status = AlarmStatus.COMPLETED.value
                alert.next_delivery_at = None
                trigger.status = "CLOSED"
                trigger.closed_at = now
            else:
                alert.next_delivery_at = now + timedelta(seconds=alert.repeat_interval_seconds)
            sent += 1; user_counts[row.telegram_user_id] += 1
        except RetryAfter as exc:
            retry_value = getattr(exc, "retry_after", 30) or 30
            if isinstance(retry_value, timedelta):
                retry_value = retry_value.total_seconds()
            retry_seconds = max(1, int(retry_value))
            row.status = DeliveryStatus.RETRY.value
            row.next_retry_at = now + timedelta(seconds=retry_seconds)
            row.scheduled_for = row.next_retry_at; row.error_code = "RATE_LIMIT"; retried += 1
        except (Forbidden, BadRequest) as exc:
            row.status = DeliveryStatus.FAILED.value; row.error_code = type(exc).__name__.upper()
            row.error_message_sanitized = _safe_error(exc); failed += 1
            if isinstance(exc, Forbidden):
                alert.status = AlarmStatus.DISABLED.value
                alert.next_delivery_at = None
        except TelegramError as exc:
            if row.attempt_count >= 8:
                row.status = DeliveryStatus.FAILED.value; failed += 1
            else:
                seconds = min(900, 2 ** row.attempt_count * 5)
                row.status = DeliveryStatus.RETRY.value; row.scheduled_for = now + timedelta(seconds=seconds); retried += 1
            row.error_code = "TELEGRAM_ERROR"; row.error_message_sanitized = _safe_error(exc)
        db.commit()
    return {
        "due": len(row_ids),
        "claimed": claimed_count,
        "sent": sent,
        "retry": retried,
        "failed": failed,
        "throttled": skipped,
    }
