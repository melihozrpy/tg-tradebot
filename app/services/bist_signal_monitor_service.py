from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

from app.models.database import (
    Signal,
    SignalEvent,
    SignalEventDelivery,
    SignalStateEnum,
    User,
)
from app.services.bist_signal_runtime_service import BistSignalRuntimeService
from app.signals import CandleObservation, ExecutionPolicy, FillModel, SignalEventType, TradingState


logger = logging.getLogger("mergen_quant.bist_signal_monitor")
_MONITOR_SOURCE = "bist_signal_monitor"
_RECONCILABLE_EVENT_SOURCES = (_MONITOR_SOURCE, "telegram_manual")
_SENDING_LEASE = timedelta(minutes=5)
_OPEN_STATES = (
    SignalStateEnum.PENDING_ENTRY,
    SignalStateEnum.ACTIVE,
    SignalStateEnum.TP1_HIT,
    SignalStateEnum.TP2_HIT,
    SignalStateEnum.EXIT_PENDING,
    SignalStateEnum.SUSPENDED,
)


def _utc(value) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp saat dilimi içermiyor")
    return parsed.astimezone(timezone.utc)


def _policy(settings) -> ExecutionPolicy:
    try:
        fill_model = FillModel(str(getattr(settings, "backtest_fill_model", "conservative_volume_limited")))
    except ValueError:
        fill_model = FillModel.CONSERVATIVE_VOLUME_LIMITED
    return ExecutionPolicy(
        fill_model=fill_model,
        max_volume_participation_percent=Decimal(str(
            getattr(settings, "max_daily_volume_participation_percent", 1.0)
        )),
        conservative_limit_lock=str(getattr(settings, "backtest_limit_lock_mode", "conservative")).casefold() == "conservative",
        allow_delayed_data_for_live_trigger=bool(
            getattr(settings, "allow_delayed_data_for_live_trigger", False)
        ),
    )


def quote_to_signal_observation(symbol: str, quote: dict, settings, now: datetime) -> CandleObservation:
    timestamp = _utc(quote.get("timestamp"))
    max_stale = getattr(settings, "max_market_data_staleness_seconds", None)
    max_stale = int(max_stale if max_stale is not None else 15)
    age = (now - timestamp).total_seconds()
    if age < -5 or age > max_stale:
        raise ValueError("quote eski veya gelecekte")
    if quote.get("is_live") is not True or quote.get("is_fresh") is not True:
        raise ValueError("quote canlı/doğrulanmış değil")
    if quote.get("valid_transaction") is not True:
        raise ValueError("quote geçerli işlem kaydı değil")
    try:
        trading_state = TradingState(str(quote.get("trading_state")).upper())
    except ValueError as exc:
        raise ValueError("işlem durumu eksik veya desteklenmiyor") from exc
    price = Decimal(str(quote["price"]))
    volume = quote.get("last_trade_quantity", quote.get("volume"))
    if volume is None:
        raise ValueError("işlem miktarı eksik")
    return CandleObservation(
        symbol=symbol,
        timestamp=timestamp,
        open=quote.get("open") or price,
        high=quote.get("high") or price,
        low=quote.get("low") or price,
        close=price,
        volume=volume,
        timeframe="tick",
        provider=str(quote.get("provider") or "licensed_quote"),
        # An instantaneous quote must not masquerade as a completed candle.
        # LIMIT/zone touches and active-position TP/SL checks still use the
        # verified transaction; completed-close breakout plans wait for an
        # explicitly completed bar supplied by the contracted gateway.
        is_complete=quote.get("bar_complete") is True,
        is_session_open=quote.get("market_open") is True,
        is_delayed=False,
        safe_for_live_trigger=True,
        valid_transaction=True,
        trading_state=trading_state,
        upper_limit=quote.get("upper_limit"),
        lower_limit=quote.get("lower_limit"),
        upper_limit_locked=quote.get("upper_limit_locked") is True,
        lower_limit_locked=quote.get("lower_limit_locked") is True,
        available_buy_quantity=quote.get("available_buy_quantity"),
        available_sell_quantity=quote.get("available_sell_quantity"),
        volume_ratio=quote.get("volume_ratio"),
    )


def enqueue_signal_event_delivery(db: Session, event_id: int, *, now: datetime | None = None) -> bool:
    now = _utc(now or datetime.now(timezone.utc))
    event = db.get(SignalEvent, event_id)
    if event is None:
        return False
    signal = db.get(Signal, event.signal_id)
    user = db.get(User, signal.user_id) if signal is not None and signal.user_id is not None else None
    if signal is None or user is None:
        return False
    if db.query(SignalEventDelivery.id).filter_by(signal_event_id=event.id).first() is not None:
        return False
    row = SignalEventDelivery(
        signal_event_id=event.id,
        signal_id=signal.id,
        user_id=user.id,
        telegram_user_id=user.telegram_user_id,
        chat_id=user.telegram_user_id,
        status="PENDING",
        scheduled_for=now,
        payload_text=format_signal_event_message(signal, event),
    )
    db.add(row)
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def enqueue_missing_signal_event_deliveries(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 1000,
) -> int:
    """Reconcile monitor events committed just before a process crash.

    Creation events are excluded to avoid replaying historical plan cards.
    Monitor and ``telegram_manual`` lifecycle events are included because both
    can commit just before their caller enqueues a notification.  The
    event-level unique constraint makes restart reconciliation idempotent.
    """

    now = _utc(now or datetime.now(timezone.utc))
    event_ids = [
        row[0]
        for row in (
            db.query(SignalEvent.id)
            .join(Signal, Signal.id == SignalEvent.signal_id)
            .join(User, User.id == Signal.user_id)
            .outerjoin(
                SignalEventDelivery,
                SignalEventDelivery.signal_event_id == SignalEvent.id,
            )
            .filter(
                SignalEventDelivery.id.is_(None),
                SignalEvent.source.in_(_RECONCILABLE_EVENT_SOURCES),
                SignalEvent.event_type.isnot(None),
                SignalEvent.event_type != SignalEventType.SIGNAL_CREATED.value,
            )
            .order_by(SignalEvent.id)
            .limit(max(1, int(limit)))
            .all()
        )
    ]
    return sum(enqueue_signal_event_delivery(db, event_id, now=now) for event_id in event_ids)


def run_signal_monitor_cycle(db: Session, provider, settings, *, now: datetime | None = None) -> dict:
    now = _utc(now or datetime.now(timezone.utc))
    queued = enqueue_missing_signal_event_deliveries(db, now=now)
    expired = errors = 0
    runtime = BistSignalRuntimeService(
        db,
        execution_policy=_policy(settings),
        move_stop_to_breakeven_after_tp1=bool(getattr(settings, "move_stop_to_breakeven_after_tp1", True)),
        move_stop_to_tp1_after_tp2=bool(getattr(settings, "move_stop_to_tp1_after_tp2", True)),
    )

    # Validity is a wall-clock lifecycle rule.  It must advance even when BIST
    # is closed and even if the user temporarily disabled quote monitoring.
    expiry_candidates = db.query(Signal).filter(
        Signal.user_id.isnot(None),
        Signal.side == "BUY",
        Signal.state == SignalStateEnum.PENDING_ENTRY,
        Signal.expires_at.isnot(None),
    ).all()
    for signal in expiry_candidates:
        if _utc(signal.expires_at) > now:
            continue
        try:
            result = runtime.expire_pending(
                signal.id,
                signal.user_id,
                as_of=now,
                source=_MONITOR_SOURCE,
            )
            if result.applied:
                expired += 1
            for event_id in result.event_ids:
                if enqueue_signal_event_delivery(db, event_id, now=now):
                    queued += 1
        except Exception as exc:
            db.rollback()
            logger.warning("Sinyal expiry islenemedi id=%s error=%s", signal.id, type(exc).__name__)
            errors += 1

    signals = db.query(Signal).filter(
        Signal.user_id.isnot(None),
        Signal.side == "BUY",
        Signal.monitoring_enabled.is_(True),
        Signal.state.in_(_OPEN_STATES),
    ).all()
    user_ids = {signal.user_id for signal in signals}
    users = {user.id: user for user in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}
    grouped = defaultdict(list)
    skipped = 0
    for signal in signals:
        owner = users.get(signal.user_id)
        if owner is None or owner.kill_switch_active:
            skipped += 1
            continue
        grouped[signal.symbol].append(signal)
    if not grouped:
        return {
            "signals": len(signals),
            "symbols_fetched": 0,
            "updated": 0,
            "queued": queued,
            "expired": expired,
            "rejected": 0,
            "skipped": skipped,
            "errors": errors,
        }
    try:
        market_open = bool(provider.is_market_open())
    except Exception:
        market_open = False
    if not market_open:
        return {"signals": len(signals), "symbols_fetched": 0, "updated": 0, "queued": queued,
                "expired": expired, "rejected": 0, "skipped": len(signals), "errors": errors}

    fetched = updated = rejected = 0
    for symbol, rows in grouped.items():
        try:
            quote = provider.get_quote(symbol)
            observation = quote_to_signal_observation(symbol, quote, settings, now)
            fetched += 1
        except Exception as exc:
            logger.warning("Sinyal quote reddedildi symbol=%s error=%s", symbol, type(exc).__name__)
            rejected += len(rows)
            continue
        for signal in rows:
            try:
                result = runtime.process_observation(
                    signal.id,
                    signal.user_id,
                    observation,
                    source=_MONITOR_SOURCE,
                )
                if result.applied:
                    updated += 1
                for event_id in result.event_ids:
                    if enqueue_signal_event_delivery(db, event_id, now=now):
                        queued += 1
            except Exception as exc:
                db.rollback()
                logger.warning("Sinyal observation işlenemedi id=%s error=%s", signal.id, type(exc).__name__)
                errors += 1
    return {"signals": len(signals), "symbols_fetched": fetched, "updated": updated, "queued": queued,
            "expired": expired, "rejected": rejected, "skipped": skipped, "errors": errors}


_EVENT_LABELS = {
    "SIGNAL_CREATED": "📌 ALIM PLANI OLUŞTURULDU",
    "ENTRY_FILLED": "✅ GİRİŞ GERÇEKLEŞTİ",
    "ENTRY_PARTIALLY_FILLED": "🟡 GİRİŞ KISMİ GERÇEKLEŞTİ",
    "ENTRY_REACHED": "✅ KIRILIM TEYİT EDİLDİ · SONRAKİ AÇILIŞ BEKLENİYOR",
    "SIGNAL_EXPIRED": "⌛ SİNYALİN SÜRESİ DOLDU",
    "ENTRY_INVALIDATED": "❌ GİRİŞ PLANI GEÇERSİZ OLDU",
    "ORDER_REMAINED_UNFILLED": "⚠️ EMİR GERÇEKLEŞMEDİ",
    "TP1_REACHED": "🎯 TP1 GERÇEKLEŞTİ",
    "TP2_REACHED": "🎯 TP2 GERÇEKLEŞTİ",
    "TP3_REACHED": "🏆 TP3 GERÇEKLEŞTİ",
    "STOP_REACHED": "🛑 STOP SEVİYESİNE GELDİ",
    "STOP_EXECUTED": "🛑 STOP GERÇEKLEŞTİ",
    "STOP_EXECUTION_DELAYED": "⚠️ STOP ÇIKIŞI BEKLİYOR",
    "STOP_MOVED": "🛡️ STOP YUKARI TAŞINDI",
    "CIRCUIT_BREAKER_STARTED": "⏸️ DEVRE KESİCİ BAŞLADI",
    "CIRCUIT_BREAKER_ENDED": "▶️ DEVRE KESİCİ SONA ERDİ",
    "TRADING_SUSPENDED": "⏸️ İŞLEM SIRASI DURDU",
    "TRADING_RESUMED": "▶️ İŞLEM SIRASI AÇILDI",
    "SIGNAL_CANCELLED": "❌ SİNYAL İPTAL EDİLDİ",
    "POSITION_CLOSED_MANUALLY": "✅ POZİSYON MANUEL KAPANDI",
}


def format_signal_event_message(signal: Signal, event: SignalEvent) -> str:
    from zoneinfo import ZoneInfo

    label = _EVENT_LABELS.get(event.event_type, f"🔔 {event.event_type}")
    price = event.execution_price or event.price_at_event
    price_line = f"Fiyat: {Decimal(str(price)):.2f} TL\n" if price is not None else ""
    event_time = event.candle_open_time or event.trading_date or event.created_at
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    local_time = event_time.astimezone(ZoneInfo("Europe/Istanbul"))
    if event.event_type == "SIGNAL_CREATED":
        return (
            f"{label}\n━━━━━━━━━━━━━━━━━━\n"
            f"Hisse: {signal.symbol}\nSinyal: #{signal.id}\n"
            f"Planlanan giriş: {Decimal(str(signal.planned_entry_price or signal.entry_trigger)):.2f} TL\n"
            f"Stop: {Decimal(str(signal.current_stop_price or signal.stop_price)):.2f} TL\n"
            f"TP1: {Decimal(str(signal.target_1)):.2f} TL\n"
            f"TP2: {Decimal(str(signal.target_2)):.2f} TL\n"
            f"TP3: {Decimal(str(signal.target_3)):.2f} TL\n"
            f"Planlanan miktar: {Decimal(str(signal.requested_quantity or 0)):.0f} lot\n"
            f"Veri zamanı: {local_time:%d.%m.%Y %H:%M:%S}\n"
            f"Kaynak: {event.provider or signal.provider}\n\n"
            "Henüz alım gerçekleşmedi. Giriş koşulu doğrulanırsa ayrıca bildirim gönderilecek.\n"
            "Bu çıktı sanal takip/analizdir; gerçek emir gönderilmez."
        )
    quantity_line = ""
    if event.executed_quantity is not None:
        quantity_line = f"Gerçekleşen: {Decimal(str(event.executed_quantity)):.0f} lot\n"
    try:
        metadata = json.loads(event.metadata_json or "{}")
    except (TypeError, ValueError):
        metadata = {}
    remaining = metadata.get("remaining_quantity") if isinstance(metadata, dict) else None
    if remaining is None and event.event_type in {"ENTRY_FILLED", "ENTRY_PARTIALLY_FILLED"}:
        remaining = event.executed_quantity
    if remaining is None and event.to_state in {"TP3_HIT", "STOPPED", "EXPIRED", "CLOSED_MANUALLY"}:
        remaining = 0
    remaining_line = (
        f"Kalan lot: {Decimal(str(remaining)):.0f}\n" if remaining is not None else ""
    )
    return (
        f"{label}\n━━━━━━━━━━━━━━━━━━\n"
        f"Hisse: {signal.symbol}\nSinyal: #{signal.id}\n"
        f"Durum: {event.from_state or '-'} → {event.to_state}\n"
        f"{price_line}{quantity_line}{remaining_line}"
        f"Veri zamanı: {local_time:%d.%m.%Y %H:%M:%S}\n"
        f"Kaynak: {event.provider or signal.provider}\n\n"
        "Bu çıktı sanal takip/analizdir; gerçek emir gönderilmez."
    )


def _claim_signal_event_delivery(
    db: Session,
    delivery_id: int,
    now: datetime,
) -> SignalEventDelivery | None:
    """Atomically claim one due row across SQLite and PostgreSQL workers."""

    claimed = db.query(SignalEventDelivery).filter(
        SignalEventDelivery.id == delivery_id,
        SignalEventDelivery.status.in_(["PENDING", "RETRY"]),
        SignalEventDelivery.scheduled_for <= now,
    ).update(
        {
            SignalEventDelivery.status: "SENDING",
            SignalEventDelivery.attempted_at: now,
            SignalEventDelivery.attempt_count: SignalEventDelivery.attempt_count + 1,
        },
        synchronize_session=False,
    )
    db.commit()
    if claimed != 1:
        return None
    db.expire_all()
    return db.get(SignalEventDelivery, delivery_id)


def _retry_after_seconds(exc: RetryAfter) -> int:
    value = getattr(exc, "retry_after", 30) or 30
    if isinstance(value, timedelta):
        value = value.total_seconds()
    return max(1, int(value))


async def deliver_signal_event_outbox(application, db: Session, settings, *, now: datetime | None = None) -> dict:
    now = _utc(now or datetime.now(timezone.utc))
    recovered = enqueue_missing_signal_event_deliveries(db, now=now)
    db.query(SignalEventDelivery).filter(
        SignalEventDelivery.status == "SENDING",
        or_(
            SignalEventDelivery.attempted_at.is_(None),
            SignalEventDelivery.attempted_at <= now - _SENDING_LEASE,
        ),
    ).update(
        {
            "status": "RETRY",
            "scheduled_for": now,
            "next_retry_at": now,
            "error_code": "STALE_SENDING_LEASE",
        },
        synchronize_session=False,
    )
    db.commit()
    limit = int(getattr(settings, "user_price_alert_max_global_deliveries_per_minute", 500))
    row_ids = [
        row[0]
        for row in db.query(SignalEventDelivery.id).filter(
            SignalEventDelivery.status.in_(["PENDING", "RETRY"]),
            SignalEventDelivery.scheduled_for <= now,
        ).order_by(SignalEventDelivery.scheduled_for, SignalEventDelivery.id).limit(limit).all()
    ]
    sent = retry = failed = claimed_count = 0
    for row_id in row_ids:
        row = _claim_signal_event_delivery(db, row_id, now)
        if row is None:
            continue
        claimed_count += 1
        event = db.get(SignalEvent, row.signal_event_id)
        signal = db.get(Signal, row.signal_id)
        if event is None or signal is None:
            row.status = "FAILED"
            row.error_code = "MISSING_SOURCE"
            failed += 1
            db.commit()
            continue
        try:
            message = await application.bot.send_message(
                chat_id=row.chat_id,
                text=row.payload_text or format_signal_event_message(signal, event),
                disable_notification=False,
            )
            row.status = "SENT"
            row.sent_at = now
            row.telegram_message_id = getattr(message, "message_id", None)
            row.next_retry_at = None
            row.error_code = None
            sent += 1
        except RetryAfter as exc:
            seconds = _retry_after_seconds(exc)
            row.status = "RETRY"
            row.next_retry_at = now + timedelta(seconds=seconds)
            row.scheduled_for = row.next_retry_at
            row.error_code = "RATE_LIMIT"
            retry += 1
        except (Forbidden, BadRequest):
            row.status = "FAILED"
            row.error_code = "PERMANENT_TELEGRAM_ERROR"
            failed += 1
        except TelegramError:
            if row.attempt_count >= 8:
                row.status = "FAILED"
                failed += 1
            else:
                row.status = "RETRY"
                row.scheduled_for = now + timedelta(seconds=min(900, 5 * 2 ** row.attempt_count))
                row.next_retry_at = row.scheduled_for
                retry += 1
            row.error_code = "TELEGRAM_ERROR"
        except Exception:
            logger.exception("Beklenmeyen sinyal outbox teslim hatasi delivery_id=%s", row.id)
            if row.attempt_count >= 8:
                row.status = "FAILED"
                failed += 1
            else:
                row.status = "RETRY"
                row.scheduled_for = now + timedelta(seconds=min(900, 5 * 2 ** row.attempt_count))
                row.next_retry_at = row.scheduled_for
                retry += 1
            row.error_code = "UNEXPECTED_DELIVERY_ERROR"
        db.commit()
    return {
        "due": len(row_ids),
        "claimed": claimed_count,
        "recovered": recovered,
        "sent": sent,
        "retry": retry,
        "failed": failed,
    }
