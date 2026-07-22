from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.data.provider_factory import build_market_data_provider
from app.models.database import get_db_session
from app.services.health_service import collect_health

router = APIRouter()


@router.get("/health")
def health(db: Session = Depends(get_db_session)):
    # Ana health endpoint canlı network çağrısı yapmaz; son bilinen durumları
    # ve yerel bileşenleri hızlı biçimde döndürür.
    return collect_health(db, get_settings(), provider=None)


@router.get("/health/data")
def health_data(db: Session = Depends(get_db_session)):
    snapshot = collect_health(db, get_settings(), provider=None)
    components = snapshot["components"]
    return {
        "status": snapshot["status"],
        "last_successful_data_fetch": components["last_successful_data_fetch"],
        "cache": components["cache"],
    }


@router.get("/health/providers")
def health_providers(probe: bool = False, db: Session = Depends(get_db_session)):
    settings = get_settings()
    provider = build_market_data_provider(settings) if probe else None
    snapshot = collect_health(db, settings, provider=provider)
    return {
        "status": snapshot["status"],
        "providers": snapshot["components"]["providers"],
        "gdelt": snapshot["components"]["gdelt"],
        "groq": snapshot["components"]["groq"],
    }


@router.get("/health/scheduler")
def health_scheduler(db: Session = Depends(get_db_session)):
    snapshot = collect_health(db, get_settings(), provider=None)
    return {
        "status": snapshot["components"]["scheduler"]["status"],
        "scheduler": snapshot["components"]["scheduler"],
        "last_successful_alarm_scan": snapshot["components"]["last_successful_alarm_scan"],
        "last_successful_close_scan": snapshot["components"]["last_successful_close_scan"],
    }


@router.get("/system/status")
def system_status():
    settings = get_settings()
    provider = build_market_data_provider(settings)
    provider_health = provider.health_check()
    return {
        "app_env": settings.app_env,
        "market_data_provider": provider_health,
        "kap_provider": settings.kap_provider,
        "broker_flow_provider": settings.broker_flow_provider,
        "fundamental_provider": settings.fundamental_provider,
        "market_open": provider.is_market_open(),
        "live_trading_enabled": False,
    }
