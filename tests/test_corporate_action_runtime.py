from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.database import (
    CorporateActionRecord,
    MarketSessionEvent,
    Signal,
    SignalEvent,
    SignalStateEnum,
    SignalTarget,
    User,
)
from app.services.bist_signal_runtime_service import (
    BistSignalConfigurationError,
    BistSignalOwnershipError,
    BistSignalRuntimeService,
    CreateBistSignalRequest,
)
from app.signals import CandleObservation, EntryOrderType, SignalEventType, SignalStatus


BASE_TIME = datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc)


def _user(db, telegram_id: int) -> User:
    user = User(telegram_user_id=telegram_id, total_capital=100_000)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _signal(db, owner: User, suffix: str) -> Signal:
    return BistSignalRuntimeService(db).create_pending_signal(
        CreateBistSignalRequest(
            user_id=owner.id,
            symbol="TEST",
            timeframe="1d",
            creation_price="44.80",
            entry_order_type=EntryOrderType.LIMIT_BUY,
            raw_entry_price="45.50",
            raw_stop_price="44.60",
            raw_target_prices=("47.00", "48.20", "49.40"),
            requested_quantity=1000,
            created_at=BASE_TIME,
            data_timestamp=BASE_TIME,
            provider="creation-feed",
            source="corporate-action-test",
            strategy_version="bist-v1",
            idempotency_key=f"corporate-action-{suffix}",
        )
    )


def _bar(day: float, *, open_: str, high: str, low: str, close: str) -> CandleObservation:
    return CandleObservation(
        symbol="TEST",
        timestamp=BASE_TIME + timedelta(days=day),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume="1000000",
        provider="licensed-test-feed",
        timeframe="1d",
    )


def test_two_for_one_split_preserves_value_realized_amounts_and_monitor_state(db_session):
    owner = _user(db_session, 8101)
    service = BistSignalRuntimeService(db_session)
    signal = _signal(db_session, owner, "two-for-one")
    service.process_observation(
        signal.id,
        owner.id,
        _bar(1, open_="45.60", high="45.70", low="45.40", close="45.56"),
        source="corporate-action-test",
    )
    service.process_observation(
        signal.id,
        owner.id,
        _bar(2, open_="46.80", high="47.00", low="46.70", close="46.90"),
        source="corporate-action-test",
    )
    db_session.expire_all()
    before = db_session.get(Signal, signal.id)
    assert before.state == SignalStateEnum.TP1_HIT
    old_position_value = before.average_fill_price * before.filled_quantity
    old_row_version = before.row_version
    old_target = db_session.query(SignalTarget).filter_by(
        signal_id=signal.id, target_number=1
    ).one()
    old_realized_value = old_target.execution_price * old_target.realized_quantity
    old_pnl = (old_target.gross_pnl, old_target.costs, old_target.net_pnl)

    event = service.apply_corporate_action_adjustment(
        signal.id,
        owner.id,
        action_type="stock_split",
        adjustment_factor="2",
        effective_at=BASE_TIME + timedelta(days=3),
        provider="kap-licensed",
        source="corporate-action-worker",
        corporate_action_key="KAP:TEST:2026-01-08:SPLIT-2",
    )
    db_session.expire_all()
    adjusted = db_session.get(Signal, signal.id)
    targets = db_session.query(SignalTarget).filter_by(signal_id=signal.id).order_by(
        SignalTarget.target_number
    ).all()

    assert event.event_type == SignalEventType.CORPORATE_ACTION_APPLIED.value
    assert event.from_state == SignalStatus.TP1_HIT.value
    assert event.to_state == SignalStatus.TP1_HIT.value
    assert event.provider == "kap-licensed"
    assert adjusted.state == SignalStateEnum.TP1_HIT
    assert adjusted.monitoring_enabled is True
    assert adjusted.row_version == old_row_version + 1
    assert adjusted.price_adjustment_mode == "split_adjusted"
    assert adjusted.raw_planned_entry_price == Decimal("22.750000")
    # 22.75 is not on the current 0.02 BIST tick; the executable buy level is
    # conservatively rounded while the raw economic level stays exact.
    assert adjusted.planned_entry_price == Decimal("22.740000")
    assert adjusted.average_fill_price == Decimal("22.750000")
    assert adjusted.filled_quantity == Decimal("2000.0000")
    assert adjusted.remaining_quantity == Decimal("1200.0000")
    assert adjusted.average_fill_price * adjusted.filled_quantity == old_position_value
    assert [row.target_quantity for row in targets] == [
        Decimal("800.0000"),
        Decimal("700.0000"),
        Decimal("500.0000"),
    ]
    assert targets[0].execution_price == Decimal("23.500000")
    assert targets[0].realized_quantity == Decimal("800.0000")
    assert targets[0].execution_price * targets[0].realized_quantity == old_realized_value
    assert (targets[0].gross_pnl, targets[0].costs, targets[0].net_pnl) == old_pnl

    metadata = json.loads(event.metadata_json)
    assert metadata["adjustment_factor"] == "2"
    assert metadata["resume_status"] == SignalStatus.TP1_HIT.value
    action_record = db_session.query(CorporateActionRecord).one()
    assert action_record.symbol == "TEST"
    assert action_record.corporate_action_type == "STOCK_SPLIT"
    assert Decimal(str(action_record.adjustment_factor)) == Decimal("2")
    assert metadata["corporate_action_record_id"] == action_record.id
    assert json.loads(metadata["before_snapshot_json"])["status"] == SignalStatus.TP1_HIT.value
    assert json.loads(metadata["after_snapshot_json"])["remaining_quantity"] == "1200.0000"

    # The state-preserving event must not strand monitoring: the next adjusted
    # TP can transition through the normal runtime immediately.
    tp2 = service.process_observation(
        signal.id,
        owner.id,
        _bar(4, open_="24.00", high="24.10", low="23.90", close="24.06"),
        source="corporate-action-test",
    )
    assert tp2.status == SignalStatus.TP2_HIT


def test_corporate_action_retry_after_restart_is_exactly_once(db_session):
    owner = _user(db_session, 8102)
    signal = _signal(db_session, owner, "restart")
    service = BistSignalRuntimeService(db_session)
    kwargs = dict(
        action_type="bonus",
        adjustment_factor="2",
        effective_at=BASE_TIME + timedelta(days=1),
        provider="kap-licensed",
        source="corporate-action-worker",
        corporate_action_key="KAP:TEST:2026-01-06:BONUS-100",
    )
    first = service.apply_corporate_action_adjustment(signal.id, owner.id, **kwargs)
    signal_id, owner_id, event_id = signal.id, owner.id, first.id
    engine = db_session.get_bind()
    db_session.close()

    restarted_db = sessionmaker(bind=engine)()
    try:
        restarted = BistSignalRuntimeService(restarted_db)
        second = restarted.apply_corporate_action_adjustment(signal_id, owner_id, **kwargs)
        persisted = restarted_db.get(Signal, signal_id)
        assert second.id == event_id
        assert persisted.requested_quantity == Decimal("2000.0000")
        assert persisted.raw_planned_entry_price == Decimal("22.750000")
        assert (
            restarted_db.query(SignalEvent)
            .filter_by(signal_id=signal_id, event_type=SignalEventType.CORPORATE_ACTION_APPLIED.value)
            .count()
            == 1
        )
    finally:
        restarted_db.close()


def test_corporate_action_rejects_invalid_factor_fractional_lot_and_wrong_owner(db_session):
    owner = _user(db_session, 8103)
    stranger = _user(db_session, 8104)
    signal = _signal(db_session, owner, "validation")
    service = BistSignalRuntimeService(db_session)
    base = dict(
        action_type="stock_split",
        effective_at=BASE_TIME + timedelta(days=1),
        provider="kap-licensed",
        source="corporate-action-worker",
        corporate_action_key="KAP:TEST:VALIDATION",
    )
    for factor in ("0", "-2", "1", "0.5"):
        with pytest.raises(BistSignalConfigurationError):
            service.apply_corporate_action_adjustment(
                signal.id, owner.id, adjustment_factor=factor, **base
            )
    with pytest.raises(BistSignalOwnershipError):
        service.apply_corporate_action_adjustment(
            signal.id, stranger.id, adjustment_factor="2", **base
        )

    # A reverse split whose allocations would create fractional lots is
    # rejected until the normalized source supplies a cash/entitlement rule.
    with pytest.raises(BistSignalConfigurationError, match="tam BIST lotuna"):
        service.apply_corporate_action_adjustment(
            signal.id,
            owner.id,
            action_type="reverse_split",
            adjustment_factor="0.33",
            effective_at=BASE_TIME + timedelta(days=1),
            provider="kap-licensed",
            source="corporate-action-worker",
            corporate_action_key="KAP:TEST:FRACTIONAL-REVERSE",
        )
    assert db_session.get(Signal, signal.id).requested_quantity == Decimal("1000.0000")
    assert (
        db_session.query(SignalEvent)
        .filter_by(signal_id=signal.id, event_type=SignalEventType.CORPORATE_ACTION_APPLIED.value)
        .count()
        == 0
    )


def test_market_session_event_model_persists_provider_event_exactly_once(db_session):
    row = MarketSessionEvent(
        symbol="THYAO",
        event_type="CIRCUIT_BREAKER_STARTED",
        started_at=BASE_TIME,
        source="licensed-market-state",
        metadata_json=json.dumps({"trading_state": "CIRCUIT_BREAKER"}),
        unique_dedup_key="licensed:THYAO:2026-01-05T07:00:00Z:circuit-breaker",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    assert row.id is not None
    assert row.ended_at is None

    db_session.add(
        MarketSessionEvent(
            symbol="THYAO",
            event_type="CIRCUIT_BREAKER_STARTED",
            started_at=BASE_TIME,
            source="licensed-market-state",
            unique_dedup_key=row.unique_dedup_key,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    assert db_session.query(MarketSessionEvent).count() == 1
