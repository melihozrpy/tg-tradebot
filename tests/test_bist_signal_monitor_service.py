from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.database import Signal, SignalEvent, SignalEventDelivery, SignalStateEnum, User
from app.services.bist_signal_monitor_service import (
    _claim_signal_event_delivery,
    deliver_signal_event_outbox,
    enqueue_missing_signal_event_deliveries,
    quote_to_signal_observation,
    run_signal_monitor_cycle,
)
from app.services.bist_signal_runtime_service import (
    BistSignalRuntimeService,
    CreateBistSignalRequest,
)
from app.signals import CandleObservation, EntryOrderType, SignalEventType, TradingState


BASE = datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc)


def _settings():
    return SimpleNamespace(
        backtest_fill_model="conservative_volume_limited",
        max_daily_volume_participation_percent=1.0,
        backtest_limit_lock_mode="conservative",
        allow_delayed_data_for_live_trigger=False,
        max_market_data_staleness_seconds=30,
        move_stop_to_breakeven_after_tp1=True,
        move_stop_to_tp1_after_tp2=True,
        user_price_alert_max_global_deliveries_per_minute=500,
    )


def test_sqlite_fixture_enforces_foreign_keys(db_session):
    assert db_session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def _signal(db, telegram_id: int = 9901, suffix: str = "one", expires_at=None):
    user = db.query(User).filter_by(telegram_user_id=telegram_id).one_or_none()
    if user is None:
        user = User(telegram_user_id=telegram_id, total_capital=100_000)
        db.add(user); db.commit(); db.refresh(user)
    signal = BistSignalRuntimeService(db).create_pending_signal(CreateBistSignalRequest(
        user_id=user.id,
        symbol="THYAO",
        timeframe="tick",
        creation_price="44.80",
        entry_order_type=EntryOrderType.LIMIT_BUY,
        raw_entry_price="45.50",
        raw_stop_price="44.60",
        raw_target_prices=("47.00", "48.20", "49.40"),
        requested_quantity=1000,
        created_at=BASE,
        data_timestamp=BASE,
        provider="licensed-feed",
        source="test-create",
        strategy_version="ultra-v1",
        idempotency_key=f"monitor-{suffix}",
        expires_at=expires_at,
    ))
    return user, signal


class _Provider:
    def __init__(self, timestamp, price=45.50):
        self.timestamp = timestamp
        self.price = price
        self.calls = 0

    def is_market_open(self):
        return True

    def get_quote(self, symbol):
        self.calls += 1
        return {
            "symbol": symbol,
            "price": self.price,
            "timestamp": self.timestamp,
            "provider": "licensed-feed",
            "is_live": True,
            "is_fresh": True,
            "valid_transaction": True,
            "market_open": True,
            "trading_state": "CONTINUOUS",
            "last_trade_quantity": 1_000_000,
            "available_sell_quantity": 1_000_000,
            "available_buy_quantity": 1_000_000,
        }


def test_signal_monitor_groups_quote_and_persists_idempotent_outbox(db_session):
    _, first = _signal(db_session, suffix="first")
    _, second = _signal(db_session, suffix="second")
    now = BASE + timedelta(seconds=10)
    provider = _Provider(now)
    result = run_signal_monitor_cycle(db_session, provider, _settings(), now=now)
    assert result["signals"] == 2
    assert result["symbols_fetched"] == 1
    assert result["updated"] == 2
    assert result["queued"] == 2
    assert provider.calls == 1
    assert db_session.query(SignalEventDelivery).count() == 2

    duplicate = run_signal_monitor_cycle(db_session, provider, _settings(), now=now + timedelta(seconds=1))
    assert duplicate["updated"] == 0
    assert duplicate["queued"] == 0
    assert db_session.query(SignalEventDelivery).count() == 2


def test_signal_monitor_rejects_stale_quote_without_state_change(db_session):
    _, signal = _signal(db_session, suffix="stale")
    now = BASE + timedelta(minutes=5)
    result = run_signal_monitor_cycle(db_session, _Provider(BASE), _settings(), now=now)
    db_session.refresh(signal)
    assert result["rejected"] == 1
    assert signal.state.value == "PENDING_ENTRY"
    assert db_session.query(SignalEventDelivery).count() == 0


def test_live_quote_is_not_mislabeled_as_completed_breakout_candle():
    now = BASE + timedelta(seconds=10)
    quote = _Provider(now).get_quote("THYAO")
    observation = quote_to_signal_observation("THYAO", quote, _settings(), now)
    assert observation.valid_transaction is True
    assert observation.is_complete is False


@pytest.mark.asyncio
async def test_signal_event_outbox_is_delivered_once(db_session):
    _, signal = _signal(db_session, suffix="delivery")
    now = BASE + timedelta(seconds=10)
    run_signal_monitor_cycle(db_session, _Provider(now), _settings(), now=now)

    class Bot:
        def __init__(self):
            self.messages = []

        async def send_message(self, **kwargs):
            self.messages.append(kwargs)
            return SimpleNamespace(message_id=77)

    bot = Bot()
    result = await deliver_signal_event_outbox(SimpleNamespace(bot=bot), db_session, _settings(), now=now)
    assert result["sent"] == 1
    row = db_session.query(SignalEventDelivery).one()
    assert row.status == "SENT"
    assert row.telegram_message_id == 77
    assert "GİRİŞ GERÇEKLEŞTİ" in bot.messages[0]["text"]
    again = await deliver_signal_event_outbox(SimpleNamespace(bot=bot), db_session, _settings(), now=now)
    assert again["due"] == 0
    assert len(bot.messages) == 1


def test_due_pending_signal_expires_while_market_closed_and_restart_is_idempotent(db_session):
    expires_at = BASE + timedelta(minutes=1)
    user, signal = _signal(
        db_session,
        telegram_id=5_200_119_302,
        suffix="expiry-restart",
        expires_at=expires_at,
    )
    signal.monitoring_enabled = False
    db_session.commit()

    class ClosedProvider:
        def __init__(self):
            self.calls = 0

        def is_market_open(self):
            self.calls += 1
            return False

    provider = ClosedProvider()
    now = expires_at + timedelta(seconds=1)
    first = run_signal_monitor_cycle(db_session, provider, _settings(), now=now)
    db_session.expire_all()
    persisted = db_session.get(Signal, signal.id)
    assert first["expired"] == 1
    assert first["queued"] == 1
    assert provider.calls == 0
    assert persisted.state == SignalStateEnum.EXPIRED
    assert persisted.monitoring_enabled is False
    assert persisted.source == "bist_signal_monitor"
    events = db_session.query(SignalEvent).filter_by(
        signal_id=signal.id,
        event_type=SignalEventType.SIGNAL_EXPIRED.value,
    ).all()
    assert len(events) == 1
    assert events[0].provider == "system_clock"
    assert events[0].source == "bist_signal_monitor"
    assert db_session.query(SignalEventDelivery).filter_by(signal_event_id=events[0].id).count() == 1

    signal_id = signal.id
    user_id = user.id
    engine = db_session.get_bind()
    db_session.close()
    restarted = sessionmaker(bind=engine)()
    try:
        second = run_signal_monitor_cycle(restarted, provider, _settings(), now=now + timedelta(minutes=1))
        assert second["expired"] == 0
        assert second["queued"] == 0
        assert restarted.query(SignalEvent).filter_by(
            signal_id=signal_id,
            event_type=SignalEventType.SIGNAL_EXPIRED.value,
        ).count() == 1
        assert restarted.query(SignalEventDelivery).count() == 1
        assert restarted.get(User, user_id).telegram_user_id == 5_200_119_302
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_restart_reconciles_monitor_and_manual_events_without_duplicate_delivery(db_session):
    user, signal = _signal(db_session, suffix="monitor-crash-window")
    now = BASE + timedelta(seconds=10)
    BistSignalRuntimeService(db_session).process_observation(
        signal.id,
        user.id,
        CandleObservation(
            symbol="THYAO",
            timestamp=now,
            open="45.50",
            high="45.50",
            low="45.50",
            close="45.50",
            volume="1000000",
            timeframe="tick",
            provider="licensed-feed",
            is_session_open=True,
            valid_transaction=True,
            trading_state=TradingState.CONTINUOUS,
        ),
        source="bist_signal_monitor",
    )
    assert db_session.query(SignalEventDelivery).count() == 0

    _, manual_signal = _signal(db_session, suffix="manual-crash-window")
    BistSignalRuntimeService(db_session).cancel_pending(
        manual_signal.id,
        user.id,
        event_time=now,
        source="telegram_manual",
    )
    assert db_session.query(SignalEventDelivery).count() == 0

    engine = db_session.get_bind()
    db_session.close()
    restarted = sessionmaker(bind=engine)()

    class Bot:
        def __init__(self):
            self.messages = []

        async def send_message(self, **kwargs):
            self.messages.append(kwargs)
            return SimpleNamespace(message_id=900 + len(self.messages))

    bot = Bot()
    try:
        result = await deliver_signal_event_outbox(
            SimpleNamespace(bot=bot), restarted, _settings(), now=now + timedelta(seconds=1)
        )
        assert result["recovered"] == 2
        assert result["sent"] == 2
        assert len(bot.messages) == 2
        assert restarted.query(SignalEventDelivery).count() == 2

        again = await deliver_signal_event_outbox(
            SimpleNamespace(bot=bot), restarted, _settings(), now=now + timedelta(seconds=2)
        )
        assert again["recovered"] == 0
        assert again["due"] == 0
        assert len(bot.messages) == 2
    finally:
        restarted.close()


def test_wide_candle_target_payloads_use_each_event_remaining_quantity_snapshot(db_session):
    user, signal = _signal(db_session, suffix="wide-target-snapshot")
    runtime = BistSignalRuntimeService(db_session)
    runtime.process_observation(
        signal.id,
        user.id,
        CandleObservation(
            symbol="THYAO", timestamp=BASE + timedelta(seconds=1),
            open="45.50", high="45.60", low="45.40", close="45.50",
            volume="1000000", timeframe="tick", provider="licensed-feed",
            is_session_open=True, valid_transaction=True,
            trading_state=TradingState.CONTINUOUS,
        ),
        source="bist_signal_monitor",
    )
    runtime.process_observation(
        signal.id,
        user.id,
        CandleObservation(
            symbol="THYAO", timestamp=BASE + timedelta(seconds=2),
            open="46.80", high="50.00", low="46.70", close="49.50",
            volume="1000000", timeframe="tick", provider="licensed-feed",
            is_session_open=True, valid_transaction=True,
            trading_state=TradingState.CONTINUOUS,
        ),
        source="bist_signal_monitor",
    )
    assert enqueue_missing_signal_event_deliveries(db_session, now=BASE + timedelta(seconds=3)) == 6
    target_rows = (
        db_session.query(SignalEventDelivery, SignalEvent)
        .join(SignalEvent, SignalEvent.id == SignalEventDelivery.signal_event_id)
        .filter(SignalEvent.event_type.in_([
            SignalEventType.TP1_REACHED.value,
            SignalEventType.TP2_REACHED.value,
            SignalEventType.TP3_REACHED.value,
        ]))
        .order_by(SignalEvent.id)
        .all()
    )
    assert [event.event_type for _, event in target_rows] == [
        SignalEventType.TP1_REACHED.value,
        SignalEventType.TP2_REACHED.value,
        SignalEventType.TP3_REACHED.value,
    ]
    assert ["Kalan lot: 600" in delivery.payload_text for delivery, _ in target_rows] == [True, False, False]
    assert ["Kalan lot: 250" in delivery.payload_text for delivery, _ in target_rows] == [False, True, False]
    assert ["Kalan lot: 0" in delivery.payload_text for delivery, _ in target_rows] == [False, False, True]


@pytest.mark.asyncio
async def test_stale_sending_lease_is_recovered_once(db_session):
    _, signal = _signal(db_session, suffix="stale-sending")
    now = BASE + timedelta(seconds=10)
    run_signal_monitor_cycle(db_session, _Provider(now), _settings(), now=now)
    row = db_session.query(SignalEventDelivery).one()
    row.status = "SENDING"
    row.attempted_at = now - timedelta(minutes=6)
    row.attempt_count = 1
    db_session.commit()

    class Bot:
        def __init__(self):
            self.messages = []

        async def send_message(self, **kwargs):
            self.messages.append(kwargs)
            return SimpleNamespace(message_id=88)

    bot = Bot()
    result = await deliver_signal_event_outbox(
        SimpleNamespace(bot=bot), db_session, _settings(), now=now
    )
    db_session.refresh(row)
    assert result["claimed"] == 1
    assert result["sent"] == 1
    assert row.status == "SENT"
    assert row.attempt_count == 2
    assert len(bot.messages) == 1


def test_delivery_claim_is_compare_and_set_idempotent_across_sessions(db_session):
    _signal(db_session, suffix="atomic-claim")
    now = BASE + timedelta(seconds=10)
    run_signal_monitor_cycle(db_session, _Provider(now), _settings(), now=now)
    delivery_id = db_session.query(SignalEventDelivery.id).scalar()
    engine = db_session.get_bind()
    first_db = sessionmaker(bind=engine)()
    second_db = sessionmaker(bind=engine)()
    try:
        # Both workers can discover the same due id, but only one status-CAS
        # update may move it to SENDING.
        assert first_db.get(SignalEventDelivery, delivery_id).status == "PENDING"
        assert second_db.get(SignalEventDelivery, delivery_id).status == "PENDING"
        assert _claim_signal_event_delivery(first_db, delivery_id, now) is not None
        second_db.expire_all()
        assert _claim_signal_event_delivery(second_db, delivery_id, now) is None
        second_db.expire_all()
        persisted = second_db.get(SignalEventDelivery, delivery_id)
        assert persisted.status == "SENDING"
        assert persisted.attempt_count == 1
    finally:
        first_db.close()
        second_db.close()
