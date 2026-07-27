from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.models.database import Signal, SignalStateEnum, SignalTarget, SignalTypeEnum, User
from app.telegram.ultra_signal_handlers import SignalCommandError, clone_analysis_signal_for_user


NOW = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)


def _settings():
    return SimpleNamespace(
        default_total_capital=100_000,
        default_risk_percent=1.0,
        max_position_percent=20.0,
        max_daily_volume_participation_percent=1.0,
        default_tp1_allocation=40.0,
        default_tp2_allocation=35.0,
        default_tp3_allocation=25.0,
        backtest_price_mode="split_adjusted",
    )


def _legacy_signal(db, *, key: str = "legacy-plan") -> Signal:
    row = Signal(
        symbol="THYAO",
        timeframe="1d",
        signal_type=SignalTypeEnum.BUY_CANDIDATE,
        state=SignalStateEnum.WAITING_TRIGGER,
        score=82,
        confidence="yuksek",
        entry_zone_low=45.30,
        entry_zone_high=45.60,
        entry_trigger=45.50,
        stop_price=44.60,
        target_1=47.00,
        target_2=48.20,
        target_3=49.40,
        risk_reward=4.33,
        strategy_version="v3",
        data_timestamp=NOW,
        provider="licensed-test",
        idempotency_key=key,
        user_id=None,
        side="BUY",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_follow_clones_legacy_analysis_as_owned_pending_signal_idempotently(db_session):
    user = User(telegram_user_id=99001, total_capital=100_000, cash_balance=40_000)
    db_session.add(user)
    db_session.commit()
    source = _legacy_signal(db_session)

    first = clone_analysis_signal_for_user(db_session, user, source, _settings(), now=NOW)
    second = clone_analysis_signal_for_user(db_session, user, source, _settings(), now=NOW)

    assert first.id == second.id
    assert first.id != source.id
    assert first.user_id == user.id
    assert first.state == SignalStateEnum.PENDING_ENTRY
    assert first.actual_entry_price is None
    assert first.entry_order_type == "ENTRY_ZONE"
    assert first.requested_quantity > 0
    assert first.expires_at is not None
    targets = (
        db_session.query(SignalTarget)
        .filter(SignalTarget.signal_id == first.id)
        .order_by(SignalTarget.target_number)
        .all()
    )
    assert [float(row.allocation_percent) for row in targets] == [40.0, 35.0, 25.0]


def test_follow_rejects_incomplete_plan_and_other_users_signal(db_session):
    user = User(telegram_user_id=99002, total_capital=100_000)
    other = User(telegram_user_id=99003, total_capital=100_000)
    db_session.add_all([user, other])
    db_session.commit()
    source = _legacy_signal(db_session, key="legacy-incomplete")
    source.target_3 = None
    db_session.commit()
    with pytest.raises(SignalCommandError, match="TP1–TP3"):
        clone_analysis_signal_for_user(db_session, user, source, _settings(), now=NOW)

    source.target_3 = 49.40
    source.user_id = other.id
    db_session.commit()
    with pytest.raises(SignalCommandError, match="başka bir kullanıcı"):
        clone_analysis_signal_for_user(db_session, user, source, _settings(), now=NOW)
