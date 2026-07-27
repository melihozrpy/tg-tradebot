from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config.settings import get_settings
from app.data.provider_factory import build_market_data_provider
from app.models.database import Signal, SignalEvent, SignalStateEnum, User, get_session_factory
from app.services.bist_signal_monitor_service import (
    enqueue_signal_event_delivery,
    quote_to_signal_observation,
)
from app.services.bist_signal_runtime_service import (
    BistSignalRuntimeError,
    BistSignalRuntimeService,
    CreateBistSignalRequest,
)
from app.services.watchlist_service import get_or_create_user
from app.signals import EntryOrderType, PositionSizingRequest, calculate_position_size
from app.telegram.handlers import _reject_unauthorized


_ACTIVE_POSITION_STATES = (
    SignalStateEnum.ACTIVE,
    SignalStateEnum.TP1_HIT,
    SignalStateEnum.TP2_HIT,
    SignalStateEnum.EXIT_PENDING,
)


class SignalCommandError(ValueError):
    pass


def _positive_signal_id(args: list[str], command: str) -> int:
    if len(args) != 1:
        raise SignalCommandError(f"Kullanım: /{command} <sinyal_id>")
    try:
        signal_id = int(args[0])
    except (TypeError, ValueError) as exc:
        raise SignalCommandError("Sinyal numarası pozitif bir tam sayı olmalı.") from exc
    if signal_id <= 0:
        raise SignalCommandError("Sinyal numarası pozitif bir tam sayı olmalı.")
    return signal_id


def _user(db, update: Update, settings) -> User:
    return get_or_create_user(
        db,
        update.effective_user.id,
        update.effective_user.id in settings.admin_ids,
        settings.default_total_capital,
    )


def _expiration_for(timeframe: str, created_at: datetime) -> datetime:
    """Translate the documented completed-candle defaults into safe deadlines.

    The deadline is deliberately conservative. The runtime still records an
    explicit EXPIRED event; a licensed exchange calendar can later replace the
    wall-clock approximation without changing stored signal semantics.
    """

    normalized = timeframe.strip().casefold()
    durations = {
        "5m": timedelta(hours=3),
        "5d": timedelta(hours=3),
        "15m": timedelta(hours=6),
        "15d": timedelta(hours=6),
        "1h": timedelta(days=3),
        "1s": timedelta(days=3),
        "4h": timedelta(days=7),
        "4s": timedelta(days=7),
        "1d": timedelta(days=8),
        "1g": timedelta(days=8),
        "1wk": timedelta(days=28),
        "1w": timedelta(days=28),
        "1hafta": timedelta(days=28),
    }
    return created_at + durations.get(normalized, timedelta(days=8))


def _entry_plan(source: Signal) -> tuple[EntryOrderType, Decimal, Decimal | None, Decimal | None]:
    zone_low = Decimal(str(source.entry_zone_low)) if source.entry_zone_low is not None else None
    zone_high = Decimal(str(source.entry_zone_high)) if source.entry_zone_high is not None else None
    if zone_low is not None and zone_high is not None:
        planned = Decimal(str(source.entry_trigger)) if source.entry_trigger is not None else zone_high
        return EntryOrderType.ENTRY_ZONE, planned, zone_low, zone_high
    if source.entry_trigger is not None:
        return EntryOrderType.BREAKOUT_BUY, Decimal(str(source.entry_trigger)), None, None
    if source.planned_entry_price is not None:
        return EntryOrderType.LIMIT_BUY, Decimal(str(source.planned_entry_price)), None, None
    raise SignalCommandError("Bu analizde izlenebilir bir giriş seviyesi yok. Önce /analiz çalıştır.")


def clone_analysis_signal_for_user(
    db,
    user: User,
    source: Signal,
    settings,
    *,
    now: datetime | None = None,
) -> Signal:
    """Create an owned PENDING_ENTRY copy without claiming an executed trade."""

    if source.user_id is not None and source.user_id != user.id:
        raise SignalCommandError("Bu sinyal başka bir kullanıcıya ait.")
    if source.user_id == user.id:
        return BistSignalRuntimeService(db).set_monitoring(source.id, user.id, True)
    if source.side and source.side.upper() != "BUY":
        raise SignalCommandError("BIST spot modunda yalnız AL planları takip edilebilir.")
    required = (source.stop_price, source.target_1, source.target_2, source.target_3)
    if any(value is None for value in required):
        raise SignalCommandError("Sinyalde stop ile TP1–TP3 seviyelerinin tamamı bulunmuyor.")

    order_type, entry, zone_low, zone_high = _entry_plan(source)
    stop = Decimal(str(source.stop_price))
    targets = tuple(Decimal(str(value)) for value in (source.target_1, source.target_2, source.target_3))
    capital = Decimal(str(user.total_capital or settings.default_total_capital))
    available_cash = Decimal(str(user.cash_balance if user.cash_balance is not None else capital))
    sizing = calculate_position_size(
        PositionSizingRequest(
            portfolio_balance=capital,
            risk_percent=Decimal(str(settings.default_risk_percent)),
            entry_price=entry,
            stop_price=stop,
            available_cash=available_cash,
            maximum_position_percent=Decimal(str(settings.max_position_percent)),
            maximum_volume_participation_percent=Decimal(
                str(settings.max_daily_volume_participation_percent)
            ),
            target_prices=targets,
            target_allocations=(
                Decimal(str(settings.default_tp1_allocation)),
                Decimal(str(settings.default_tp2_allocation)),
                Decimal(str(settings.default_tp3_allocation)),
            ),
        )
    )
    if sizing.suggested_lots <= 0:
        raise SignalCommandError(
            "Sermaye/risk sınırları bu plan için en az 1 lot üretmedi. /sermaye_ayarla ve /nakit_ayarla değerlerini kontrol et."
        )

    now = now or datetime.now(timezone.utc)
    data_timestamp = source.data_timestamp
    if data_timestamp.tzinfo is None:
        data_timestamp = data_timestamp.replace(tzinfo=timezone.utc)
    else:
        data_timestamp = data_timestamp.astimezone(timezone.utc)
    allocations = (
        Decimal(str(settings.default_tp1_allocation)),
        Decimal(str(settings.default_tp2_allocation)),
        Decimal(str(settings.default_tp3_allocation)),
    )
    return BistSignalRuntimeService(db).create_pending_signal(
        CreateBistSignalRequest(
            user_id=user.id,
            symbol=source.symbol,
            timeframe=source.timeframe,
            creation_price=entry,
            entry_order_type=order_type,
            raw_entry_price=entry,
            raw_entry_zone_low=zone_low,
            raw_entry_zone_high=zone_high,
            raw_stop_price=stop,
            raw_target_prices=targets,
            requested_quantity=sizing.suggested_lots,
            created_at=now,
            data_timestamp=data_timestamp,
            provider=source.provider,
            source=f"telegram_follow:{source.id}",
            strategy_version=source.strategy_version,
            score=Decimal(str(source.score)),
            confidence=source.confidence,
            risk_reward=Decimal(str(source.risk_reward)) if source.risk_reward is not None else None,
            target_allocations=allocations,
            valid_from=now,
            expires_at=_expiration_for(source.timeframe, now),
            price_adjustment_mode=source.price_adjustment_mode
            or getattr(settings, "backtest_price_mode", "split_adjusted"),
            idempotency_key=f"telegram-follow:{user.id}:{source.id}",
        )
    )


def _creation_event_id(db, signal_id: int) -> int | None:
    row = (
        db.query(SignalEvent)
        .filter(SignalEvent.signal_id == signal_id, SignalEvent.event_type == "SIGNAL_CREATED")
        .order_by(SignalEvent.id)
        .first()
    )
    return row.id if row is not None else None


async def cmd_takip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    try:
        signal_id = _positive_signal_id(context.args, "takip")
    except SignalCommandError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return
    settings = get_settings()
    db = get_session_factory()()
    try:
        user = _user(db, update, settings)
        source = db.get(Signal, signal_id)
        if source is None:
            raise SignalCommandError("Sinyal bulunamadı.")
        was_owned = source.user_id == user.id
        signal = clone_analysis_signal_for_user(db, user, source, settings)
        event_id = _creation_event_id(db, signal.id)
        if not was_owned and event_id is not None:
            enqueue_signal_event_delivery(db, event_id)
        if was_owned:
            await update.message.reply_text(f"▶️ #{signal.id} {signal.symbol} takibi yeniden açıldı.")
        else:
            await update.message.reply_text(
                f"📌 #{signal.id} {signal.symbol} planı PENDING_ENTRY olarak takibe alındı.\n"
                "Henüz alım gerçekleşmedi. Giriş şartı doğrulanırsa ayrı bildirim gelecek.\n"
                "Ayrıntılı plan bildirimi teslimat sırasına eklendi."
            )
    except (SignalCommandError, BistSignalRuntimeError, ValueError) as exc:
        db.rollback()
        await update.message.reply_text(f"❌ Takip başlatılamadı: {exc}")
    finally:
        db.close()


async def cmd_takip_birak(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    try:
        signal_id = _positive_signal_id(context.args, "takip_birak")
    except SignalCommandError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return
    settings = get_settings()
    db = get_session_factory()()
    try:
        user = _user(db, update, settings)
        signal = BistSignalRuntimeService(db).set_monitoring(signal_id, user.id, False)
        await update.message.reply_text(
            f"⏸️ #{signal.id} {signal.symbol} takibi durduruldu. Kayıt ve olay geçmişi silinmedi."
        )
    except BistSignalRuntimeError as exc:
        db.rollback()
        await update.message.reply_text(f"❌ Takip durdurulamadı: {exc}")
    finally:
        db.close()


async def cmd_sinyal_iptal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    try:
        signal_id = _positive_signal_id(context.args, "sinyal_iptal")
    except SignalCommandError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return
    settings = get_settings()
    db = get_session_factory()()
    try:
        user = _user(db, update, settings)
        event = BistSignalRuntimeService(db).cancel_pending(
            signal_id, user.id, event_time=datetime.now(timezone.utc)
        )
        enqueue_signal_event_delivery(db, event.id)
        await update.message.reply_text("✅ Sinyal iptali kaydedildi; durum bildirimi sıraya alındı.")
    except BistSignalRuntimeError as exc:
        db.rollback()
        await update.message.reply_text(f"❌ Sinyal iptal edilemedi: {exc}")
    finally:
        db.close()


async def cmd_stop_girise(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    try:
        signal_id = _positive_signal_id(context.args, "stop_girise")
    except SignalCommandError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return
    settings = get_settings()
    db = get_session_factory()()
    try:
        user = _user(db, update, settings)
        event = BistSignalRuntimeService(db).move_stop_to_breakeven(
            signal_id, user.id, event_time=datetime.now(timezone.utc)
        )
        enqueue_signal_event_delivery(db, event.id)
        await update.message.reply_text("🛡️ Stop giriş fiyatına taşındı; olay bildirimi sıraya alındı.")
    except BistSignalRuntimeError as exc:
        db.rollback()
        await update.message.reply_text(f"❌ Stop taşınamadı: {exc}")
    finally:
        db.close()


async def cmd_pozisyon_kapat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    try:
        signal_id = _positive_signal_id(context.args, "pozisyon_kapat")
    except SignalCommandError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return
    settings = get_settings()
    db = get_session_factory()()
    try:
        user = _user(db, update, settings)
        signal = db.query(Signal).filter(Signal.id == signal_id, Signal.user_id == user.id).one_or_none()
        if signal is None:
            raise SignalCommandError("Sinyal bulunamadı veya sana ait değil.")
        provider = build_market_data_provider(settings)
        now = datetime.now(timezone.utc)
        quote = await asyncio.to_thread(provider.get_quote, signal.symbol)
        observation = quote_to_signal_observation(signal.symbol, quote, settings, now)
        event = BistSignalRuntimeService(db).close_manually(
            signal.id,
            user.id,
            execution_price=observation.close,
            event_time=observation.timestamp,
            provider=observation.provider,
        )
        enqueue_signal_event_delivery(db, event.id)
        await update.message.reply_text(
            "✅ Sanal takip pozisyonu güncel, doğrulanmış işlem fiyatıyla kapatıldı; bildirim sıraya alındı.\n"
            "ℹ️ Borsa aracı kurumuna gerçek emir gönderilmedi."
        )
    except (SignalCommandError, BistSignalRuntimeError, ValueError) as exc:
        db.rollback()
        await update.message.reply_text(
            f"❌ Pozisyon kapatılamadı: {exc}\n"
            "Gecikmeli/eksik veriyle kapanış fiyatı üretilmez; lisanslı canlı veri ayarlarını kontrol et."
        )
    except Exception:
        db.rollback()
        await update.message.reply_text(
            "❌ Güncel ve doğrulanabilir fiyat alınamadığı için pozisyon kapatılmadı."
        )
    finally:
        db.close()


async def cmd_aktif_pozisyonlar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    settings = get_settings()
    db = get_session_factory()()
    try:
        user = _user(db, update, settings)
        rows = (
            db.query(Signal)
            .filter(Signal.user_id == user.id, Signal.state.in_(_ACTIVE_POSITION_STATES))
            .order_by(Signal.updated_at.desc(), Signal.id.desc())
            .limit(30)
            .all()
        )
        if not rows:
            await update.message.reply_text("📭 Aktif sanal takip pozisyonun yok.")
            return
        lines = ["📍 AKTİF SANAL POZİSYONLAR", "━━━━━━━━━━━━━━━━━━"]
        for signal in rows:
            state = signal.state.value if hasattr(signal.state, "value") else str(signal.state)
            lines.extend(
                [
                    f"#{signal.id} • {signal.symbol} • {state}",
                    f"Giriş: {Decimal(str(signal.average_fill_price or signal.actual_entry_price)):.2f} TL",
                    f"Stop: {Decimal(str(signal.current_stop_price or signal.stop_price)):.2f} TL",
                    f"Kalan: {Decimal(str(signal.remaining_quantity or 0)):.0f} lot",
                    "",
                ]
            )
        lines.append("Detay: /sinyal <id>  •  Kapat: /pozisyon_kapat <id>")
        await update.message.reply_text("\n".join(lines))
    finally:
        db.close()


def register_ultra_signal_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("takip", cmd_takip))
    application.add_handler(CommandHandler("takip_birak", cmd_takip_birak))
    application.add_handler(CommandHandler("sinyal_iptal", cmd_sinyal_iptal))
    application.add_handler(CommandHandler("stop_girise", cmd_stop_girise))
    application.add_handler(CommandHandler("pozisyon_kapat", cmd_pozisyon_kapat))
    application.add_handler(CommandHandler("aktif_pozisyonlar", cmd_aktif_pozisyonlar))
