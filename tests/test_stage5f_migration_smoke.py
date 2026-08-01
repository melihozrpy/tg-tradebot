from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from telegram.ext import CallbackQueryHandler

from app.config.settings import get_settings
from app.main import app
from app.models.database import PortfolioPosition, User
from app.telegram.bot import build_telegram_application
from app.telegram.handlers_v3 import (
    _analysis_action_keyboard,
    _long_term_keyboard,
    _target_keyboard,
    handle_stage5f_callback,
)


ROOT = Path(__file__).resolve().parent.parent
STAGE5F_TABLES = {
    "long_term_scenarios",
    "user_price_targets",
    "target_roadmap_steps",
    "valuation_snapshots",
    "target_tracking_records",
}


def _alembic(url: str, *args: str):
    env = os.environ.copy()
    env.update({"DATABASE_URL": url, "MARKET_DATA_PROVIDER": "mock", "TELEGRAM_BOT_TOKEN": "test-token"})
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )


def test_28_fresh_database_migrates_to_head(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh-stage5f.db'}"
    result = _alembic(url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    engine = create_engine(url)
    try:
        assert STAGE5F_TABLES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_29_existing_stage5d_database_upgrades_to_head(tmp_path):
    url = f"sqlite:///{tmp_path / 'existing-stage5d.db'}"
    assert _alembic(url, "upgrade", "0005_stage5d_reliability_alerts_charts").returncode == 0
    result = _alembic(url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    engine = create_engine(url)
    try:
        assert STAGE5F_TABLES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_30_existing_user_and_portfolio_rows_are_preserved(tmp_path):
    url = f"sqlite:///{tmp_path / 'preserve.db'}"
    assert _alembic(url, "upgrade", "0005_stage5d_reliability_alerts_charts").returncode == 0
    engine = create_engine(url)
    db = sessionmaker(bind=engine)()
    user = User(telegram_user_id=556677, total_capital=321_000, cash_balance=45_000)
    db.add(user)
    db.flush()
    db.add(PortfolioPosition(user_id=user.id, symbol="SVGYO", lot=125, average_cost=12.34))
    db.commit()
    db.close()
    engine.dispose()

    result = _alembic(url, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    engine = create_engine(url)
    db = sessionmaker(bind=engine)()
    try:
        saved_user = db.query(User).filter_by(telegram_user_id=556677).one()
        saved_position = db.query(PortfolioPosition).filter_by(user_id=saved_user.id, symbol="SVGYO").one()
        assert saved_user.total_capital == 321_000
        assert saved_user.cash_balance == 45_000
        assert saved_position.lot == 125
        assert saved_position.average_cost == 12.34
    finally:
        db.close()
        engine.dispose()


def test_31_fastapi_smoke_test_passes():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert response.json()["name"] == "MONTANA FİNANS ROBOTU"


def test_32_telegram_bot_and_stage5f_buttons_load(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token-placeholder-for-stage5f")
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "mock")
    get_settings.cache_clear()
    application = build_telegram_application()
    try:
        button_texts = {
            button.text
            for markup in (
                _analysis_action_keyboard("SVGYO"),
                _long_term_keyboard("SVGYO"),
                _target_keyboard("SVGYO", 70),
            )
            for row in markup.inline_keyboard
            for button in row
        }
        expected = {
            "Boğa Detayı",
            "Ayı Detayı",
            "Teknik Detay",
            "Değerleme Detayı",
            "Hedef Yolu",
            "Standart Grafik",
            "Detaylı Grafik",
            "Uzun Grafik",
            "Veri Kaynakları",
        }
        assert expected <= button_texts
        assert any(
            isinstance(handler, CallbackQueryHandler) and handler.callback == handle_stage5f_callback
            for handlers in application.handlers.values()
            for handler in handlers
        )
    finally:
        get_settings.cache_clear()
