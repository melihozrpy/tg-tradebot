from __future__ import annotations

from fastapi.testclient import TestClient

from app.config.settings import Settings
from app.data.provider_factory import build_market_data_provider
from app.main import app
from app.telegram.bot import _build_evening_scan_scheduler
from app.utils.financial_formatter import format_price, format_percent


def test_fastapi_stage5e_smoke():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert response.json()["name"] == "MERGEN QUANT"


def test_bot_scheduler_stage5e_smoke():
    settings = Settings(
        _env_file=None,
        telegram_bot_token="test-token",
        market_data_provider="mock",
        close_scan_enabled=True,
        enhanced_alarm_scan_enabled=True,
    )
    scheduler = _build_evening_scan_scheduler(settings, application=None)
    assert scheduler is not None
    assert scheduler.running is False


def test_default_historical_price_mode_is_adjusted():
    settings = Settings(
        _env_file=None, market_data_provider="yfinance", price_adjustment_mode="adjusted",
        yahoo_chart_fallback_enabled=False,
    )
    provider = build_market_data_provider(settings)
    assert provider.price_mode == "adjusted"
    assert provider.primary.price_mode == "adjusted"


def test_raw_historical_price_mode_remains_configurable():
    settings = Settings(
        _env_file=None, market_data_provider="yfinance", price_adjustment_mode="raw",
        yahoo_chart_fallback_enabled=False,
    )
    provider = build_market_data_provider(settings)
    assert provider.price_mode == "unadjusted"


def test_financial_formatter_hides_nan_inf_and_none():
    assert format_price(float("nan")) == "Veri bulunamadı"
    assert format_percent(float("inf")) == "Veri bulunamadı"
    assert format_price(None) == "Veri bulunamadı"
