from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd

from app.models.database import User
from app.services.alert_service import create_alert
from app.services.notification_service import scan_and_notify_anomalies
from app.services.watchlist_service import add_symbol


class _FakeBot:
    def __init__(self):
        self.sent_messages: list[dict] = []
        self.sent_photos: list[dict] = []

    async def send_message(self, chat_id, text):
        self.sent_messages.append({"chat_id": chat_id, "text": text})

    async def send_photo(self, chat_id, photo, caption):
        self.sent_photos.append({"chat_id": chat_id, "caption": caption})


class _FakeApplication:
    def __init__(self):
        self.bot = _FakeBot()


def _synthetic_df_with_spike(n: int = 40) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=n, freq="B", tz="UTC")
    close = np.linspace(100, 110, n)
    df = pd.DataFrame({
        "timestamp": dates,
        "open": close - 0.2,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": [100000.0] * n,
    })
    df.loc[n - 1, "volume"] = 600000.0
    df.loc[n - 1, "open"] = df.loc[n - 2, "close"] * 1.05
    df.loc[n - 1, "high"] = df.loc[n - 1, "open"] + 1
    df.loc[n - 1, "low"] = df.loc[n - 1, "open"] - 0.5
    df.loc[n - 1, "close"] = df.loc[n - 1, "open"] + 0.5
    return df


def _user(db):
    u = User(telegram_user_id=777, total_capital=100000.0)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_scan_and_notify_anomalies_sends_to_subscribed_watcher(db_session, mock_provider, monkeypatch):
    user = _user(db_session)
    add_symbol(db_session, user, "THYAO")
    create_alert(db_session, user, "THYAO", "anomali")

    from app.data.mock_provider import MockMarketDataProvider

    def fake_get_ohlcv(self, symbol, timeframe, start, end):
        return _synthetic_df_with_spike()

    monkeypatch.setattr(MockMarketDataProvider, "get_ohlcv", fake_get_ohlcv)

    application = _FakeApplication()
    notified = asyncio.run(
        scan_and_notify_anomalies(application, db_session, mock_provider, ["THYAO"], timeframe="1d")
    )

    assert notified >= 1
    assert len(application.bot.sent_photos) + len(application.bot.sent_messages) >= 1


def test_scan_and_notify_anomalies_skips_users_without_watchlist_entry(db_session, mock_provider, monkeypatch):
    user = _user(db_session)
    # Kullanicinin izleme listesinde THYAO YOK; alarm kursa bile bildirim gitmemeli.
    create_alert(db_session, user, "THYAO", "anomali")

    from app.data.mock_provider import MockMarketDataProvider

    def fake_get_ohlcv(self, symbol, timeframe, start, end):
        return _synthetic_df_with_spike()

    monkeypatch.setattr(MockMarketDataProvider, "get_ohlcv", fake_get_ohlcv)

    application = _FakeApplication()
    notified = asyncio.run(
        scan_and_notify_anomalies(application, db_session, mock_provider, ["THYAO"], timeframe="1d")
    )
    assert notified == 0
    assert len(application.bot.sent_messages) == 0
    assert len(application.bot.sent_photos) == 0


def test_scan_and_notify_anomalies_noop_without_application(db_session, mock_provider, monkeypatch):
    user = _user(db_session)
    add_symbol(db_session, user, "THYAO")
    create_alert(db_session, user, "THYAO", "anomali")

    from app.data.mock_provider import MockMarketDataProvider

    def fake_get_ohlcv(self, symbol, timeframe, start, end):
        return _synthetic_df_with_spike()

    monkeypatch.setattr(MockMarketDataProvider, "get_ohlcv", fake_get_ohlcv)

    # application=None -> anomaliler kaydedilir ama Telegram mesaji GONDERILMEZ, cokme de olmaz.
    notified = asyncio.run(
        scan_and_notify_anomalies(None, db_session, mock_provider, ["THYAO"], timeframe="1d")
    )
    assert notified >= 1
