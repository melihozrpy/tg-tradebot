from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.models.database import (
    Signal,
    SignalEvent,
    SignalStateEnum,
    SignalTarget,
    SignalTransitionErrorAudit,
    User,
)
from app.services.bist_signal_runtime_service import (
    BistSignalConfigurationError,
    BistSignalOwnershipError,
    BistSignalRuntimeService,
    BistSignalTransitionError,
    CreateBistSignalRequest,
)
from app.signals import (
    CandleObservation,
    EntryOrderType,
    ExecutionPolicy,
    FillModel,
    FillStatus,
    SignalEventType,
    SignalStatus,
    TradingState,
    TransactionCostModel,
)


BASE_TIME = datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc)


def _user(db, telegram_id: int = 7001) -> User:
    user = User(telegram_user_id=telegram_id, total_capital=100_000)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _request(user_id: int, *, suffix: str = "base", side: str = "BUY") -> CreateBistSignalRequest:
    return CreateBistSignalRequest(
        user_id=user_id,
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
        source="unit-create",
        strategy_version="bist-v1",
        score="84",
        confidence="84/100",
        idempotency_key=f"test-signal-{suffix}",
        side=side,
    )


def _candle(
    day: float,
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: str = "1000000",
    provider: str = "test-feed",
    **kwargs,
) -> CandleObservation:
    return CandleObservation(
        symbol="TEST",
        timestamp=BASE_TIME + timedelta(days=day),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        provider=provider,
        timeframe="1d",
        **kwargs,
    )


def test_sqlalchemy_enum_accepts_every_new_runtime_state():
    required = {
        "PENDING_ENTRY",
        "ACTIVE",
        "TP1_HIT",
        "TP2_HIT",
        "TP3_HIT",
        "STOPPED",
        "EXIT_PENDING",
        "UNFILLED",
        "SUSPENDED",
        "CORPORATE_ACTION_ADJUSTED",
    }
    assert required.issubset(SignalStateEnum.__members__)


def test_create_pending_long_signal_is_atomic_rounded_owned_and_idempotent(db_session):
    owner = _user(db_session)
    service = BistSignalRuntimeService(db_session)
    signal = service.create_pending_signal(_request(owner.id))

    assert signal.user_id == owner.id
    assert signal.side == "BUY"
    assert signal.state == SignalStateEnum.PENDING_ENTRY
    assert signal.signal_type.value == "BUY_CANDIDATE"
    assert signal.raw_planned_entry_price == Decimal("45.500000")
    assert signal.planned_entry_price == Decimal("45.500000")
    assert Decimal(str(signal.stop_price)) == Decimal("44.6")
    assert signal.current_stop_price == Decimal("44.600000")
    assert signal.invalidation_price == Decimal("44.600000")
    assert signal.source == "unit-create"
    assert signal.market_rule_version == "BIST_PAY_2026_01"
    assert signal.requested_quantity == Decimal("1000.0000")
    assert signal.filled_quantity == Decimal("0.0000")

    targets = db_session.query(SignalTarget).filter_by(signal_id=signal.id).order_by(SignalTarget.target_number).all()
    assert [row.target_number for row in targets] == [1, 2, 3]
    assert [row.target_price for row in targets] == [
        Decimal("47.000000"),
        Decimal("48.200000"),
        Decimal("49.400000"),
    ]
    assert [row.target_quantity for row in targets] == [
        Decimal("400.0000"),
        Decimal("350.0000"),
        Decimal("250.0000"),
    ]
    assert [row.allocation_percent for row in targets] == [
        Decimal("40.0000"),
        Decimal("35.0000"),
        Decimal("25.0000"),
    ]

    created = db_session.query(SignalEvent).filter_by(signal_id=signal.id).one()
    metadata = json.loads(created.metadata_json)
    assert created.event_type == SignalEventType.SIGNAL_CREATED.value
    assert created.unique_dedup_key
    assert created.provider == "creation-feed"
    assert created.source == "unit-create"
    assert metadata["raw_stop_price"] == "44.60"
    assert metadata["rounded_stop_price"] == "44.60"

    duplicate = service.create_pending_signal(_request(owner.id))
    assert duplicate.id == signal.id
    assert db_session.query(Signal).count() == 1
    assert db_session.query(SignalEvent).count() == 1
    assert db_session.query(SignalTarget).count() == 3

    with pytest.raises(BistSignalConfigurationError):
        service.create_pending_signal(_request(owner.id, suffix="sell", side="SELL"))

    rounded = service.create_pending_signal(
        replace(
            _request(owner.id, suffix="raw-rounded"),
            raw_entry_price="45.517",
            raw_stop_price="44.607",
            raw_target_prices=("47.019", "48.219", "49.419"),
        )
    )
    rounded_targets = (
        db_session.query(SignalTarget)
        .filter_by(signal_id=rounded.id)
        .order_by(SignalTarget.target_number)
        .all()
    )
    assert rounded.raw_planned_entry_price == Decimal("45.517000")
    assert rounded.planned_entry_price == Decimal("45.500000")
    assert rounded.invalidation_price == Decimal("44.607000")
    assert rounded.current_stop_price == Decimal("44.620000")
    assert [row.raw_target_price for row in rounded_targets] == [
        Decimal("47.019000"),
        Decimal("48.219000"),
        Decimal("49.419000"),
    ]
    assert [row.target_price for row in rounded_targets] == [
        Decimal("47.000000"),
        Decimal("48.200000"),
        Decimal("49.400000"),
    ]


def test_exact_4550_database_lifecycle_restart_costs_and_dedup(db_session):
    owner = _user(db_session, 7002)
    costs = TransactionCostModel(
        commission_rate=Decimal("0.001"),
        commission_tax_rate=Decimal("0.05"),
    )
    service = BistSignalRuntimeService(db_session, cost_model=costs)
    signal = service.create_pending_signal(_request(owner.id, suffix="acceptance"))
    signal_id = signal.id
    owner_id = owner.id

    below = _candle(1, open_="44.80", high="45.40", low="44.70", close="45.10")
    pending = service.process_observation(signal_id, owner.id, below, source="acceptance")
    assert pending.status == SignalStatus.PENDING_ENTRY
    assert not pending.applied

    entry_bar = _candle(2, open_="45.60", high="45.70", low="45.40", close="45.56")
    activated = service.process_observation(signal_id, owner.id, entry_bar, source="acceptance")
    assert activated.status == SignalStatus.ACTIVE
    assert activated.fill_status == FillStatus.FILLED
    db_session.expire_all()
    signal = db_session.get(Signal, signal_id)
    assert signal.actual_entry_price == Decimal("45.500000")
    assert signal.filled_quantity == Decimal("1000.0000")
    assert signal.remaining_quantity == Decimal("1000.0000")
    entry_event = db_session.query(SignalEvent).filter_by(
        signal_id=signal_id,
        event_type=SignalEventType.ENTRY_FILLED.value,
    ).one()
    evidence = json.loads(entry_event.metadata_json)["observation"]
    assert evidence == {
        "bar_complete": True,
        "available_buy_quantity": None,
        "available_sell_quantity": None,
        "close": "45.56",
        "delayed": False,
        "high": "45.70",
        "lower_limit": None,
        "lower_limit_locked": False,
        "low": "45.40",
        "open": "45.60",
        "provider": "test-feed",
        "safe_for_live_trigger": True,
        "symbol": "TEST",
        "timeframe": "1d",
        "timestamp": entry_bar.timestamp.isoformat(),
        "trading_state": "CONTINUOUS",
        "upper_limit": None,
        "upper_limit_locked": False,
        "valid_transaction": True,
        "volume": "1000000",
        "volume_ratio": None,
    }

    # A new Session is the persistence-level restart boundary.
    engine = db_session.get_bind()
    db_session.close()
    restarted_db = sessionmaker(bind=engine)()
    try:
        restarted = BistSignalRuntimeService(restarted_db, cost_model=costs)
        duplicate_entry = restarted.process_observation(signal_id, owner_id, entry_bar, source="acceptance")
        assert duplicate_entry.duplicate and not duplicate_entry.applied

        tp1 = restarted.process_observation(
            signal_id,
            owner_id,
            _candle(3, open_="46.80", high="47.00", low="46.70", close="46.90"),
            source="acceptance",
        )
        assert tp1.status == SignalStatus.TP1_HIT
        assert tp1.event_types == (SignalEventType.TP1_REACHED.value, SignalEventType.STOP_MOVED.value)
        signal = restarted_db.get(Signal, signal_id)
        assert signal.remaining_quantity == Decimal("600.0000")
        assert signal.current_stop_price == Decimal("45.500000")

        old = restarted.process_observation(
            signal_id,
            owner_id,
            _candle(2.5, open_="46.00", high="46.20", low="45.80", close="46.10"),
            source="acceptance",
        )
        assert old.out_of_order and not old.applied

        # Second restart must restore TP1 and its moved stop from the DB.
        restarted_db.close()
        restarted_db = sessionmaker(bind=engine)()
        restarted = BistSignalRuntimeService(restarted_db, cost_model=costs)
        restored = restarted.restore_lifecycle(restarted_db.get(Signal, signal_id), user_id=owner_id)
        assert restored.status == SignalStatus.TP1_HIT
        assert restored.has_event(tp1.event_ids[0] and restarted_db.get(SignalEvent, tp1.event_ids[0]).unique_dedup_key)

        tp2 = restarted.process_observation(
            signal_id,
            owner_id,
            _candle(4, open_="48.00", high="48.20", low="47.90", close="48.10"),
            source="acceptance",
        )
        assert tp2.status == SignalStatus.TP2_HIT
        signal = restarted_db.get(Signal, signal_id)
        assert signal.remaining_quantity == Decimal("250.0000")
        assert signal.current_stop_price == Decimal("47.000000")

        tp3_bar = _candle(5, open_="49.20", high="49.40", low="49.10", close="49.30")
        tp3 = restarted.process_observation(signal_id, owner_id, tp3_bar, source="acceptance")
        assert tp3.status == SignalStatus.TP3_HIT
        signal = restarted_db.get(Signal, signal_id)
        assert signal.remaining_quantity == Decimal("0.0000")
        assert signal.closed_at is not None

        targets = restarted_db.query(SignalTarget).filter_by(signal_id=signal_id).order_by(SignalTarget.target_number).all()
        assert [row.status for row in targets] == ["EXECUTED", "EXECUTED", "EXECUTED"]
        assert [row.realized_quantity for row in targets] == [
            Decimal("400.0000"),
            Decimal("350.0000"),
            Decimal("250.0000"),
        ]
        assert sum((row.gross_pnl for row in targets), Decimal("0")) == Decimal("2520.000000")
        assert sum((row.net_pnl for row in targets), Decimal("0")) == Decimal("2421.800000")

        events = restarted_db.query(SignalEvent).filter_by(signal_id=signal_id).all()
        event_types = [row.event_type for row in events]
        assert event_types.count(SignalEventType.ENTRY_FILLED.value) == 1
        assert event_types.count(SignalEventType.TP1_REACHED.value) == 1
        assert event_types.count(SignalEventType.TP2_REACHED.value) == 1
        assert event_types.count(SignalEventType.TP3_REACHED.value) == 1
        assert event_types.count(SignalEventType.STOP_MOVED.value) == 2
        keys = [row.unique_dedup_key for row in events]
        assert None not in keys and len(keys) == len(set(keys))
        triggered = [row for row in events if row.event_type != SignalEventType.SIGNAL_CREATED.value]
        assert all(row.provider == "test-feed" and row.source == "acceptance" for row in triggered)

        duplicate_tp3 = restarted.process_observation(signal_id, owner_id, tp3_bar, source="acceptance")
        assert duplicate_tp3.duplicate and not duplicate_tp3.applied
        assert restarted_db.query(SignalEvent).filter_by(signal_id=signal_id).count() == len(events)

        raw_state = restarted_db.execute(text("SELECT state FROM signals WHERE id=:id"), {"id": signal_id}).scalar_one()
        assert raw_state == "TP3_HIT"

        stranger = _user(restarted_db, 7999)
        with pytest.raises(BistSignalOwnershipError):
            restarted.process_observation(signal_id, stranger.id, _candle(6, open_="50", high="51", low="49", close="50"))
    finally:
        restarted_db.close()


def test_partial_entry_reallocates_targets_and_tavan_lock_is_terminal(db_session):
    owner = _user(db_session, 7003)
    service = BistSignalRuntimeService(db_session)
    partial_signal = service.create_pending_signal(_request(owner.id, suffix="partial"))
    partial = service.process_observation(
        partial_signal.id,
        owner.id,
        _candle(1, open_="45.60", high="45.70", low="45.40", close="45.56", volume="50000"),
        source="partial-test",
    )
    assert partial.status == SignalStatus.ACTIVE
    assert partial.fill_status == FillStatus.PARTIALLY_FILLED
    signal = db_session.get(Signal, partial_signal.id)
    assert signal.filled_quantity == Decimal("500.0000")
    assert signal.remaining_quantity == Decimal("500.0000")
    targets = db_session.query(SignalTarget).filter_by(signal_id=signal.id).order_by(SignalTarget.target_number).all()
    assert [row.target_quantity for row in targets] == [
        Decimal("200.0000"),
        Decimal("175.0000"),
        Decimal("125.0000"),
    ]

    locked_signal = service.create_pending_signal(_request(owner.id, suffix="tavan"))
    locked = service.process_observation(
        locked_signal.id,
        owner.id,
        _candle(
            2,
            open_="45.50",
            high="45.50",
            low="45.50",
            close="45.50",
            upper_limit="45.50",
            upper_limit_locked=True,
            available_sell_quantity="0",
        ),
        source="lock-test",
    )
    assert locked.status == SignalStatus.UNFILLED
    assert locked.fill_status == FillStatus.UNFILLED_LIMIT_LOCK


def test_unsafe_observation_is_not_consumed_and_corrected_same_timestamp_can_activate(db_session):
    owner = _user(db_session, 7007)
    service = BistSignalRuntimeService(db_session)
    signal = service.create_pending_signal(_request(owner.id, suffix="unsafe-correction"))
    unsafe = _candle(
        1,
        open_="45.60",
        high="45.70",
        low="45.40",
        close="45.56",
        is_delayed=True,
        safe_for_live_trigger=False,
    )
    skipped = service.process_observation(signal.id, owner.id, unsafe, source="freshness-test")
    assert skipped.fill_status == FillStatus.UNSAFE_DATA
    assert skipped.status == SignalStatus.PENDING_ENTRY
    stored_timestamp = db_session.get(Signal, signal.id).data_timestamp
    assert stored_timestamp.replace(tzinfo=timezone.utc) == BASE_TIME

    corrected = replace(unsafe, is_delayed=False, safe_for_live_trigger=True)
    activated = service.process_observation(signal.id, owner.id, corrected, source="freshness-test")
    assert activated.status == SignalStatus.ACTIVE
    assert activated.fill_status == FillStatus.FILLED


def test_suspension_resume_taban_pending_and_first_tradable_stop(db_session):
    owner = _user(db_session, 7004)
    full_policy = ExecutionPolicy(fill_model=FillModel.FULL_FILL)
    service = BistSignalRuntimeService(db_session, execution_policy=full_policy)
    signal = service.create_pending_signal(_request(owner.id, suffix="suspend-stop"))

    suspended = service.process_observation(
        signal.id,
        owner.id,
        _candle(
            1,
            open_="45.00",
            high="45.00",
            low="45.00",
            close="45.00",
            trading_state=TradingState.SUSPENDED,
            valid_transaction=False,
        ),
        source="runtime-test",
    )
    assert suspended.status == SignalStatus.SUSPENDED

    resumed = service.process_observation(
        signal.id,
        owner.id,
        _candle(2, open_="45.60", high="45.70", low="45.40", close="45.56"),
        source="runtime-test",
    )
    assert resumed.status == SignalStatus.ACTIVE
    assert resumed.event_types == (
        SignalEventType.TRADING_RESUMED.value,
        SignalEventType.ENTRY_FILLED.value,
    )

    taban = service.process_observation(
        signal.id,
        owner.id,
        _candle(
            3,
            open_="40.00",
            high="40.00",
            low="40.00",
            close="40.00",
            lower_limit="40.00",
            lower_limit_locked=True,
            available_buy_quantity="0",
        ),
        source="runtime-test",
    )
    assert taban.status == SignalStatus.EXIT_PENDING
    assert taban.fill_status == FillStatus.EXIT_PENDING_LIMIT_LOCK

    still_taban = service.process_observation(
        signal.id,
        owner.id,
        _candle(
            3.5,
            open_="40.00",
            high="40.00",
            low="40.00",
            close="40.00",
            lower_limit="40.00",
            lower_limit_locked=True,
            available_buy_quantity="0",
        ),
        source="runtime-test",
    )
    assert still_taban.status == SignalStatus.EXIT_PENDING
    assert still_taban.fill_status == FillStatus.EXIT_PENDING_LIMIT_LOCK
    assert not still_taban.applied

    first_tradable = service.process_observation(
        signal.id,
        owner.id,
        _candle(4, open_="39.50", high="40.20", low="39.40", close="40.00"),
        source="runtime-test",
    )
    assert first_tradable.status == SignalStatus.STOPPED
    stop_event = (
        db_session.query(SignalEvent)
        .filter_by(signal_id=signal.id, event_type=SignalEventType.STOP_EXECUTED.value)
        .one()
    )
    assert stop_event.execution_price == Decimal("39.500000")
    assert db_session.get(Signal, signal.id).remaining_quantity == Decimal("0.0000")


def test_partial_target_and_partial_stop_are_resumable(db_session):
    owner = _user(db_session, 7006)
    service = BistSignalRuntimeService(db_session)

    target_signal = service.create_pending_signal(_request(owner.id, suffix="partial-target"))
    service.process_observation(
        target_signal.id,
        owner.id,
        _candle(1, open_="45.60", high="45.70", low="45.40", close="45.56"),
        source="partial-runtime",
    )
    partial_target = service.process_observation(
        target_signal.id,
        owner.id,
        _candle(2, open_="46.80", high="47.00", low="46.70", close="46.90", volume="10000"),
        source="partial-runtime",
    )
    assert partial_target.status == SignalStatus.ACTIVE
    assert partial_target.fill_status == FillStatus.PARTIALLY_FILLED
    target = db_session.query(SignalTarget).filter_by(signal_id=target_signal.id, target_number=1).one()
    assert target.status == "PARTIALLY_FILLED"
    assert target.realized_quantity == Decimal("100.0000")
    assert db_session.get(Signal, target_signal.id).remaining_quantity == Decimal("900.0000")

    completed_target = service.process_observation(
        target_signal.id,
        owner.id,
        _candle(3, open_="46.90", high="47.10", low="46.80", close="47.00"),
        source="partial-runtime",
    )
    assert completed_target.status == SignalStatus.TP1_HIT
    db_session.expire_all()
    target = db_session.query(SignalTarget).filter_by(signal_id=target_signal.id, target_number=1).one()
    assert target.status == "EXECUTED"
    assert target.realized_quantity == Decimal("400.0000")
    assert target.gross_pnl == Decimal("600.000000")

    stop_signal = service.create_pending_signal(_request(owner.id, suffix="partial-stop"))
    service.process_observation(
        stop_signal.id,
        owner.id,
        _candle(1, open_="45.60", high="45.70", low="45.40", close="45.56"),
        source="partial-runtime",
    )
    partial_stop = service.process_observation(
        stop_signal.id,
        owner.id,
        _candle(2, open_="45.00", high="45.20", low="44.50", close="44.70", volume="50000"),
        source="partial-runtime",
    )
    assert partial_stop.status == SignalStatus.EXIT_PENDING
    assert partial_stop.fill_status == FillStatus.PARTIALLY_FILLED
    assert db_session.get(Signal, stop_signal.id).remaining_quantity == Decimal("500.0000")

    stopped = service.process_observation(
        stop_signal.id,
        owner.id,
        _candle(3, open_="44.00", high="44.20", low="43.80", close="44.10", volume="50000"),
        source="partial-runtime",
    )
    assert stopped.status == SignalStatus.STOPPED
    assert db_session.get(Signal, stop_signal.id).remaining_quantity == Decimal("0.0000")


def test_invalid_transition_raises_and_rollback_preserves_db(db_session):
    owner = _user(db_session, 7005)
    service = BistSignalRuntimeService(db_session)
    signal = service.create_pending_signal(_request(owner.id, suffix="invalid"))
    initial_events = db_session.query(SignalEvent).filter_by(signal_id=signal.id).count()
    lifecycle = service.restore_lifecycle(signal, user_id=owner.id)
    observation = _candle(1, open_="45", high="46", low="44", close="45")
    with pytest.raises(BistSignalTransitionError):
        service._transition(  # persistence boundary deliberately exercised
            signal,
            lifecycle,
            SignalStatus.TP2_HIT,
            SignalEventType.TP2_REACHED,
            observation,
            "invalid-test",
        )
    db_session.rollback()
    db_session.expire_all()
    assert db_session.get(Signal, signal.id).state == SignalStateEnum.PENDING_ENTRY
    assert db_session.query(SignalEvent).filter_by(signal_id=signal.id).count() == initial_events
    audit = db_session.query(SignalTransitionErrorAudit).filter_by(signal_id=signal.id).one()
    assert audit.user_id == owner.id
    assert audit.previous_status == SignalStatus.PENDING_ENTRY.value
    assert audit.attempted_status == SignalStatus.TP2_HIT.value
    assert audit.event_type == SignalEventType.TP2_REACHED.value
    assert audit.provider == "test-feed"
    assert audit.source == "invalid-test"
    assert audit.dedup_key

    # Restart/retry with the same immutable observation must not duplicate the
    # audit record and still must not mutate the Signal/SignalEvent rows.
    lifecycle = service.restore_lifecycle(db_session.get(Signal, signal.id), user_id=owner.id)
    with pytest.raises(BistSignalTransitionError):
        service._transition(
            db_session.get(Signal, signal.id),
            lifecycle,
            SignalStatus.TP2_HIT,
            SignalEventType.TP2_REACHED,
            observation,
            "invalid-test",
        )
    db_session.rollback()
    assert db_session.query(SignalTransitionErrorAudit).filter_by(signal_id=signal.id).count() == 1
    assert db_session.get(Signal, signal.id).state == SignalStateEnum.PENDING_ENTRY
    assert db_session.query(SignalEvent).filter_by(signal_id=signal.id).count() == initial_events


def test_manual_tracking_breakeven_and_close_are_owned_and_audited(db_session):
    owner = _user(db_session, 7010)
    service = BistSignalRuntimeService(db_session)
    signal = service.create_pending_signal(_request(owner.id, suffix="manual-actions"))
    service.set_monitoring(signal.id, owner.id, False)
    assert db_session.get(Signal, signal.id).monitoring_enabled is False
    service.set_monitoring(signal.id, owner.id, True)
    service.process_observation(
        signal.id, owner.id,
        _candle(1, open_="45.60", high="45.70", low="45.40", close="45.56"),
        source="manual-test",
    )
    moved = service.move_stop_to_breakeven(
        signal.id, owner.id, event_time=BASE_TIME + timedelta(days=2),
    )
    assert moved.event_type == SignalEventType.STOP_MOVED.value
    assert db_session.get(Signal, signal.id).current_stop_price == Decimal("45.500000")
    closed = service.close_manually(
        signal.id, owner.id, execution_price="46.20",
        event_time=BASE_TIME + timedelta(days=3), provider="licensed-feed",
    )
    assert closed.event_type == SignalEventType.POSITION_CLOSED_MANUALLY.value
    persisted = db_session.get(Signal, signal.id)
    assert persisted.state == SignalStateEnum.CLOSED_MANUALLY
    assert persisted.remaining_quantity == Decimal("0.0000")
    assert persisted.monitoring_enabled is False


def test_pending_signal_can_be_cancelled_but_not_by_another_user(db_session):
    owner = _user(db_session, 7011)
    other = _user(db_session, 7012)
    service = BistSignalRuntimeService(db_session)
    signal = service.create_pending_signal(_request(owner.id, suffix="cancel"))
    with pytest.raises(BistSignalOwnershipError):
        service.cancel_pending(signal.id, other.id, event_time=BASE_TIME + timedelta(days=1))
    event = service.cancel_pending(signal.id, owner.id, event_time=BASE_TIME + timedelta(days=1))
    assert event.event_type == SignalEventType.SIGNAL_CANCELLED.value
    assert db_session.get(Signal, signal.id).state == SignalStateEnum.CANCELLED


def test_valid_from_invalidation_and_breakout_confirmation_survive_restart(db_session):
    owner = _user(db_session, 7013)
    service = BistSignalRuntimeService(db_session)

    future = service.create_pending_signal(
        replace(
            _request(owner.id, suffix="valid-from"),
            valid_from=BASE_TIME + timedelta(days=2),
        )
    )
    early = service.process_observation(
        future.id,
        owner.id,
        _candle(1, open_="45.50", high="45.70", low="45.00", close="45.60"),
        source="validity-test",
    )
    assert early.status == SignalStatus.PENDING_ENTRY
    assert not early.applied
    assert db_session.get(Signal, future.id).data_timestamp.replace(tzinfo=timezone.utc) == BASE_TIME

    invalidated = service.create_pending_signal(_request(owner.id, suffix="invalidated-before-entry"))
    invalidation_result = service.process_observation(
        invalidated.id,
        owner.id,
        _candle(1, open_="44.80", high="45.20", low="44.50", close="44.70"),
        source="validity-test",
    )
    assert invalidation_result.status == SignalStatus.INVALIDATED
    assert invalidation_result.event_types == (SignalEventType.ENTRY_INVALIDATED.value,)
    assert db_session.get(Signal, invalidated.id).monitoring_enabled is False

    breakout = service.create_pending_signal(
        replace(
            _request(owner.id, suffix="breakout-next-open"),
            entry_order_type=EntryOrderType.BREAKOUT_BUY,
        )
    )
    confirming = _candle(
        1,
        open_="45.30",
        high="45.90",
        low="44.80",
        close="45.70",
    )
    confirmed = service.process_observation(
        breakout.id,
        owner.id,
        confirming,
        source="breakout-test",
    )
    assert confirmed.status == SignalStatus.PENDING_ENTRY
    assert confirmed.event_types == (SignalEventType.ENTRY_REACHED.value,)
    assert db_session.get(Signal, breakout.id).actual_entry_price is None

    engine = db_session.get_bind()
    owner_id = owner.id
    db_session.close()
    restarted_db = sessionmaker(bind=engine)()
    try:
        restarted = BistSignalRuntimeService(restarted_db)
        opened = restarted.process_observation(
                breakout.id,
                owner_id,
            _candle(2, open_="45.84", high="49.80", low="45.70", close="49.50"),
            source="breakout-test",
        )
        assert opened.status == SignalStatus.ACTIVE
        assert opened.event_types == (SignalEventType.ENTRY_FILLED.value,)
        stored = restarted_db.get(Signal, breakout.id)
        assert stored.actual_entry_price == Decimal("45.840000")
        # TP seviyeleri aynı giriş mumunda görülse bile kronoloji bilinmediği
        # için aynı observation içinde gerçekleşmiş sayılmaz.
        assert stored.remaining_quantity == Decimal("1000.0000")
    finally:
        restarted_db.close()
