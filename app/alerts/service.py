from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.alerts.enums import AlarmMode, AlarmStatus
from app.alerts.schemas import AlarmDraft, BulkParseResult
from app.models.database import (
    AlarmImportJob, AlarmImportRow, PriceAlertDelivery, PriceAlertTrigger,
    User, UserAlarmSetting, UserPriceAlert,
)
from app.services.alarm_sound_service import normalize_sound


class AlarmServiceError(ValueError):
    pass


class DuplicateAlarmError(AlarmServiceError):
    def __init__(self, existing: UserPriceAlert):
        self.existing = existing
        super().__init__(f"Aynı alarm zaten mevcut: {existing.public_id}")


def _public_id(prefix: str = "ALR", size: int = 6) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return f"{prefix}-" + "".join(secrets.choice(alphabet) for _ in range(size))


def get_alarm_settings(db: Session, user_id: int) -> UserAlarmSetting:
    value = db.get(UserAlarmSetting, user_id)
    if value is None:
        value = UserAlarmSetting(user_id=user_id)
        db.add(value); db.flush()
    return value


def create_alarm(db: Session, user: User, chat_id: int, draft: AlarmDraft, *, maximum_active: int = 500,
                 allow_duplicate: bool = False, commit: bool = True) -> UserPriceAlert:
    active_count = db.query(UserPriceAlert).filter(
        UserPriceAlert.user_id == user.id,
        UserPriceAlert.status.in_(["ACTIVE", "TRIGGERED", "SNOOZED", "ACKNOWLEDGED", "PAUSED"]),
    ).count()
    user_settings = get_alarm_settings(db, user.id)
    limit = min(maximum_active, user_settings.max_active_alerts)
    if active_count >= limit:
        raise AlarmServiceError(f"Aktif alarm sınırına ulaşıldı ({limit}).")
    if not 30 <= draft.repeat_interval_seconds <= 86_400:
        raise AlarmServiceError("Tekrar aralığı 30–86400 saniye olmalı.")
    duplicate = db.query(UserPriceAlert).filter(
        UserPriceAlert.user_id == user.id,
        UserPriceAlert.normalized_symbol == draft.symbol,
        UserPriceAlert.condition_type == draft.condition.value,
        UserPriceAlert.target_price == draft.target_price,
        UserPriceAlert.status.in_(["ACTIVE", "TRIGGERED", "SNOOZED", "ACKNOWLEDGED", "PAUSED"]),
    ).first()
    if duplicate is not None and not allow_duplicate:
        raise DuplicateAlarmError(duplicate)
    row = UserPriceAlert(
        public_id=_public_id(), user_id=user.id, telegram_user_id=user.telegram_user_id,
        chat_id=chat_id, symbol=draft.symbol, normalized_symbol=draft.symbol,
        condition_type=draft.condition.value, target_price=draft.target_price,
        base_price=draft.base_price, percentage_value=draft.percentage_value,
        near_tolerance=draft.near_tolerance, status=AlarmStatus.ACTIVE.value,
        mode=draft.mode.value, repeat_interval_seconds=draft.repeat_interval_seconds,
        sound_mode=draft.sound_mode.value, sound_name=normalize_sound(draft.sound_name or user_settings.default_sound_name),
        market_hours_only=user_settings.market_hours_only,
        note=draft.note, source_type=draft.source.value,
    )
    db.add(row)
    try:
        db.flush()
        if commit:
            db.commit(); db.refresh(row)
    except IntegrityError:
        db.rollback()
        raise AlarmServiceError("Alarm referansı oluşturulamadı; tekrar deneyin.")
    return row


def create_import_preview(db: Session, user: User, chat_id: int, parsed: BulkParseResult,
                          source_type: str, ttl_minutes: int = 30) -> AlarmImportJob:
    now = datetime.now(timezone.utc)
    job = AlarmImportJob(
        public_id=_public_id("IMP", 7), user_id=user.id, telegram_user_id=user.telegram_user_id,
        chat_id=chat_id, source_type=source_type, total_rows=len(parsed.valid) + len(parsed.invalid),
        valid_rows=len(parsed.valid), invalid_rows=len(parsed.invalid), duplicate_rows=len(parsed.duplicate_rows),
        expires_at=now + timedelta(minutes=ttl_minutes),
    )
    db.add(job); db.flush()
    valid_index = 0
    invalid_by_row = {item.row_number: item for item in parsed.invalid}
    max_row = max([*invalid_by_row, len(parsed.valid) + len(parsed.invalid)], default=0)
    for row_number in range(1, max_row + 1):
        issue = invalid_by_row.get(row_number)
        if issue:
            db.add(AlarmImportRow(import_job_id=job.id, row_number=row_number, raw_text=issue.raw_text,
                                  status="INVALID", validation_error=issue.error))
        elif valid_index < len(parsed.valid):
            item = parsed.valid[valid_index]; valid_index += 1
            db.add(AlarmImportRow(
                import_job_id=job.id, row_number=row_number, parsed_symbol=item.symbol,
                parsed_price=item.target_price, parsed_condition=item.condition.value,
                base_price=item.base_price, percentage_value=item.percentage_value,
                near_tolerance=item.near_tolerance, sound_name=item.sound_name,
                parsed_mode=item.mode.value, repeat_interval_seconds=item.repeat_interval_seconds,
                note=item.note, confidence=item.confidence, status="VALID",
            ))
    db.commit(); db.refresh(job)
    return job


def confirm_import(db: Session, user: User, job_ref: str, *, maximum_active: int = 500) -> list[UserPriceAlert]:
    job = db.query(AlarmImportJob).filter(
        AlarmImportJob.public_id == job_ref.upper(), AlarmImportJob.user_id == user.id,
    ).one_or_none()
    if job is None:
        raise AlarmServiceError("İçe aktarma önizlemesi bulunamadı.")
    now = datetime.now(timezone.utc)
    expires = job.expires_at.replace(tzinfo=timezone.utc) if job.expires_at.tzinfo is None else job.expires_at
    if expires <= now:
        raise AlarmServiceError("İçe aktarma önizlemesinin süresi dolmuş.")
    if job.status == "CONFIRMED":
        return db.query(UserPriceAlert).filter(UserPriceAlert.import_job_id == job.id, UserPriceAlert.user_id == user.id).all()
    rows = db.query(AlarmImportRow).filter_by(import_job_id=job.id, status="VALID").order_by(AlarmImportRow.row_number).all()
    created = []
    from app.alerts.enums import AlarmCondition, AlarmMode, ImportSource
    for item in rows:
        try:
            draft = AlarmDraft(item.parsed_symbol, Decimal(item.parsed_price), AlarmCondition(item.parsed_condition),
                               AlarmMode(item.parsed_mode), item.repeat_interval_seconds or 60, item.note,
                               source=ImportSource(job.source_type),
                               base_price=Decimal(item.base_price) if item.base_price is not None else None,
                               percentage_value=Decimal(item.percentage_value) if item.percentage_value is not None else None,
                               near_tolerance=Decimal(item.near_tolerance) if item.near_tolerance is not None else None,
                               sound_name=item.sound_name)
            alarm = create_alarm(db, user, job.chat_id, draft, maximum_active=maximum_active, commit=False)
            alarm.import_job_id = job.id; item.created_alert_id = alarm.id; created.append(alarm)
        except DuplicateAlarmError:
            item.status = "DUPLICATE"
    job.status = "CONFIRMED"; job.confirmed_at = now
    db.commit()
    return created


def acknowledge_alarm(db: Session, alert: UserPriceAlert, now=None) -> str:
    now = now or datetime.now(timezone.utc)
    if alert.status in {AlarmStatus.COMPLETED.value, AlarmStatus.ACKNOWLEDGED.value, AlarmStatus.DELETED.value}:
        return "Alarm zaten durdurulmuş."
    alert.acknowledged_at = now
    alert.status = AlarmStatus.COMPLETED.value if alert.mode == AlarmMode.ONE_SHOT.value else AlarmStatus.ACKNOWLEDGED.value
    alert.next_delivery_at = None
    db.query(PriceAlertTrigger).filter_by(alert_id=alert.id, status="OPEN").update({
        "status": "ACKNOWLEDGED", "acknowledged_at": now, "closed_at": now,
    })
    db.query(PriceAlertDelivery).filter(
        PriceAlertDelivery.alert_id == alert.id,
        PriceAlertDelivery.status.in_(["PENDING", "RETRY"]),
    ).update({"status": "CANCELLED"}, synchronize_session=False)
    db.commit()
    return "🔕 Alarm durduruldu."


def snooze_alarm(db: Session, alert: UserPriceAlert, minutes: int, now=None) -> str:
    now = now or datetime.now(timezone.utc)
    if alert.status not in {AlarmStatus.TRIGGERED.value, AlarmStatus.SNOOZED.value}:
        return "Yalnızca tetiklenmiş alarm ertelenebilir."
    alert.status = AlarmStatus.SNOOZED.value
    alert.snoozed_until = now + timedelta(minutes=minutes)
    alert.next_delivery_at = alert.snoozed_until
    db.query(PriceAlertDelivery).filter(
        PriceAlertDelivery.alert_id == alert.id,
        PriceAlertDelivery.status.in_(["PENDING", "RETRY"]),
    ).update({"status": "CANCELLED"}, synchronize_session=False)
    db.commit()
    return f"⏰ Alarm {minutes} dakika ertelendi."


def pause_alarm(db: Session, alert: UserPriceAlert) -> str:
    if alert.status == AlarmStatus.PAUSED.value:
        return "Alarm zaten duraklatılmış."
    alert.status = AlarmStatus.PAUSED.value; alert.next_delivery_at = None
    db.query(PriceAlertDelivery).filter(
        PriceAlertDelivery.alert_id == alert.id,
        PriceAlertDelivery.status.in_(["PENDING", "RETRY"]),
    ).update({"status": "CANCELLED"}, synchronize_session=False)
    db.commit(); return "⏸️ Alarm duraklatıldı."


def resume_alarm(db: Session, alert: UserPriceAlert) -> str:
    if alert.status == AlarmStatus.ACTIVE.value:
        return "Alarm zaten aktif."
    if alert.status in {AlarmStatus.DELETED.value, AlarmStatus.DISABLED.value}:
        return "Silinmiş veya devre dışı alarm yeniden açılamaz."
    alert.status = AlarmStatus.ACTIVE.value; alert.snoozed_until = None
    alert.acknowledged_at = None; alert.previous_valid_price = None
    db.commit(); return "▶️ Alarm yeniden etkinleştirildi."


def delete_alarm(db: Session, alert: UserPriceAlert, now=None) -> str:
    if alert.status == AlarmStatus.DELETED.value:
        return "Alarm zaten silinmiş."
    acknowledge_alarm(db, alert, now=now)
    alert.status = AlarmStatus.DELETED.value; alert.deleted_at = now or datetime.now(timezone.utc)
    db.commit(); return "🗑️ Alarm silindi."
