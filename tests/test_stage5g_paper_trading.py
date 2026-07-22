from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.backtest.engine_v5g import TransactionCostConfig
from app.execution.paper_trading_engine import PaperTradingEngine, PaperTradingError
from app.models.database import Base, PaperTradeEvent, User, build_engine


@pytest.fixture()
def paper_env(tmp_path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'paper.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    users = [User(telegram_user_id=7001), User(telegram_user_id=7002)]
    db.add_all(users); db.commit()
    for user in users: db.refresh(user)
    yield factory, db, users
    db.close()


def _paper(db):
    return PaperTradingEngine(db, transaction_costs=TransactionCostConfig(0, 0, 0, 0, 0))


def _open(engine, user_id, signal_id=10):
    return engine.open_trade(
        user_id=user_id, symbol="THYAO", quantity=10, current_price=100,
        stop_price=95, targets=(105, 110, 115), signal_id=signal_id,
        provider="fake", data_quality="VALID",
    )


def _bar(high, low, close=100):
    return {"open": 100, "high": high, "low": low, "close": close, "is_complete": True, "data_quality": "VALID"}


def test_24_paper_trade_opens_and_is_persisted(paper_env):
    _, db, users = paper_env
    trade = _open(_paper(db), users[0].id)
    assert trade.status == "ACTIVE"
    assert trade.user_id == users[0].id
    assert db.query(PaperTradeEvent).filter_by(paper_trade_id=trade.id, event_type="OPENED").count() == 1


def test_25_duplicate_signal_paper_trade_is_blocked(paper_env):
    _, db, users = paper_env
    engine = _paper(db); _open(engine, users[0].id, signal_id=55)
    with pytest.raises(PaperTradingError, match="Ayni sinyal"):
        _open(engine, users[0].id, signal_id=55)


def test_26_stop_is_triggered_on_completed_fresh_bar(paper_env):
    _, db, users = paper_env
    engine = _paper(db); trade = _open(engine, users[0].id)
    updated = engine.update_trade_from_completed_bar(
        user_id=users[0].id, trade_id=trade.id, bar=_bar(101, 94, 95),
        fetched_at=datetime.now(timezone.utc),
    )
    assert updated.status == "STOPPED"
    assert updated.remaining_quantity == 0


def test_27_targets_one_two_three_are_triggered(paper_env):
    _, db, users = paper_env
    engine = _paper(db); trade = _open(engine, users[0].id)
    updated = engine.update_trade_from_completed_bar(
        user_id=users[0].id, trade_id=trade.id, bar=_bar(116, 99, 115),
        fetched_at=datetime.now(timezone.utc),
    )
    events = [item.event_type for item in db.query(PaperTradeEvent).filter_by(paper_trade_id=trade.id).all()]
    assert updated.status == "TARGET_3_HIT"
    assert {"TARGET_1_HIT", "TARGET_2_HIT", "TARGET_3_HIT"}.issubset(events)


def test_28_partial_exit_leaves_correct_quantity(paper_env):
    _, db, users = paper_env
    engine = _paper(db); trade = _open(engine, users[0].id)
    updated = engine.update_trade_from_completed_bar(
        user_id=users[0].id, trade_id=trade.id, bar=_bar(106, 99, 105),
        fetched_at=datetime.now(timezone.utc),
    )
    assert updated.remaining_quantity == pytest.approx(6)
    assert updated.status == "TARGET_1_HIT"


def test_29_restart_preserves_open_paper_trade(paper_env):
    factory, db, users = paper_env
    user_id = users[0].id
    trade = _open(_paper(db), user_id)
    trade_id = trade.id
    db.close()
    restarted_db = factory()
    try:
        restored = _paper(restarted_db).get_trade(user_id, trade_id)
        assert restored.status == "ACTIVE"
        assert restored.remaining_quantity == 10
    finally:
        restarted_db.close()


def test_30_stale_cache_does_not_trigger_target_or_stop(paper_env):
    _, db, users = paper_env
    engine = _paper(db); trade = _open(engine, users[0].id)
    updated = engine.update_trade_from_completed_bar(
        user_id=users[0].id, trade_id=trade.id, bar=_bar(120, 90),
        fetched_at=datetime.now(timezone.utc) - timedelta(hours=2),
        now=datetime.now(timezone.utc), max_cache_age=timedelta(minutes=30),
    )
    assert updated.status == "ACTIVE"
    assert updated.remaining_quantity == 10


def test_31_other_user_cannot_view_or_close_trade(paper_env):
    _, db, users = paper_env
    engine = _paper(db); trade = _open(engine, users[0].id)
    with pytest.raises(PaperTradingError, match="bulunamadi"):
        engine.get_trade(users[1].id, trade.id)
    assert engine.list_trades(users[1].id) == []


def test_32_paper_engine_contains_no_real_broker_or_order_call():
    source = Path("app/execution/paper_trading_engine.py").read_text(encoding="utf-8").lower()
    assert "requests.post" not in source
    assert "broker api" not in source
    assert "basebrokeradapter" not in source


def test_66_paper_trade_expires_at_configured_holding_limit(paper_env):
    _, db, users = paper_env
    opened = datetime.now(timezone.utc) - timedelta(days=2)
    engine = _paper(db)
    trade = engine.open_trade(
        user_id=users[0].id, symbol="THYAO", quantity=10, current_price=100,
        stop_price=90, targets=(120, 130, 140), signal_id=909,
        provider="fake", data_quality="VALID", opened_at=opened, max_holding_days=1,
    )
    bar = _bar(102, 98, 101); bar["timestamp"] = datetime.now(timezone.utc)
    updated = engine.update_trade_from_completed_bar(
        user_id=users[0].id, trade_id=trade.id, bar=bar,
        fetched_at=datetime.now(timezone.utc),
    )
    assert updated.status == "EXPIRED"
