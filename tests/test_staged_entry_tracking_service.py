from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis.staged_entry import build_staged_entry_plan
from app.models.database import Base, StagedEntryRecord, User
from app.services.staged_entry_tracking_service import (
    monitor_staged_entry_plans,
    pending_staged_entry_events,
    save_staged_entry_plan,
)
from tests.test_indicator_bundle_and_staged_entry import _scenario


class _Provider:
    def __init__(self, close_value: float, low_value: float, high_value: float):
        count = 140
        close = np.linspace(90.0, close_value, count)
        self.frame = pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-01-01", periods=count, freq="15min", tz="UTC"),
                "open": close - 0.1,
                "high": close + 0.35,
                "low": close - 0.35,
                "close": close,
                "volume": np.linspace(1_000_000, 2_000_000, count),
            }
        )
        self.frame.loc[count - 1, ["low", "high", "close"]] = [low_value, high_value, close_value]

    def get_ohlcv(self, symbol, timeframe, start, end):
        return self.frame.copy()


class _Settings:
    technical_screener_min_confluence = 3


def _db_and_plan():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    user = User(telegram_user_id=12345, is_admin=True)
    db.add(user)
    db.commit()
    plan = build_staged_entry_plan(_scenario(current_price=112.0), symbol="THYAO")
    save_staged_entry_plan(db, user=user, telegram_chat_id=12345, plan=plan)
    return db


def test_monitor_confirms_zone_fills_stages_and_queues_average_cost_messages() -> None:
    db = _db_and_plan()
    try:
        result = monitor_staged_entry_plans(db, _Provider(104.5, 103.8, 106.2), _Settings())
        record = db.query(StagedEntryRecord).one()
        events = pending_staged_entry_events(db)
        assert result["confirmed"] == 1
        assert result["filled"] == 3
        assert record.status == "COMPLETED"
        assert len(events) == 4
        assert any("Yeni ortalama maliyet" in event.message_text for event in events)
    finally:
        db.close()


def test_monitor_invalidates_pending_plan_and_cancels_unfilled_stages() -> None:
    db = _db_and_plan()
    try:
        result = monitor_staged_entry_plans(db, _Provider(103.2, 102.8, 104.0), _Settings())
        record = db.query(StagedEntryRecord).one()
        events = pending_staged_entry_events(db)
        assert result["invalidated"] == 1
        assert record.status == "INVALIDATED"
        assert len(events) == 1
        assert "Kalan sanal kademeler otomatik iptal edildi" in events[0].message_text
    finally:
        db.close()
