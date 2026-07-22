from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.database import User, UserSettings


def get_or_create_settings(db: Session, user: User) -> UserSettings:
    settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
    if settings is None:
        settings = UserSettings(user_id=user.id)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


ALLOWED_SETTING_KEYS = {
    "minimum_signal_score": float,
    "minimum_risk_reward": float,
    "evening_report_enabled": lambda v: str(v).lower() in ("true", "1", "evet", "acik"),
    "evening_report_time": str,
    "top_candidate_count": int,
    "quiet_hours_start": str,
    "quiet_hours_end": str,
    "intraday_preview_enabled": lambda v: str(v).lower() in ("true", "1", "evet", "acik"),
    "chart_type": str,
    "maximum_open_positions": int,
    "maximum_sector_exposure_percent": float,
}


class InvalidSettingError(Exception):
    pass


def update_setting(db: Session, user: User, key: str, raw_value: str) -> UserSettings:
    if key not in ALLOWED_SETTING_KEYS:
        raise InvalidSettingError(f"Bilinmeyen ayar: {key}. Gecerli ayarlar: {sorted(ALLOWED_SETTING_KEYS)}")

    caster = ALLOWED_SETTING_KEYS[key]
    try:
        value = caster(raw_value)
    except (ValueError, TypeError) as exc:
        raise InvalidSettingError(f"'{raw_value}' gecersiz deger ({key} icin).") from exc

    settings = get_or_create_settings(db, user)
    setattr(settings, key, value)
    db.commit()
    db.refresh(settings)
    return settings
