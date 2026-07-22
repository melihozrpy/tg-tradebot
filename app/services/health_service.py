from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.database import ProviderHealthLog


@dataclass
class RuntimeHealthState:
    last_successful_data_fetch: Optional[datetime] = None
    last_successful_alarm_scan: Optional[datetime] = None
    last_successful_close_scan: Optional[datetime] = None
    scheduler_status: str = "not_started"
    telegram_status: str = "not_started"
    details: dict = field(default_factory=dict)


_STATE = RuntimeHealthState()
_LOCK = RLock()


def mark_runtime_health(component: str, status: str = "ok", at: Optional[datetime] = None, detail: Optional[str] = None) -> None:
    with _LOCK:
        moment = at or datetime.now(timezone.utc)
        if component == "data_fetch" and status == "ok":
            _STATE.last_successful_data_fetch = moment
        elif component == "alarm_scan" and status == "ok":
            _STATE.last_successful_alarm_scan = moment
        elif component == "close_scan" and status == "ok":
            _STATE.last_successful_close_scan = moment
        elif component == "scheduler":
            _STATE.scheduler_status = status
        elif component == "telegram":
            _STATE.telegram_status = status
        if detail is not None:
            _STATE.details[component] = detail


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _latest_optional_provider(db: Session, provider: str, enabled: bool) -> dict:
    if not enabled:
        return {"status": "disabled", "detail": "Yapılandırma ile devre dışı."}
    try:
        row = (
            db.query(ProviderHealthLog)
            .filter(ProviderHealthLog.provider == provider)
            .order_by(ProviderHealthLog.checked_at.desc())
            .first()
        )
    except Exception as exc:  # noqa: BLE001 - health endpoint'i kısmi migration'da da yanıt vermeli
        db.rollback()
        return {"status": "unknown", "detail": f"Sağlık tablosu okunamadı: {exc.__class__.__name__}."}
    if row is None:
        return {"status": "unknown", "detail": "Henüz sağlık kaydı yok."}
    return {"status": row.status, "detail": row.detail, "checked_at": _iso(row.checked_at)}


def collect_health(db: Session, settings, provider=None) -> dict:
    try:
        db.execute(text("SELECT 1"))
        database = {"status": "ok"}
    except Exception as exc:  # noqa: BLE001
        database = {"status": "down", "detail": str(exc)}

    if provider is None:
        providers = {"status": "unknown", "detail": "Provider bu istekte probe edilmedi."}
        cache = {"status": "unknown"}
    else:
        try:
            providers = provider.health_check()
        except Exception as exc:  # noqa: BLE001
            providers = {"status": "down", "detail": str(exc)}
        cache_obj = getattr(provider, "cache", None)
        cache = cache_obj.health() if cache_obj and hasattr(cache_obj, "health") else {"status": "not_configured"}

    with _LOCK:
        runtime = {
            "scheduler": {"status": _STATE.scheduler_status, "detail": _STATE.details.get("scheduler")},
            "telegram": {"status": _STATE.telegram_status, "detail": _STATE.details.get("telegram")},
            "last_successful_data_fetch": _iso(_STATE.last_successful_data_fetch or getattr(provider, "last_successful_fetch_at", None)),
            "last_successful_alarm_scan": _iso(_STATE.last_successful_alarm_scan),
            "last_successful_close_scan": _iso(_STATE.last_successful_close_scan),
        }

    components = {
        "database": database,
        "providers": providers,
        "cache": cache,
        **runtime,
        "gdelt": _latest_optional_provider(db, "gdelt", settings.gdelt_enabled),
        "groq": _latest_optional_provider(db, "groq", settings.groq_enabled),
    }
    bad = any(
        isinstance(value, dict) and value.get("status") in {"down", "error"}
        for value in components.values()
    )
    return {"status": "degraded" if bad else "ok", "components": components, "checked_at": datetime.now(timezone.utc).isoformat()}
