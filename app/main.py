from __future__ import annotations

import logging
from contextlib import asynccontextmanager

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


def on_startup() -> None:
    # Hatalı config ile uygulama başlamasın. Ayar validator'ları ve
    # strategy.yaml doğrulaması web sunucusu trafiğe açılmadan çalışır.
    settings = get_settings()
    get_strategy_config()
    init_db()
    logger.info("Mergen Quant V3 baslatildi. app_env=%s provider=%s", settings.app_env, settings.market_data_provider)


@asynccontextmanager
async def lifespan(_: FastAPI):
    on_startup()
    yield


app = FastAPI(
    title="MONTANA MELİH HİSSE BOT API",
    description="Akıllı BIST Analiz ve Risk Sistemi - MONTANA MELİH HİSSE BOT (analiz, tarama, sinyal takip, portföy risk ve backtest)",
    version="3.0.0",
    lifespan=lifespan,
)

app.include_router(routes_health.router)
app.include_router(routes_analysis.router)
app.include_router(routes_backtest.router)
app.include_router(routes_portfolio.router)
app.include_router(routes_dashboard.router)

@app.get("/")
def root():
    return {
        "name": "MONTANA MELİH HİSSE BOT",
        "tagline": "Akilli BIST Analiz ve Risk Sistemi",
        "version": "V3",
        "status": "running",
        "disclaimer": "Bu sistem yatirim tavsiyesi vermez, kural tabanli analiz ciktisi sunar.",
    }
