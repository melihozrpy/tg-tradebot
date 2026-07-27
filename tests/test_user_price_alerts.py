from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.alerts.delivery import _claim_alarm_delivery, deliver_alarm_outbox
from app.alerts.enums import AlarmCondition, AlarmMode, AlarmStatus
from app.alerts.evaluator import evaluate_price_alarm
from app.alerts.monitor import run_alarm_monitor_cycle
from app.alerts.parser import parse_alarm_line, parse_bulk_text, parse_decimal
from app.alerts.schemas import AlarmDraft, PriceObservation
from app.alerts.service import (
    DuplicateAlarmError,
    acknowledge_alarm,
    confirm_import,
    create_alarm,
    create_import_preview,
)
from app.models.database import (
    AlarmImportJob,
    PriceAlertDelivery,
    PriceAlertTrigger,
    User,
    UserPriceAlert,
)


def _user(db, telegram_id: int = 1001) -> User:
    value = User(telegram_user_id=telegram_id, total_capital=100_000)
    db.add(value)
    db.commit()
    db.refresh(value)
    return value


def _draft(
    symbol: str = "THYAO",
    target: str = "9.20",
    condition: AlarmCondition = AlarmCondition.PRICE_GTE,
) -> AlarmDraft:
    return AlarmDraft(symbol, Decimal(target), condition, AlarmMode.PERSISTENT, 60)


def _settings(**overrides):
    values = {
        "timezone_name": "Europe/Istanbul",
        "user_price_alert_stale_after_seconds": 180,
        "user_price_alert_max_global_deliveries_per_minute": 500,
        "user_price_alert_max_deliveries_per_minute_per_user": 20,
        "user_price_alert_audio_enabled": False,
        "app_env": "test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _OpenProvider:
    def is_market_open(self):
        return True


def _price_result(symbol: str, price: float, timestamp: datetime, *, live=True, fallback=False):
    return SimpleNamespace(
        symbol=symbol,
        current_price=price,
        current_price_timestamp=timestamp,
        current_price_source="licensed-test-feed",
        is_live_price=live,
        fallback_used=fallback,
    )


def test_turkish_parser_supports_single_and_bulk_rows():
    value = parse_alarm_line("ASELS 72,50 üstü")
    assert value.symbol == "ASELS"
    assert value.target_price == Decimal("72.5000")
    assert value.condition is AlarmCondition.PRICE_GTE
    parsed = parse_bulk_text(
        "hisse;fiyat;koşul\nTHYAO;285;altı\nEREGL;31,20;yukarı_kes\nEREGL;31,20;yukarı_kes"
    )
    assert [item.symbol for item in parsed.valid] == ["THYAO", "EREGL"]
    assert parsed.duplicate_rows == (4,)
    assert not parsed.invalid


def test_percentage_alarm_calculates_target_and_survives_import(db_session):
    parsed = parse_bulk_text("ASELS 100 +5% ciro sonrası")
    draft = parsed.valid[0]
    assert draft.condition is AlarmCondition.PERCENT_UP_FROM_BASE
    assert draft.base_price == Decimal("100.0000")
    assert draft.percentage_value == Decimal("5.0000")
    assert draft.target_price == Decimal("105.0000")
    owner = _user(db_session)
    job = create_import_preview(db_session, owner, 1001, parsed, "TEXT")
    created = confirm_import(db_session, owner, job.public_id)
    assert created[0].base_price == Decimal("100.000000")
    assert created[0].percentage_value == Decimal("5.000000")


def test_import_preserves_selected_alarm_sound(db_session):
    owner = _user(db_session)
    parsed = SimpleNamespace(
        valid=(AlarmDraft(
            "THYAO", Decimal("300"), AlarmCondition.PRICE_GTE,
            sound_name="radar",
        ),),
        invalid=(),
        duplicate_rows=(),
    )
    job = create_import_preview(db_session, owner, 1001, parsed, "TEXT")
    created = confirm_import(db_session, owner, job.public_id)
    assert created[0].sound_name == "radar"


@pytest.mark.parametrize("raw, expected", [("9,20", "9.2000"), ("1.234,56", "1234.5600"), ("1,234.56", "1234.5600")])
def test_decimal_parser_is_locale_safe(raw, expected):
    assert parse_decimal(raw) == Decimal(expected)


def test_alarm_evaluator_is_decimal_based_and_fail_closed_for_stale_data():
    now = datetime(2026, 7, 24, 9, 20, tzinfo=timezone.utc)
    alert = SimpleNamespace(
        condition_type="CROSS_UP",
        target_price=Decimal("9.20"),
        last_observed_price=Decimal("9.19"),
        previous_valid_price=None,
        near_tolerance=None,
        base_price=None,
        percentage_value=None,
        reset_band_value=None,
        status="ACTIVE",
    )
    current = PriceObservation("THYAO", Decimal("9.21"), "licensed", now, now, 0, True, False)
    assert evaluate_price_alarm(alert, current).triggered
    stale = PriceObservation("THYAO", Decimal("9.21"), "licensed", now, now, 181, True, False)
    decision = evaluate_price_alarm(alert, stale, stale_after_seconds=180)
    assert not decision.triggered
    assert decision.state_key == "STALE_REJECTED"


def test_create_alarm_is_user_owned_and_duplicate_safe(db_session):
    owner = _user(db_session, 111)
    other = _user(db_session, 222)
    created = create_alarm(db_session, owner, 111, _draft())
    assert created.user_id == owner.id
    with pytest.raises(DuplicateAlarmError):
        create_alarm(db_session, owner, 111, _draft())
    second = create_alarm(db_session, other, 222, _draft())
    assert second.user_id == other.id


def test_bulk_import_is_preview_first_and_confirmation_is_idempotent(db_session):
    owner = _user(db_session)
    parsed = parse_bulk_text("ASELS;72,50;üstü\nTHYAO;285;altı\nBOZUK")
    job = create_import_preview(db_session, owner, 1001, parsed, "TEXT")
    assert job.status == "PREVIEW"
    assert db_session.query(UserPriceAlert).count() == 0
    created = confirm_import(db_session, owner, job.public_id)
    assert len(created) == 2
    assert db_session.query(UserPriceAlert).count() == 2
    assert len(confirm_import(db_session, owner, job.public_id)) == 2
    assert db_session.query(AlarmImportJob).filter_by(public_id=job.public_id).one().status == "CONFIRMED"


def test_monitor_fetches_each_symbol_once_and_does_not_duplicate_pending_delivery(db_session, monkeypatch):
    owner = _user(db_session)
    create_alarm(db_session, owner, 1001, _draft("THYAO", "9.20", AlarmCondition.PRICE_GTE))
    create_alarm(db_session, owner, 1001, _draft("THYAO", "10.50", AlarmCondition.PRICE_LTE))
    now = datetime(2026, 7, 24, 11, 0, tzinfo=timezone.utc)
    calls = []

    def resolve(_provider, symbol, **_kwargs):
        calls.append(symbol)
        return _price_result(symbol, 10.0, now)

    monkeypatch.setattr("app.alerts.monitor.resolve_current_price", resolve)
    first = run_alarm_monitor_cycle(db_session, _OpenProvider(), _settings(), now=now)
    assert first["triggered"] == 2
    assert calls == ["THYAO"]
    assert db_session.query(PriceAlertTrigger).count() == 2
    assert db_session.query(PriceAlertDelivery).count() == 2

    second = run_alarm_monitor_cycle(db_session, _OpenProvider(), _settings(), now=now + timedelta(seconds=30))
    assert second["repeats_queued"] == 0
    assert db_session.query(PriceAlertDelivery).count() == 2


def test_monitor_rejects_delayed_fallback_price(db_session, monkeypatch):
    owner = _user(db_session)
    alert = create_alarm(db_session, owner, 1001, _draft())
    now = datetime(2026, 7, 24, 11, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "app.alerts.monitor.resolve_current_price",
        lambda *_args, **_kwargs: _price_result("THYAO", 9.50, now, live=False, fallback=True),
    )
    result = run_alarm_monitor_cycle(db_session, _OpenProvider(), _settings(), now=now)
    db_session.refresh(alert)
    assert result["rejected"] == 1
    assert alert.status == AlarmStatus.ACTIVE.value
    assert db_session.query(PriceAlertTrigger).count() == 0


def test_thousand_alerts_are_grouped_into_one_fetch_per_symbol(db_session, monkeypatch):
    owner = _user(db_session)
    rows = []
    for index in range(1000):
        symbol = f"T{index % 50:02d}X"
        rows.append(UserPriceAlert(
            public_id=f"ALR-{index:06d}", user_id=owner.id,
            telegram_user_id=owner.telegram_user_id, chat_id=1001,
            symbol=symbol, normalized_symbol=symbol, condition_type="PRICE_GTE",
            target_price=Decimal("999"), status="ACTIVE", mode="PERSISTENT",
            repeat_interval_seconds=60, sound_mode="TEXT_ONLY", market_hours_only=False,
        ))
    db_session.add_all(rows)
    db_session.commit()
    now = datetime(2026, 7, 24, 11, 0, tzinfo=timezone.utc)
    calls = []

    def resolve(_provider, symbol, **_kwargs):
        calls.append(symbol)
        return _price_result(symbol, 10.0, now)

    monkeypatch.setattr("app.alerts.monitor.resolve_current_price", resolve)
    result = run_alarm_monitor_cycle(db_session, _OpenProvider(), _settings(), now=now)
    assert result["alerts"] == 1000
    assert result["symbols_fetched"] == 50
    assert len(calls) == 50
    assert result["triggered"] == 0


@pytest.mark.asyncio
async def test_delivery_outbox_marks_text_sent_even_when_audio_fails(db_session):
    owner = _user(db_session)
    alert = create_alarm(db_session, owner, 1001, _draft())
    now = datetime.now(timezone.utc)
    alert.status = AlarmStatus.TRIGGERED.value
    alert.sound_mode = "FIRST_TRIGGER"
    trigger = PriceAlertTrigger(
        alert_id=alert.id,
        trigger_sequence=1,
        triggered_price=Decimal("9.20"),
        target_price_snapshot=Decimal("9.20"),
        condition_type_snapshot="PRICE_GTE",
        detected_at=now,
        data_timestamp=now,
        provider="licensed-test-feed",
        freshness_seconds=0,
        idempotency_key="trigger-1",
    )
    db_session.add(trigger)
    db_session.flush()
    delivery = PriceAlertDelivery(
        trigger_id=trigger.id,
        alert_id=alert.id,
        telegram_user_id=owner.telegram_user_id,
        chat_id=1001,
        scheduled_for=now,
        idempotency_key="delivery-1",
    )
    db_session.add(delivery)
    db_session.commit()

    class Bot:
        async def send_message(self, **_kwargs):
            return SimpleNamespace(message_id=42)

        async def send_audio(self, **_kwargs):
            from telegram.error import NetworkError

            raise NetworkError("test")

    result = await deliver_alarm_outbox(
        SimpleNamespace(bot=Bot()),
        db_session,
        _settings(user_price_alert_audio_enabled=True),
        now=now,
    )
    db_session.refresh(delivery)
    assert result["sent"] == 1
    assert delivery.status == "SENT"
    assert delivery.telegram_message_id == 42
    assert delivery.error_code == "AUDIO_DELIVERY_ERROR"


@pytest.mark.asyncio
async def test_local_audio_generation_failure_never_retries_sent_text(db_session, monkeypatch):
    owner = _user(db_session)
    alert = create_alarm(db_session, owner, 1001, _draft())
    now = datetime.now(timezone.utc)
    alert.status = AlarmStatus.TRIGGERED.value
    alert.sound_mode = "FIRST_TRIGGER"
    trigger = PriceAlertTrigger(
        alert_id=alert.id,
        trigger_sequence=1,
        triggered_price=Decimal("9.20"),
        target_price_snapshot=Decimal("9.20"),
        condition_type_snapshot="PRICE_GTE",
        detected_at=now,
        data_timestamp=now,
        provider="licensed-test-feed",
        freshness_seconds=0,
        idempotency_key="trigger-local-audio",
    )
    db_session.add(trigger)
    db_session.flush()
    delivery = PriceAlertDelivery(
        trigger_id=trigger.id,
        alert_id=alert.id,
        telegram_user_id=owner.telegram_user_id,
        chat_id=1001,
        scheduled_for=now,
        idempotency_key="delivery-local-audio",
    )
    db_session.add(delivery)
    db_session.commit()

    calls = 0

    class Bot:
        async def send_message(self, **_kwargs):
            nonlocal calls
            calls += 1
            return SimpleNamespace(message_id=43)

    def broken_wav(_sound):
        raise OSError("disk unavailable")

    monkeypatch.setattr("app.alerts.delivery.generate_alarm_wav", broken_wav)
    result = await deliver_alarm_outbox(
        SimpleNamespace(bot=Bot()),
        db_session,
        _settings(user_price_alert_audio_enabled=True),
        now=now,
    )
    db_session.refresh(delivery)
    assert result["sent"] == 1
    assert calls == 1
    assert delivery.status == "SENT"
    assert delivery.error_code == "AUDIO_DELIVERY_ERROR"


@pytest.mark.asyncio
async def test_one_shot_alarm_completes_after_first_delivery(db_session):
    owner = _user(db_session)
    draft = AlarmDraft("ASELS", Decimal("72.50"), AlarmCondition.PRICE_GTE, AlarmMode.ONE_SHOT, 60)
    alert = create_alarm(db_session, owner, 1001, draft)
    now = datetime.now(timezone.utc)
    alert.status = "TRIGGERED"
    trigger = PriceAlertTrigger(
        alert_id=alert.id, trigger_sequence=1, triggered_price=Decimal("72.60"),
        target_price_snapshot=Decimal("72.50"), condition_type_snapshot="PRICE_GTE",
        detected_at=now, data_timestamp=now, provider="licensed", freshness_seconds=0,
        idempotency_key="one-shot-trigger",
    )
    db_session.add(trigger); db_session.flush()
    db_session.add(PriceAlertDelivery(
        trigger_id=trigger.id, alert_id=alert.id, telegram_user_id=owner.telegram_user_id,
        chat_id=1001, scheduled_for=now, idempotency_key="one-shot-delivery",
    ))
    db_session.commit()

    class Bot:
        async def send_message(self, **_kwargs):
            return SimpleNamespace(message_id=7)

    await deliver_alarm_outbox(SimpleNamespace(bot=Bot()), db_session, _settings(), now=now)
    db_session.refresh(alert); db_session.refresh(trigger)
    assert alert.status == "COMPLETED"
    assert alert.next_delivery_at is None
    assert trigger.status == "CLOSED"


def test_alarm_outbox_claim_is_atomic_and_cannot_be_claimed_twice(db_session):
    owner = _user(db_session)
    alert = create_alarm(db_session, owner, 1001, _draft())
    now = datetime.now(timezone.utc)
    alert.status = "TRIGGERED"
    trigger = PriceAlertTrigger(
        alert_id=alert.id, trigger_sequence=1, triggered_price=Decimal("9.20"),
        target_price_snapshot=Decimal("9.20"), condition_type_snapshot="PRICE_GTE",
        detected_at=now, data_timestamp=now, provider="licensed", freshness_seconds=0,
        idempotency_key="atomic-claim-trigger",
    )
    db_session.add(trigger); db_session.flush()
    delivery = PriceAlertDelivery(
        trigger_id=trigger.id, alert_id=alert.id, telegram_user_id=owner.telegram_user_id,
        chat_id=1001, scheduled_for=now, idempotency_key="atomic-claim-delivery",
    )
    db_session.add(delivery); db_session.commit()

    claimed = _claim_alarm_delivery(db_session, delivery.id, now)
    assert claimed is not None
    assert claimed.status == "SENDING"
    assert claimed.attempt_count == 1
    assert _claim_alarm_delivery(db_session, delivery.id, now) is None


def test_acknowledge_cancels_all_pending_repeats(db_session):
    owner = _user(db_session)
    alert = create_alarm(db_session, owner, 1001, _draft())
    now = datetime.now(timezone.utc)
    alert.status = "TRIGGERED"
    trigger = PriceAlertTrigger(
        alert_id=alert.id, trigger_sequence=1, triggered_price=Decimal("9.20"),
        target_price_snapshot=Decimal("9.20"), condition_type_snapshot="PRICE_GTE",
        detected_at=now, data_timestamp=now, provider="licensed", freshness_seconds=0,
        idempotency_key="ack-trigger",
    )
    db_session.add(trigger); db_session.flush()
    db_session.add(PriceAlertDelivery(
        trigger_id=trigger.id, alert_id=alert.id, telegram_user_id=owner.telegram_user_id,
        chat_id=1001, scheduled_for=now, idempotency_key="ack-delivery",
    ))
    db_session.commit()
    acknowledge_alarm(db_session, alert, now=now)
    assert alert.status == "ACKNOWLEDGED"
    assert trigger.status == "ACKNOWLEDGED"
    assert db_session.query(PriceAlertDelivery).one().status == "CANCELLED"
