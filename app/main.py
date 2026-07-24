from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api import routes_analysis, routes_backtest, routes_dashboard, routes_health, routes_portfolio
from app.config.settings import get_settings, get_strategy_config
from app.models.database import init_db
from app.utils.logging_filters import install_sensitive_data_filter

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
)
install_sensitive_data_filter()
logger = logging.getLogger("mergen_quant.main")

app = FastAPI(
    title="MERGEN QUANT API",
    description="Akilli BIST Analiz ve Risk Sistemi - MERGEN QUANT (BIST hisse analiz, tarama, sinyal takip, portfoy risk ve backtest botu)",
    version="3.0.0",
)

app.include_router(routes_health.router)
app.include_router(routes_analysis.router)
app.include_router(routes_backtest.router)
app.include_router(routes_portfolio.router)
app.include_router(routes_dashboard.router)


@app.on_event("startup")
def on_startup() -> None:
    # Hatali config ile uygulama baslamasin (config.settings icindeki
    # validator'lar zaten kontrol eder, burada strategy.yaml de dogrulanir).
    settings = get_settings()
    get_strategy_config()
    init_db()
    logger.info(f"Mergen Quant V3 baslatildi. app_env={settings.app_env} provider={settings.market_data_provider}")


@app.get("/")
def root():
    return {
        "name": "MERGEN QUANT",
        "tagline": "Akilli BIST Analiz ve Risk Sistemi",
        "version": "V3",
        "status": "running",
        "disclaimer": "Bu sistem yatirim tavsiyesi vermez, kural tabanli analiz ciktisi sunar.",
    }
