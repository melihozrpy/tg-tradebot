"""Transactional persistence adapter for the pure BIST signal domain.

The service consumes already obtained :class:`CandleObservation` objects.  It
does not call a market-data provider and does not send Telegram messages.  A
notification worker can safely key deliveries by ``SignalEvent.unique_dedup_key``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, NoReturn

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.database import (
    CorporateActionRecord,
    Signal,
    SignalEvent,
    SignalStateEnum,
    SignalTarget,
    SignalTransitionErrorAudit,
    SignalTypeEnum,
    User,
)
from app.signals.enums import (
    BreakoutConfirmationMode,
    EntryOrderType,
    ExitOrderType,
    FillStatus,
    PricePurpose,
    SignalEventType,
    SignalStatus,
    TradingState,
)
from app.signals.execution import (
    CandleObservation,
    EntryPlan,
    ExecutionPolicy,
    TransactionCostModel,
    allocate_target_lots,
    evaluate_entry,
    evaluate_long_exit,
)
from app.signals.lifecycle import (
    FINAL_STATUSES,
    SignalDomainEvent,
    SignalLifecycle,
    TransitionErrorRecord,
    build_event_dedup_key,
)
from app.signals.market_rules import (
    DEFAULT_BIST_MARKET_RULES,
    BistMarketRules,
    DecimalLike,
    as_decimal,
)


logger = logging.getLogger("mergen_quant.bist_signal_runtime")
MONEY = Decimal("0.01")
PRICE_PRECISION = Decimal("0.000001")
QUANTITY_PRECISION = Decimal("0.0001")

_CORPORATE_ACTION_TYPES: dict[str, str] = {
    "SPLIT": "STOCK_SPLIT",
    "STOCK_SPLIT": "STOCK_SPLIT",
    "HİSSE_BÖLÜNMESİ": "STOCK_SPLIT",
    "STOCK_DIVIDEND": "BONUS_ISSUE",
    "BONUS": "BONUS_ISSUE",
    "BONUS_ISSUE": "BONUS_ISSUE",
    "BEDELSIZ": "BONUS_ISSUE",
    "BEDELSİZ_SERMAYE_ARTIRIMI": "BONUS_ISSUE",
    "REVERSE_SPLIT": "REVERSE_SPLIT",
    "TERS_BOLUNME": "REVERSE_SPLIT",
    "TERS_BÖLÜNME": "REVERSE_SPLIT",
}


class BistSignalRuntimeError(RuntimeError):
    pass


class BistSignalNotFoundError(BistSignalRuntimeError):
    pass


class BistSignalOwnershipError(BistSignalRuntimeError):
    pass


class BistSignalConfigurationError(BistSignalRuntimeError):
    pass


class BistSignalTransitionError(BistSignalRuntimeError):
    pass


class BistSignalAuditPersistenceError(BistSignalRuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _price_precision(value: Decimal) -> Decimal:
    return value.quantize(PRICE_PRECISION, rounding=ROUND_HALF_UP)


def _quantity_precision(value: Decimal) -> Decimal:
    return value.quantize(QUANTITY_PRECISION, rounding=ROUND_HALF_UP)


def _state_value(value: SignalStateEnum | str) -> str:
    return value.value if isinstance(value, SignalStateEnum) else str(value)


_LEGACY_STATE_MAP: dict[str, SignalStatus] = {
    "CREATED": SignalStatus.PENDING_ENTRY,
    "WAITING_CONFIRMATION": SignalStatus.PENDING_ENTRY,
    "WAITING_TRIGGER": SignalStatus.PENDING_ENTRY,
    "CONFIRMED": SignalStatus.PENDING_ENTRY,
    "SENT": SignalStatus.PENDING_ENTRY,
    "TARGET_1_HIT": SignalStatus.TP1_HIT,
    "TARGET_2_HIT": SignalStatus.TP2_HIT,
    "TARGET_3_HIT": SignalStatus.TP3_HIT,
    "STOP_HIT": SignalStatus.STOPPED,
}


def _domain_status(value: SignalStateEnum | str | None) -> SignalStatus:
    if value is None:
        return SignalStatus.PENDING_ENTRY
    raw = _state_value(value)
    if raw in _LEGACY_STATE_MAP:
        return _LEGACY_STATE_MAP[raw]
    try:
        return SignalStatus(raw)
    except ValueError as exc:
        raise BistSignalConfigurationError(f"Desteklenmeyen veritabani sinyal durumu: {raw}") from exc


def _db_status(value: SignalStatus) -> SignalStateEnum:
    try:
        return SignalStateEnum[value.value]
    except KeyError as exc:
        raise BistSignalConfigurationError(
            f"SQLAlchemy SignalStateEnum {value.value} degerini icermiyor."
        ) from exc


def _decimal_or_none(value: Any) -> Decimal | None:
    return as_decimal(value) if value is not None else None


def _json_load(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (TypeError, ValueError):
        return {"raw": value}


def _metadata_for_json(metadata: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    def thaw(value: Any) -> Any:
        if isinstance(value, tuple):
            if all(
                isinstance(item, tuple)
                and len(item) == 2
                and isinstance(item[0], str)
                for item in value
            ):
                return {key: thaw(item) for key, item in value}
            return [thaw(item) for item in value]
        return value

    return {key: thaw(value) for key, value in metadata}


def _stable_key(*parts: Any) -> str:
    canonical = "|".join(str(part) for part in parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _observation_key(observation: CandleObservation, source: str) -> str:
    return (
        f"{observation.provider}:{source}:{observation.symbol}:"
        f"{observation.timeframe}:{observation.timestamp.isoformat()}"
    )


def _event_metadata(
    observation: CandleObservation,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach the immutable market evidence used for an execution decision.

    SignalEvent columns intentionally stay compact for backwards-compatible
    migrations; the complete trigger/fill evidence is retained in JSON so an
    operator can later audit the exact OHLCV range, market state and safety
    flags that caused (or delayed) a transition.
    """

    payload: dict[str, Any] = dict(metadata or {})
    payload["observation"] = {
        "symbol": observation.symbol,
        "timestamp": observation.timestamp.isoformat(),
        "timeframe": observation.timeframe,
        "open": str(observation.open),
        "high": str(observation.high),
        "low": str(observation.low),
        "close": str(observation.close),
        "volume": str(observation.volume),
        "provider": observation.provider,
        "bar_complete": observation.is_complete,
        "delayed": observation.is_delayed,
        "safe_for_live_trigger": observation.safe_for_live_trigger,
        "valid_transaction": observation.valid_transaction,
        "trading_state": observation.trading_state.value,
        "upper_limit": str(observation.upper_limit) if observation.upper_limit is not None else None,
        "lower_limit": str(observation.lower_limit) if observation.lower_limit is not None else None,
        "upper_limit_locked": observation.upper_limit_locked,
        "lower_limit_locked": observation.lower_limit_locked,
        "available_buy_quantity": (
            str(observation.available_buy_quantity)
            if observation.available_buy_quantity is not None
            else None
        ),
        "available_sell_quantity": (
            str(observation.available_sell_quantity)
            if observation.available_sell_quantity is not None
            else None
        ),
        "volume_ratio": (
            str(observation.volume_ratio) if observation.volume_ratio is not None else None
        ),
    }
    return payload


@dataclass(frozen=True, slots=True)
class CreateBistSignalRequest:
    user_id: int
    symbol: str
    timeframe: str
    creation_price: DecimalLike
    entry_order_type: EntryOrderType
    raw_entry_price: DecimalLike
    raw_stop_price: DecimalLike
    raw_target_prices: tuple[DecimalLike, DecimalLike, DecimalLike]
    requested_quantity: int
    created_at: datetime
    data_timestamp: datetime
    provider: str
    source: str
    strategy_version: str
    score: DecimalLike = Decimal("50")
    confidence: str = "orta"
    risk_reward: DecimalLike | None = None
    raw_entry_zone_low: DecimalLike | None = None
    raw_entry_zone_high: DecimalLike | None = None
    target_allocations: tuple[DecimalLike, DecimalLike, DecimalLike] = (
        Decimal("40"),
        Decimal("35"),
        Decimal("25"),
    )
    valid_from: datetime | None = None
    expires_at: datetime | None = None
    price_adjustment_mode: str = "raw"
    idempotency_key: str | None = None
    side: str = "BUY"


@dataclass(frozen=True, slots=True)
class ObservationProcessResult:
    signal_id: int
    status: SignalStatus
    applied: bool
    duplicate: bool
    out_of_order: bool
    event_ids: tuple[int, ...] = ()
    event_types: tuple[str, ...] = ()
    fill_status: FillStatus | None = None
    detail: str = ""


class BistSignalRuntimeService:
    def __init__(
        self,
        db: Session,
        *,
        market_rules: BistMarketRules = DEFAULT_BIST_MARKET_RULES,
        execution_policy: ExecutionPolicy = ExecutionPolicy(),
        cost_model: TransactionCostModel = TransactionCostModel(),
        move_stop_to_breakeven_after_tp1: bool = True,
        move_stop_to_tp1_after_tp2: bool = True,
    ) -> None:
        self.db = db
        self.market_rules = market_rules
        self.execution_policy = execution_policy
        self.cost_model = cost_model
        self.move_stop_to_breakeven_after_tp1 = move_stop_to_breakeven_after_tp1
        self.move_stop_to_tp1_after_tp2 = move_stop_to_tp1_after_tp2

    def _persist_rejected_transition(
        self,
        *,
        signal_id: int,
        user_id: int | None,
        error: TransitionErrorRecord,
        observation: CandleObservation,
        source: str,
        metadata: Mapping[str, Any] | None,
    ) -> SignalTransitionErrorAudit:
        """Rollback state changes, then durably audit a rejected transition."""

        # The lifecycle transaction may already contain earlier writes from the
        # same observation.  They must never be committed merely to retain the
        # rejection audit, so start the audit in a clean transaction.
        self.db.rollback()
        existing = (
            self.db.query(SignalTransitionErrorAudit)
            .filter(SignalTransitionErrorAudit.dedup_key == error.dedup_key)
            .one_or_none()
        )
        if existing is not None:
            if (
                existing.signal_id != signal_id
                or existing.event_type != error.event_type.value
                or existing.attempted_status != error.attempted_status.value
            ):
                self.db.rollback()
                raise BistSignalAuditPersistenceError(
                    "Transition audit dedup anahtari baska bir red kaydiyla cakisti."
                )
            return existing

        audit_metadata = {
            "symbol": observation.symbol,
            "timeframe": observation.timeframe,
            "observation_timestamp": observation.timestamp.isoformat(),
            "market_price": str(observation.close),
            "transition_metadata": dict(metadata or {}),
        }
        row = SignalTransitionErrorAudit(
            signal_id=signal_id,
            user_id=user_id,
            previous_status=error.previous_status.value,
            attempted_status=error.attempted_status.value,
            event_type=error.event_type.value,
            event_time=error.event_time,
            dedup_key=error.dedup_key,
            reason=error.reason,
            provider=observation.provider[:48],
            source=source[:48],
            metadata_json=json.dumps(audit_metadata, ensure_ascii=False, sort_keys=True, default=str),
        )
        self.db.add(row)
        try:
            self.db.commit()
            self.db.refresh(row)
            return row
        except IntegrityError as exc:
            self.db.rollback()
            concurrent = (
                self.db.query(SignalTransitionErrorAudit)
                .filter(SignalTransitionErrorAudit.dedup_key == error.dedup_key)
                .one_or_none()
            )
            if concurrent is not None and concurrent.signal_id == signal_id:
                return concurrent
            self.db.rollback()
            raise BistSignalAuditPersistenceError(
                "Reddedilen transition audit kaydi atomik olarak yazilamadi."
            ) from exc
        except Exception as exc:
            self.db.rollback()
            raise BistSignalAuditPersistenceError(
                "Reddedilen transition audit kaydi yazilamadi; state islemi geri alindi."
            ) from exc

    def _raise_rejected_transition(
        self,
        *,
        signal: Signal,
        error: TransitionErrorRecord,
        observation: CandleObservation,
        source: str,
        metadata: Mapping[str, Any] | None,
    ) -> NoReturn:
        signal_id = int(signal.id)
        user_id = int(signal.user_id) if signal.user_id is not None else None
        self._persist_rejected_transition(
            signal_id=signal_id,
            user_id=user_id,
            error=error,
            observation=observation,
            source=source,
            metadata=metadata,
        )
        raise BistSignalTransitionError(error.reason)

    def _owned_signal(self, signal_id: int, user_id: int, *, lock: bool = False) -> Signal:
        query = self.db.query(Signal).filter(Signal.id == signal_id)
        if lock:
            query = query.with_for_update()
        signal = query.one_or_none()
        if signal is None:
            raise BistSignalNotFoundError(f"Sinyal bulunamadi: {signal_id}")
        if signal.user_id != user_id:
            raise BistSignalOwnershipError("Bu sinyal baska bir kullaniciya ait.")
        return signal

    @staticmethod
    def _validate_request(request: CreateBistSignalRequest) -> tuple[str, tuple[Decimal, ...]]:
        if request.user_id <= 0:
            raise BistSignalConfigurationError("user_id pozitif olmalidir.")
        if request.side.strip().upper() != "BUY":
            raise BistSignalConfigurationError("BIST spot LONG_ONLY modunda yalnizca BUY/AL sinyali acilabilir.")
        symbol = request.symbol.strip().upper().removesuffix(".IS")
        if not symbol or not symbol.isalnum() or len(symbol) > 12:
            raise BistSignalConfigurationError("Gecerli bir BIST sembolu girilmelidir.")
        if not request.timeframe.strip() or not request.provider.strip() or not request.source.strip():
            raise BistSignalConfigurationError("Zaman dilimi, provider ve source zorunludur.")
        if not request.strategy_version.strip():
            raise BistSignalConfigurationError("Strateji surumu zorunludur.")
        if request.requested_quantity <= 0:
            raise BistSignalConfigurationError("Istenen lot sifirdan buyuk olmalidir.")
        allocations = tuple(as_decimal(value, field_name="allocation") for value in request.target_allocations)
        if len(allocations) != 3 or sum(allocations) != Decimal("100") or any(value < 0 for value in allocations):
            raise BistSignalConfigurationError("TP1/TP2/TP3 dagilimlari pozitif ve toplam %100 olmalidir.")
        return symbol, allocations

    def create_pending_signal(self, request: CreateBistSignalRequest) -> Signal:
        """Create the signal, its targets and creation event in one transaction."""

        symbol, allocations = self._validate_request(request)
        if self.db.query(User.id).filter(User.id == request.user_id).scalar() is None:
            raise BistSignalOwnershipError("Sinyal sahibi kullanici bulunamadi.")

        entry_purpose = (
            PricePurpose.BREAKOUT_TRIGGER
            if request.entry_order_type == EntryOrderType.BREAKOUT_BUY
            else PricePurpose.BUY_LIMIT
        )
        raw_entry = as_decimal(request.raw_entry_price, field_name="raw_entry_price")
        raw_stop = as_decimal(request.raw_stop_price, field_name="raw_stop_price")
        raw_targets = tuple(as_decimal(value, field_name="raw_target_price") for value in request.raw_target_prices)
        if len(raw_targets) != 3:
            raise BistSignalConfigurationError("TP1, TP2 ve TP3 zorunludur.")
        entry = self.market_rules.round_price(raw_entry, entry_purpose).rounded_order_price
        stop = self.market_rules.round_price(raw_stop, PricePurpose.PROTECTIVE_STOP_LONG).rounded_order_price
        targets = tuple(
            self.market_rules.round_price(value, PricePurpose.TARGET_LONG).rounded_order_price
            for value in raw_targets
        )
        if not stop < entry < targets[0] < targets[1] < targets[2]:
            raise BistSignalConfigurationError("Long plan seviyeleri stop < giris < TP1 < TP2 < TP3 olmali.")

        zone_low = zone_high = None
        if request.entry_order_type == EntryOrderType.ENTRY_ZONE:
            if request.raw_entry_zone_low is None or request.raw_entry_zone_high is None:
                raise BistSignalConfigurationError("ENTRY_ZONE icin alt ve ust sinir zorunludur.")
            zone_low = self.market_rules.round_price(
                request.raw_entry_zone_low, PricePurpose.ENTRY_ZONE_LOW
            ).rounded_order_price
            zone_high = self.market_rules.round_price(
                request.raw_entry_zone_high, PricePurpose.ENTRY_ZONE_HIGH
            ).rounded_order_price
            if zone_low > zone_high:
                raise BistSignalConfigurationError("Yuvarlanmis giris bolgesi gecersiz.")

        created_at = _utc(request.created_at)
        data_timestamp = _utc(request.data_timestamp)
        valid_from = _utc(request.valid_from) if request.valid_from else created_at
        expires_at = _utc(request.expires_at) if request.expires_at else None
        creation_price = as_decimal(request.creation_price, field_name="creation_price")
        score = as_decimal(request.score, field_name="score")
        risk_reward = (
            as_decimal(request.risk_reward, field_name="risk_reward")
            if request.risk_reward is not None
            else (targets[2] - entry) / (entry - stop)
        )
        target_quantities = allocate_target_lots(request.requested_quantity, allocations)
        identity = request.idempotency_key or _stable_key(
            "bist-signal",
            request.user_id,
            symbol,
            request.timeframe,
            request.strategy_version,
            data_timestamp.isoformat(),
            raw_entry,
        )
        if len(identity) > 128:
            identity = _stable_key(identity)

        existing = self.db.query(Signal).filter(Signal.idempotency_key == identity).one_or_none()
        if existing is not None:
            if existing.user_id != request.user_id:
                raise BistSignalOwnershipError("Idempotency anahtari baska kullaniciya ait.")
            return existing

        signal = Signal(
            user_id=request.user_id,
            symbol=symbol,
            side="BUY",
            timeframe=request.timeframe.strip(),
            signal_type=SignalTypeEnum.BUY_CANDIDATE,
            state=_db_status(SignalStatus.PENDING_ENTRY),
            score=float(score),
            confidence=request.confidence.strip()[:16] or "orta",
            entry_order_type=request.entry_order_type.value,
            entry_zone_low=float(zone_low) if zone_low is not None else None,
            entry_zone_high=float(zone_high) if zone_high is not None else None,
            entry_trigger=float(entry),
            planned_entry_price=entry,
            raw_planned_entry_price=raw_entry,
            actual_entry_price=None,
            requested_quantity=Decimal(request.requested_quantity),
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("0"),
            average_fill_price=None,
            stop_price=float(stop),
            current_stop_price=stop,
            invalidation_price=raw_stop,
            target_1=float(targets[0]),
            target_2=float(targets[1]),
            target_3=float(targets[2]),
            risk_reward=float(risk_reward),
            analysis_mode="confirmed_close",
            trading_date=data_timestamp,
            data_timestamp=data_timestamp,
            provider=request.provider.strip()[:32],
            source=request.source.strip()[:48],
            idempotency_key=identity,
            strategy_version=request.strategy_version.strip()[:16],
            valid_from=valid_from,
            expires_at=expires_at,
            price_adjustment_mode=request.price_adjustment_mode.strip()[:24],
            market_rule_version=self.market_rules.version[:32],
            row_version=1,
            created_at=created_at,
        )
        try:
            self.db.add(signal)
            self.db.flush()
            for index, (raw, rounded, allocation, quantity) in enumerate(
                zip(raw_targets, targets, allocations, target_quantities), start=1
            ):
                self.db.add(
                    SignalTarget(
                        signal_id=signal.id,
                        target_number=index,
                        raw_target_price=raw,
                        target_price=rounded,
                        allocation_percent=allocation,
                        target_quantity=Decimal(quantity),
                        status="PENDING",
                    )
                )
            creation_key = _stable_key(identity, SignalEventType.SIGNAL_CREATED.value)
            self.db.add(
                SignalEvent(
                    signal_id=signal.id,
                    from_state=None,
                    to_state=SignalStatus.PENDING_ENTRY.value,
                    event_type=SignalEventType.SIGNAL_CREATED.value,
                    price_at_event=float(creation_price),
                    planned_price=entry,
                    requested_quantity=Decimal(request.requested_quantity),
                    executed_quantity=Decimal("0"),
                    trading_date=data_timestamp,
                    candle_open_time=data_timestamp,
                    provider=request.provider.strip()[:48],
                    source=request.source.strip()[:48],
                    metadata_json=json.dumps(
                        {
                            "side": "BUY",
                            "long_only": True,
                            "raw_entry_price": str(raw_entry),
                            "rounded_entry_price": str(entry),
                            "raw_stop_price": str(raw_stop),
                            "rounded_stop_price": str(stop),
                            "market_rule_version": self.market_rules.version,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    unique_dedup_key=creation_key,
                    created_at=created_at,
                )
            )
            self.db.commit()
            self.db.refresh(signal)
            return signal
        except IntegrityError:
            self.db.rollback()
            concurrent = self.db.query(Signal).filter(Signal.idempotency_key == identity).one_or_none()
            if concurrent is not None and concurrent.user_id == request.user_id:
                return concurrent
            raise
        except Exception:
            self.db.rollback()
            raise

    def restore_lifecycle(self, signal: Signal, *, user_id: int | None = None) -> SignalLifecycle:
        if user_id is not None and signal.user_id != user_id:
            raise BistSignalOwnershipError("Bu sinyal baska bir kullaniciya ait.")
        rows = (
            self.db.query(SignalEvent)
            .filter(SignalEvent.signal_id == signal.id)
            .order_by(SignalEvent.created_at, SignalEvent.id)
            .all()
        )
        events: list[SignalDomainEvent] = []
        for row in rows:
            try:
                event_type = SignalEventType(row.event_type)
            except (TypeError, ValueError):
                continue
            new_status = _domain_status(row.to_state)
            previous_status = _domain_status(row.from_state or row.to_state)
            event_time = row.candle_open_time or row.trading_date or row.created_at
            metadata = tuple(sorted(_json_load(row.metadata_json).items(), key=lambda item: item[0]))
            events.append(
                SignalDomainEvent(
                    signal_key=str(signal.id),
                    event_type=event_type,
                    previous_status=previous_status,
                    new_status=new_status,
                    event_time=_utc(event_time),
                    dedup_key=row.unique_dedup_key or f"legacy-signal-event:{row.id}",
                    market_price=_decimal_or_none(row.price_at_event),
                    planned_price=_decimal_or_none(row.planned_price),
                    execution_price=_decimal_or_none(row.execution_price),
                    requested_quantity=int(row.requested_quantity) if row.requested_quantity is not None else None,
                    executed_quantity=int(row.executed_quantity) if row.executed_quantity is not None else None,
                    provider=row.provider,
                    source=row.source,
                    metadata=metadata,
                )
            )
        return SignalLifecycle.restore(str(signal.id), _domain_status(signal.state), events)

    def _persist_domain_event(self, signal: Signal, event: SignalDomainEvent) -> SignalEvent:
        existing = (
            self.db.query(SignalEvent)
            .filter(SignalEvent.unique_dedup_key == event.dedup_key)
            .one_or_none()
        )
        if existing is not None:
            if existing.signal_id != signal.id or existing.event_type != event.event_type.value:
                raise BistSignalTransitionError("Dedup anahtari baska bir olayla cakisti.")
            return existing
        metadata = _metadata_for_json(event.metadata)
        row = SignalEvent(
            signal_id=signal.id,
            from_state=event.previous_status.value,
            to_state=event.new_status.value,
            event_type=event.event_type.value,
            price_at_event=float(event.market_price) if event.market_price is not None else None,
            planned_price=event.planned_price,
            execution_price=event.execution_price,
            requested_quantity=(Decimal(event.requested_quantity) if event.requested_quantity is not None else None),
            executed_quantity=(Decimal(event.executed_quantity) if event.executed_quantity is not None else None),
            trading_date=event.event_time,
            candle_open_time=event.event_time,
            provider=event.provider,
            source=event.source,
            metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str),
            unique_dedup_key=event.dedup_key,
            created_at=event.event_time,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _transition(
        self,
        signal: Signal,
        lifecycle: SignalLifecycle,
        new_status: SignalStatus,
        event_type: SignalEventType,
        observation: CandleObservation,
        source: str,
        *,
        planned_price: DecimalLike | None = None,
        execution_price: DecimalLike | None = None,
        requested_quantity: int | None = None,
        executed_quantity: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        target_number: int | None = None,
    ) -> SignalEvent | None:
        key = build_event_dedup_key(
            str(signal.id),
            event_type,
            _observation_key(observation, source),
            target_number=target_number,
        )
        frozen_metadata = _event_metadata(observation, metadata)
        outcome = lifecycle.transition(
            new_status,
            event_type,
            event_time=observation.timestamp,
            dedup_key=key,
            market_price=observation.close,
            planned_price=planned_price,
            execution_price=execution_price,
            requested_quantity=requested_quantity,
            executed_quantity=executed_quantity,
            provider=observation.provider,
            source=source,
            metadata=frozen_metadata,
        )
        if outcome.duplicate:
            return None
        if not outcome.applied or outcome.event is None:
            if outcome.error is None:
                raise BistSignalTransitionError("Bilinmeyen gecis hatasi")
            self._raise_rejected_transition(
                signal=signal,
                error=outcome.error,
                observation=observation,
                source=source,
                metadata=frozen_metadata,
            )
        signal.state = _db_status(new_status)
        signal.row_version = int(signal.row_version or 0) + 1
        return self._persist_domain_event(signal, outcome.event)

    def _record_event(
        self,
        signal: Signal,
        lifecycle: SignalLifecycle,
        event_type: SignalEventType,
        observation: CandleObservation,
        source: str,
        *,
        execution_price: DecimalLike | None = None,
        metadata: Mapping[str, Any] | None = None,
        suffix: str = "",
    ) -> SignalEvent | None:
        observation_key = _observation_key(observation, source) + suffix
        key = build_event_dedup_key(str(signal.id), event_type, observation_key)
        frozen_metadata = _event_metadata(observation, metadata)
        outcome = lifecycle.record_event(
            event_type,
            event_time=observation.timestamp,
            dedup_key=key,
            market_price=observation.close,
            execution_price=execution_price,
            provider=observation.provider,
            source=source,
            metadata=frozen_metadata,
        )
        if outcome.duplicate:
            return None
        if not outcome.applied or outcome.event is None:
            if outcome.error is None:
                raise BistSignalTransitionError("Durum degistirmeyen olay kaydedilemedi.")
            self._raise_rejected_transition(
                signal=signal,
                error=outcome.error,
                observation=observation,
                source=source,
                metadata=frozen_metadata,
            )
        return self._persist_domain_event(signal, outcome.event)

    @staticmethod
    def _target_rows(db: Session, signal_id: int) -> list[SignalTarget]:
        return (
            db.query(SignalTarget)
            .filter(SignalTarget.signal_id == signal_id)
            .order_by(SignalTarget.target_number)
            .all()
        )

    def apply_corporate_action_adjustment(
        self,
        signal_id: int,
        user_id: int,
        *,
        action_type: str,
        adjustment_factor: DecimalLike,
        effective_at: datetime,
        provider: str,
        source: str,
        corporate_action_key: str,
    ) -> SignalEvent:
        """Atomically adjust an owned open signal for a share-count action.

        ``adjustment_factor`` is always *shares after / shares before*.  Thus a
        2-for-1 split uses ``2`` and a 1-for-10 reverse split uses ``0.1``.
        Prices are divided by the factor and quantities are multiplied by it.
        Already-realized P&L/cost amounts are intentionally retained: their
        adjusted execution price and quantity still represent the same
        economic transaction.

        The immutable ``CORPORATE_ACTION_APPLIED`` event is state preserving.
        This is intentional: using the legacy transient
        ``CORPORATE_ACTION_ADJUSTED`` status would remove the signal from the
        monitor until a second recovery transition happened.  A commit stores
        the complete adjustment and its audit event together, while a retry
        returns the existing event without applying the factor twice.
        """

        normalized_action = _CORPORATE_ACTION_TYPES.get(
            action_type.strip().upper().replace("-", "_").replace(" ", "_")
        )
        if normalized_action is None:
            raise BistSignalConfigurationError(
                "Yalniz stock split, bonus/bedelsiz ve reverse split duzeltilebilir."
            )
        try:
            factor = as_decimal(adjustment_factor, field_name="adjustment_factor")
        except ValueError as exc:
            raise BistSignalConfigurationError("Sermaye islemi katsayisi gecerli degil.") from exc
        if factor <= 0 or factor == 1:
            raise BistSignalConfigurationError("Sermaye islemi katsayisi pozitif ve 1'den farkli olmalidir.")
        if normalized_action in {"STOCK_SPLIT", "BONUS_ISSUE"} and factor <= 1:
            raise BistSignalConfigurationError("Split/bedelsiz icin pay katsayisi 1'den buyuk olmalidir.")
        if normalized_action == "REVERSE_SPLIT" and factor >= 1:
            raise BistSignalConfigurationError("Reverse split icin pay katsayisi 0 ile 1 arasinda olmalidir.")

        provider_name = provider.strip()[:48]
        source_name = source.strip()[:48]
        action_key = corporate_action_key.strip()
        if not provider_name or not source_name or not action_key:
            raise BistSignalConfigurationError(
                "Provider, source ve kalici corporate_action_key zorunludur."
            )
        event_time = _utc(effective_at)
        dedup_key = build_event_dedup_key(
            str(signal_id),
            SignalEventType.CORPORATE_ACTION_APPLIED,
            f"corporate-action:{action_key}",
        )

        def _verify_existing(row: SignalEvent) -> SignalEvent:
            if row.signal_id != signal_id or row.event_type != SignalEventType.CORPORATE_ACTION_APPLIED.value:
                raise BistSignalTransitionError("Corporate action dedup anahtari baska bir olayla cakisti.")
            metadata = _json_load(row.metadata_json)
            try:
                stored_factor = as_decimal(metadata.get("adjustment_factor"), field_name="stored_factor")
            except ValueError as exc:
                raise BistSignalTransitionError("Kayitli corporate action audit verisi gecersiz.") from exc
            if (
                metadata.get("corporate_action_key") != action_key
                or metadata.get("action_type") != normalized_action
                or stored_factor != factor
                or metadata.get("effective_at") != event_time.isoformat()
            ):
                raise BistSignalTransitionError(
                    "Ayni corporate_action_key farkli bir sermaye islemiyle kullanilamaz."
                )
            return row

        def _exact_adjusted_price(value: Any) -> Decimal | None:
            if value is None:
                return None
            adjusted = as_decimal(value, field_name="corporate_action_price") / factor
            if adjusted <= 0:
                raise BistSignalConfigurationError("Duzeltilmis fiyat sifirdan buyuk olmalidir.")
            return _price_precision(adjusted)

        def _order_price(value: Any, purpose: PricePurpose) -> Decimal | None:
            exact = _exact_adjusted_price(value)
            if exact is None:
                return None
            return self.market_rules.round_price(exact, purpose).rounded_order_price

        def _adjusted_quantity(value: Any, field_name: str) -> Decimal | None:
            if value is None:
                return None
            raw = as_decimal(value, field_name=field_name) * factor
            adjusted = _quantity_precision(raw)
            if adjusted != raw:
                raise BistSignalConfigurationError(
                    f"{field_name} veritabani lot hassasiyetine sigmiyor; nakit/kusurat uzlastirmasi gerekli."
                )
            lot_size = Decimal(self.market_rules.lot_size)
            if adjusted % lot_size != 0:
                raise BistSignalConfigurationError(
                    f"{field_name} tam BIST lotuna donusmuyor; nakit/kusurat uzlastirmasi gerekli."
                )
            return adjusted

        try:
            signal = self._owned_signal(signal_id, user_id, lock=True)
            existing = (
                self.db.query(SignalEvent)
                .filter(SignalEvent.unique_dedup_key == dedup_key)
                .one_or_none()
            )
            if existing is not None:
                return _verify_existing(existing)

            status = _domain_status(signal.state)
            if status in FINAL_STATUSES:
                raise BistSignalTransitionError("Nihai durumdaki sinyale sermaye islemi uygulanamaz.")
            targets = self._target_rows(self.db, signal.id)
            if len(targets) != 3:
                raise BistSignalConfigurationError("Sinyalin TP1/TP2/TP3 satirlari eksik.")

            action_record = (
                self.db.query(CorporateActionRecord)
                .filter(
                    CorporateActionRecord.symbol == signal.symbol,
                    CorporateActionRecord.corporate_action_type == normalized_action,
                    CorporateActionRecord.effective_date == event_time,
                )
                .one_or_none()
            )
            if action_record is None:
                action_record = CorporateActionRecord(
                    symbol=signal.symbol,
                    corporate_action_type=normalized_action,
                    effective_date=event_time,
                    adjustment_factor=float(factor),
                    share_ratio=float(factor),
                    source=provider_name,
                    payload_json=json.dumps(
                        {
                            "corporate_action_key": action_key,
                            "source": source_name,
                            "factor_semantics": "shares_after_divided_by_shares_before",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
                self.db.add(action_record)
                self.db.flush()
            else:
                stored_factor = as_decimal(
                    action_record.adjustment_factor,
                    field_name="stored_adjustment_factor",
                )
                if stored_factor != factor:
                    raise BistSignalTransitionError(
                        "Ayni sembol/tarih sermaye islemi farkli katsayiyla kayitli."
                    )

            quantity_fields = (
                "requested_quantity",
                "filled_quantity",
                "remaining_quantity",
            )
            adjusted_signal_quantities = {
                field: _adjusted_quantity(getattr(signal, field), field)
                for field in quantity_fields
            }
            adjusted_target_quantities = [
                (
                    _adjusted_quantity(target.target_quantity, f"target_{target.target_number}_quantity"),
                    _adjusted_quantity(target.realized_quantity, f"target_{target.target_number}_realized_quantity"),
                )
                for target in targets
            ]

            entry_purpose = (
                PricePurpose.BREAKOUT_TRIGGER
                if signal.entry_order_type == EntryOrderType.BREAKOUT_BUY.value
                else PricePurpose.BUY_LIMIT
            )
            before_snapshot = {
                "status": status.value,
                "price_adjustment_mode": signal.price_adjustment_mode,
                "planned_entry_price": str(signal.planned_entry_price),
                "average_fill_price": str(signal.average_fill_price),
                "current_stop_price": str(signal.current_stop_price),
                "requested_quantity": str(signal.requested_quantity),
                "filled_quantity": str(signal.filled_quantity),
                "remaining_quantity": str(signal.remaining_quantity),
                "targets": [
                    {
                        "number": target.target_number,
                        "price": str(target.target_price),
                        "target_quantity": str(target.target_quantity),
                        "execution_price": str(target.execution_price),
                        "realized_quantity": str(target.realized_quantity),
                        "gross_pnl": str(target.gross_pnl),
                        "costs": str(target.costs),
                        "net_pnl": str(target.net_pnl),
                    }
                    for target in targets
                ],
            }

            signal.raw_planned_entry_price = _exact_adjusted_price(signal.raw_planned_entry_price)
            signal.planned_entry_price = _order_price(signal.planned_entry_price, entry_purpose)
            signal.entry_trigger = (
                float(_order_price(signal.entry_trigger, entry_purpose))
                if signal.entry_trigger is not None
                else None
            )
            signal.entry_zone_low = (
                float(_order_price(signal.entry_zone_low, PricePurpose.ENTRY_ZONE_LOW))
                if signal.entry_zone_low is not None
                else None
            )
            signal.entry_zone_high = (
                float(_order_price(signal.entry_zone_high, PricePurpose.ENTRY_ZONE_HIGH))
                if signal.entry_zone_high is not None
                else None
            )
            signal.actual_entry_price = _exact_adjusted_price(signal.actual_entry_price)
            signal.average_fill_price = _exact_adjusted_price(signal.average_fill_price)
            signal.stop_price = (
                float(_order_price(signal.stop_price, PricePurpose.PROTECTIVE_STOP_LONG))
                if signal.stop_price is not None
                else None
            )
            signal.current_stop_price = _order_price(
                signal.current_stop_price, PricePurpose.PROTECTIVE_STOP_LONG
            )
            signal.invalidation_price = _exact_adjusted_price(signal.invalidation_price)
            for field, value in adjusted_signal_quantities.items():
                setattr(signal, field, value)

            adjusted_signal_targets: list[Decimal] = []
            for target, (target_quantity, realized_quantity) in zip(
                targets, adjusted_target_quantities
            ):
                target.raw_target_price = _exact_adjusted_price(target.raw_target_price)
                target.target_price = _order_price(target.target_price, PricePurpose.TARGET_LONG)
                target.execution_price = _exact_adjusted_price(target.execution_price)
                target.target_quantity = target_quantity
                target.realized_quantity = realized_quantity
                adjusted_signal_targets.append(as_decimal(target.target_price))
            signal.target_1 = float(adjusted_signal_targets[0])
            signal.target_2 = float(adjusted_signal_targets[1])
            signal.target_3 = float(adjusted_signal_targets[2])
            signal.price_adjustment_mode = "split_adjusted"
            signal.row_version = int(signal.row_version or 0) + 1

            after_snapshot = {
                "status": status.value,
                "price_adjustment_mode": signal.price_adjustment_mode,
                "planned_entry_price": str(signal.planned_entry_price),
                "average_fill_price": str(signal.average_fill_price),
                "current_stop_price": str(signal.current_stop_price),
                "requested_quantity": str(signal.requested_quantity),
                "filled_quantity": str(signal.filled_quantity),
                "remaining_quantity": str(signal.remaining_quantity),
                "targets": [
                    {
                        "number": target.target_number,
                        "price": str(target.target_price),
                        "target_quantity": str(target.target_quantity),
                        "execution_price": str(target.execution_price),
                        "realized_quantity": str(target.realized_quantity),
                        # Monetary realized fields must not be scaled.
                        "gross_pnl": str(target.gross_pnl),
                        "costs": str(target.costs),
                        "net_pnl": str(target.net_pnl),
                    }
                    for target in targets
                ],
            }

            lifecycle = self.restore_lifecycle(signal, user_id=user_id)
            outcome = lifecycle.record_event(
                SignalEventType.CORPORATE_ACTION_APPLIED,
                event_time=event_time,
                dedup_key=dedup_key,
                provider=provider_name,
                source=source_name,
                metadata={
                    "corporate_action_key": action_key,
                    "corporate_action_record_id": action_record.id,
                    "action_type": normalized_action,
                    "adjustment_factor": str(factor),
                    "effective_at": event_time.isoformat(),
                    "factor_semantics": "shares_after_divided_by_shares_before",
                    "resume_status": status.value,
                    "before_snapshot_json": json.dumps(
                        before_snapshot, ensure_ascii=False, sort_keys=True
                    ),
                    "after_snapshot_json": json.dumps(
                        after_snapshot, ensure_ascii=False, sort_keys=True
                    ),
                },
            )
            if not outcome.applied or outcome.event is None:
                if outcome.duplicate:
                    duplicate = (
                        self.db.query(SignalEvent)
                        .filter(SignalEvent.unique_dedup_key == dedup_key)
                        .one()
                    )
                    return _verify_existing(duplicate)
                raise BistSignalTransitionError("Corporate action audit olayi olusturulamadi.")
            row = self._persist_domain_event(signal, outcome.event)
            self.db.commit()
            self.db.refresh(row)
            return row
        except IntegrityError as exc:
            self.db.rollback()
            concurrent = (
                self.db.query(SignalEvent)
                .filter(SignalEvent.unique_dedup_key == dedup_key)
                .one_or_none()
            )
            if concurrent is not None:
                return _verify_existing(concurrent)
            raise BistSignalRuntimeError(
                "Corporate action duzeltmesi atomik olarak kaydedilemedi."
            ) from exc
        except Exception:
            self.db.rollback()
            raise

    @staticmethod
    def _entry_plan(signal: Signal) -> EntryPlan:
        try:
            order_type = EntryOrderType(signal.entry_order_type or EntryOrderType.LIMIT_BUY.value)
        except ValueError as exc:
            raise BistSignalConfigurationError(f"Gecersiz giris emir tipi: {signal.entry_order_type}") from exc
        return EntryPlan(
            order_type=order_type,
            requested_quantity=int(signal.requested_quantity or 0),
            created_at=signal.valid_from or signal.created_at,
            planned_entry_price=signal.planned_entry_price,
            entry_zone_low=signal.entry_zone_low,
            entry_zone_high=signal.entry_zone_high,
            breakout_level=signal.planned_entry_price,
            manual_entry_price=signal.planned_entry_price,
        )

    def _latest_breakout_confirmation(self, signal_id: int) -> CandleObservation | None:
        """Restore the immutable completed candle that confirmed a breakout.

        Confirmation is an event rather than mutable in-memory state, so a
        process restart still fills only at a later observation's open.
        """

        row = (
            self.db.query(SignalEvent)
            .filter(
                SignalEvent.signal_id == signal_id,
                SignalEvent.event_type == SignalEventType.ENTRY_REACHED.value,
            )
            .order_by(SignalEvent.created_at.desc(), SignalEvent.id.desc())
            .first()
        )
        metadata = _json_load(row.metadata_json if row else None)
        if metadata.get("breakout_confirmation") is not True:
            return None
        item = metadata.get("observation")
        if not isinstance(item, dict):
            return None
        try:
            return CandleObservation(
                symbol=str(item["symbol"]),
                timestamp=_utc(datetime.fromisoformat(str(item["timestamp"]).replace("Z", "+00:00"))),
                timeframe=str(item.get("timeframe") or "1d"),
                open=item["open"],
                high=item["high"],
                low=item["low"],
                close=item["close"],
                volume=item["volume"],
                provider=str(item.get("provider") or row.provider or "unknown"),
                is_complete=item.get("bar_complete") is True,
                is_delayed=item.get("delayed") is True,
                safe_for_live_trigger=item.get("safe_for_live_trigger") is True,
                valid_transaction=item.get("valid_transaction") is True,
                trading_state=TradingState(str(item.get("trading_state") or TradingState.CONTINUOUS.value)),
                upper_limit=item.get("upper_limit"),
                lower_limit=item.get("lower_limit"),
                upper_limit_locked=item.get("upper_limit_locked") is True,
                lower_limit_locked=item.get("lower_limit_locked") is True,
                available_buy_quantity=item.get("available_buy_quantity"),
                available_sell_quantity=item.get("available_sell_quantity"),
                volume_ratio=item.get("volume_ratio"),
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("Kirilim teyit olayi geri yuklenemedi signal_id=%s", signal_id)
            return None

    @staticmethod
    def _latest_suspend_context(
        db: Session, signal_id: int
    ) -> tuple[SignalStatus, SignalEventType]:
        row = (
            db.query(SignalEvent)
            .filter(
                SignalEvent.signal_id == signal_id,
                SignalEvent.event_type.in_(
                    [
                        SignalEventType.TRADING_SUSPENDED.value,
                        SignalEventType.CIRCUIT_BREAKER_STARTED.value,
                    ]
                ),
            )
            .order_by(SignalEvent.created_at.desc(), SignalEvent.id.desc())
            .first()
        )
        metadata = _json_load(row.metadata_json if row else None)
        resume_event = (
            SignalEventType.CIRCUIT_BREAKER_ENDED
            if row is not None and row.event_type == SignalEventType.CIRCUIT_BREAKER_STARTED.value
            else SignalEventType.TRADING_RESUMED
        )
        return (
            _domain_status(metadata.get("resume_status", SignalStatus.PENDING_ENTRY.value)),
            resume_event,
        )

    def _mark_target_fill(
        self,
        signal: Signal,
        target: SignalTarget,
        fill_price: Decimal,
        fill_quantity: int,
    ) -> None:
        old_quantity = int(target.realized_quantity or 0)
        old_value = (_decimal_or_none(target.execution_price) or Decimal("0")) * old_quantity
        new_quantity = old_quantity + fill_quantity
        target.execution_price = _money((old_value + fill_price * fill_quantity) / new_quantity)
        target.realized_quantity = Decimal(new_quantity)
        gross = _money((fill_price - as_decimal(signal.average_fill_price)) * fill_quantity)
        entry_cost_total = self.cost_model.calculate(
            as_decimal(signal.average_fill_price) * int(signal.filled_quantity or 0)
        ).total
        entry_cost_share = _money(
            entry_cost_total * Decimal(fill_quantity) / Decimal(int(signal.filled_quantity or 1))
        )
        exit_cost = self.cost_model.calculate(fill_price * fill_quantity).total
        costs = entry_cost_share + exit_cost
        target.gross_pnl = _money((_decimal_or_none(target.gross_pnl) or Decimal("0")) + gross)
        target.costs = _money((_decimal_or_none(target.costs) or Decimal("0")) + costs)
        target.net_pnl = _money(as_decimal(target.gross_pnl) - as_decimal(target.costs))
        signal.remaining_quantity = max(
            Decimal("0"), as_decimal(signal.remaining_quantity or 0) - Decimal(fill_quantity)
        )

    def _persist_partial_target_event(
        self,
        signal: Signal,
        target: SignalTarget,
        observation: CandleObservation,
        source: str,
        fill_price: Decimal,
        fill_quantity: int,
    ) -> SignalEvent:
        key = _stable_key(
            signal.id,
            "TARGET_PARTIALLY_FILLED",
            target.target_number,
            _observation_key(observation, source),
        )
        existing = self.db.query(SignalEvent).filter(SignalEvent.unique_dedup_key == key).one_or_none()
        if existing is not None:
            return existing
        row = SignalEvent(
            signal_id=signal.id,
            from_state=_domain_status(signal.state).value,
            to_state=_domain_status(signal.state).value,
            event_type="TARGET_PARTIALLY_FILLED",
            price_at_event=float(observation.close),
            planned_price=target.target_price,
            execution_price=fill_price,
            requested_quantity=target.target_quantity,
            executed_quantity=Decimal(fill_quantity),
            trading_date=observation.timestamp,
            candle_open_time=observation.timestamp,
            provider=observation.provider,
            source=source,
            metadata_json=json.dumps(
                _event_metadata(
                    observation,
                    {
                    "target_number": target.target_number,
                    "remaining_target_quantity": str(
                        as_decimal(target.target_quantity) - as_decimal(target.realized_quantity or 0)
                    ),
                    "remaining_quantity": str(signal.remaining_quantity or 0),
                    },
                ),
                sort_keys=True,
            ),
            unique_dedup_key=key,
            created_at=observation.timestamp,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _move_stop_after_target(
        self,
        signal: Signal,
        lifecycle: SignalLifecycle,
        target_number: int,
        targets: list[SignalTarget],
        observation: CandleObservation,
        source: str,
    ) -> SignalEvent | None:
        new_stop: Decimal | None = None
        reason = ""
        if target_number == 1 and self.move_stop_to_breakeven_after_tp1:
            new_stop = self.market_rules.round_price(
                signal.average_fill_price, PricePurpose.PROTECTIVE_STOP_LONG
            ).rounded_order_price
            reason = "TP1 sonrasi stop giris fiyatina tasindi."
        elif target_number == 2 and self.move_stop_to_tp1_after_tp2:
            new_stop = as_decimal(targets[0].target_price)
            reason = "TP2 sonrasi stop TP1 seviyesine tasindi."
        if new_stop is None:
            return None
        old_stop = as_decimal(signal.current_stop_price or signal.stop_price)
        new_stop = self.market_rules.validate_long_stop_move(old_stop, new_stop)
        if new_stop <= old_stop:
            return None
        signal.current_stop_price = new_stop
        signal.row_version = int(signal.row_version or 0) + 1
        return self._record_event(
            signal,
            lifecycle,
            SignalEventType.STOP_MOVED,
            observation,
            source,
            execution_price=new_stop,
            metadata={"old_stop": str(old_stop), "new_stop": str(new_stop), "reason": reason},
            suffix=f":tp{target_number}",
        )

    def process_observation(
        self,
        signal_id: int,
        user_id: int,
        observation: CandleObservation,
        *,
        source: str = "bist_signal_monitor",
    ) -> ObservationProcessResult:
        """Apply one already-fetched observation atomically and exactly once."""

        if not source.strip():
            raise BistSignalConfigurationError("Observation source bos olamaz.")
        source = source.strip()[:48]
        try:
            signal = self._owned_signal(signal_id, user_id, lock=True)
            if observation.symbol != signal.symbol.upper().removesuffix(".IS"):
                raise BistSignalConfigurationError("Observation sembolu sinyalle uyusmuyor.")
            current_status = _domain_status(signal.state)
            last_timestamp = _utc(signal.data_timestamp) if signal.data_timestamp else None
            if last_timestamp is not None and observation.timestamp <= last_timestamp:
                return ObservationProcessResult(
                    signal.id,
                    current_status,
                    applied=False,
                    duplicate=observation.timestamp == last_timestamp,
                    out_of_order=observation.timestamp < last_timestamp,
                    detail="Observation daha once islendi veya sirasi eski.",
                )
            if current_status in {
                SignalStatus.TP3_HIT,
                SignalStatus.STOPPED,
                SignalStatus.EXPIRED,
                SignalStatus.INVALIDATED,
                SignalStatus.CANCELLED,
                SignalStatus.CLOSED_MANUALLY,
                SignalStatus.UNFILLED,
            }:
                return ObservationProcessResult(
                    signal.id, current_status, False, False, False, detail="Sinyal nihai durumda."
                )
            if (
                not observation.safe_for_live_trigger
                or (
                    observation.is_delayed
                    and not self.execution_policy.allow_delayed_data_for_live_trigger
                )
            ):
                return ObservationProcessResult(
                    signal.id,
                    current_status,
                    False,
                    False,
                    False,
                    fill_status=FillStatus.UNSAFE_DATA,
                    detail="Gecikmeli/guvensiz observation durum degisikligi icin kullanilmadi.",
                )

            lifecycle = self.restore_lifecycle(signal, user_id=user_id)
            targets = self._target_rows(self.db, signal.id)
            if len(targets) != 3:
                raise BistSignalConfigurationError("Sinyalin TP1/TP2/TP3 satirlari eksik.")
            event_rows: list[SignalEvent] = []
            fill_status: FillStatus | None = None
            entered_this_observation = False

            blocked = observation.trading_state in {
                TradingState.SUSPENDED,
                TradingState.CIRCUIT_BREAKER,
                TradingState.ORDER_COLLECTION,
                TradingState.CLOSED,
                TradingState.NO_VALID_TRADE,
            }
            if blocked:
                if lifecycle.status != SignalStatus.SUSPENDED:
                    event_type = (
                        SignalEventType.CIRCUIT_BREAKER_STARTED
                        if observation.trading_state == TradingState.CIRCUIT_BREAKER
                        else SignalEventType.TRADING_SUSPENDED
                    )
                    row = self._transition(
                        signal,
                        lifecycle,
                        SignalStatus.SUSPENDED,
                        event_type,
                        observation,
                        source,
                        metadata={
                            "market_state": observation.trading_state.value,
                            "resume_status": lifecycle.status.value,
                        },
                    )
                    if row is not None:
                        event_rows.append(row)
                signal.data_timestamp = observation.timestamp
                signal.provider = observation.provider[:32]
                signal.source = source
                self.db.commit()
                return ObservationProcessResult(
                    signal.id,
                    SignalStatus.SUSPENDED,
                    bool(event_rows),
                    False,
                    False,
                    tuple(row.id for row in event_rows),
                    tuple(row.event_type for row in event_rows),
                    FillStatus.SUSPENDED,
                    "Piyasa durumu nedeniyle izleme askida.",
                )

            if lifecycle.status == SignalStatus.SUSPENDED:
                resume_status, event_type = self._latest_suspend_context(self.db, signal.id)
                row = self._transition(
                    signal,
                    lifecycle,
                    resume_status,
                    event_type,
                    observation,
                    source,
                    metadata={"resumed_from": SignalStatus.SUSPENDED.value},
                )
                if row is not None:
                    event_rows.append(row)

            if lifecycle.status == SignalStatus.PENDING_ENTRY:
                valid_from = _utc(signal.valid_from) if signal.valid_from else _utc(signal.created_at)
                if observation.timestamp < valid_from:
                    return ObservationProcessResult(
                        signal.id,
                        lifecycle.status,
                        False,
                        False,
                        False,
                        fill_status=FillStatus.PENDING,
                        detail="Sinyal henuz gecerli degil; valid_from bekleniyor.",
                    )

                invalidation = _decimal_or_none(signal.invalidation_price)
                if invalidation is not None and observation.low <= invalidation:
                    row = self._transition(
                        signal,
                        lifecycle,
                        SignalStatus.INVALIDATED,
                        SignalEventType.ENTRY_INVALIDATED,
                        observation,
                        source,
                        planned_price=invalidation,
                        requested_quantity=int(signal.requested_quantity or 0),
                        executed_quantity=0,
                        metadata={
                            "reason": "Giris gerceklesmeden once gecersizlik seviyesi goruldu.",
                            "invalidation_price": str(invalidation),
                        },
                    )
                    if row is not None:
                        event_rows.append(row)
                    signal.closed_at = observation.timestamp
                    signal.monitoring_enabled = False

                previous_confirmation = None
                if (
                    lifecycle.status == SignalStatus.PENDING_ENTRY
                    and signal.entry_order_type == EntryOrderType.BREAKOUT_BUY.value
                ):
                    previous_confirmation = self._latest_breakout_confirmation(signal.id)
                fill = evaluate_entry(
                    self._entry_plan(signal),
                    observation,
                    previous_observation=previous_confirmation,
                    policy=self.execution_policy,
                    market_rules=self.market_rules,
                ) if lifecycle.status == SignalStatus.PENDING_ENTRY else None
                if fill is None:
                    fill_status = FillStatus.PENDING
                else:
                    fill_status = fill.status
                if fill is not None and fill.status == FillStatus.UNSAFE_DATA:
                    self.db.rollback()
                    return ObservationProcessResult(
                        signal.id,
                        lifecycle.status,
                        False,
                        False,
                        False,
                        detail=fill.reason,
                        fill_status=fill.status,
                    )
                if fill is not None and fill.status == FillStatus.UNFILLED_LIMIT_LOCK:
                    row = self._transition(
                        signal,
                        lifecycle,
                        SignalStatus.UNFILLED,
                        SignalEventType.ORDER_REMAINED_UNFILLED,
                        observation,
                        source,
                        planned_price=fill.planned_execution_price,
                        requested_quantity=fill.requested_quantity,
                        executed_quantity=0,
                        metadata={"reason": fill.reason, "fill_status": fill.status.value},
                    )
                    if row is not None:
                        event_rows.append(row)
                    signal.closed_at = observation.timestamp
                elif fill is not None and fill.has_fill:
                    event_type = (
                        SignalEventType.ENTRY_PARTIALLY_FILLED
                        if fill.status == FillStatus.PARTIALLY_FILLED
                        else SignalEventType.ENTRY_FILLED
                    )
                    row = self._transition(
                        signal,
                        lifecycle,
                        SignalStatus.ACTIVE,
                        event_type,
                        observation,
                        source,
                        planned_price=fill.planned_execution_price,
                        execution_price=fill.actual_execution_price,
                        requested_quantity=fill.requested_quantity,
                        executed_quantity=fill.filled_quantity,
                        metadata={
                            "fill_method": fill.fill_method,
                            "fill_status": fill.status.value,
                            "unfilled_entry_quantity": fill.remaining_quantity,
                            "remaining_quantity": fill.filled_quantity,
                        },
                    )
                    if row is not None:
                        event_rows.append(row)
                    signal.actual_entry_price = fill.actual_execution_price
                    signal.average_fill_price = fill.actual_execution_price
                    signal.filled_quantity = Decimal(fill.filled_quantity)
                    signal.remaining_quantity = Decimal(fill.filled_quantity)
                    signal.activated_at = observation.timestamp
                    signal.fill_method = fill.fill_method[:48]
                    signal.fill_source = fill.fill_source[:48]
                    entered_this_observation = True
                    quantities = allocate_target_lots(
                        fill.filled_quantity,
                        tuple(as_decimal(target.allocation_percent) for target in targets),
                    )
                    for target, quantity in zip(targets, quantities):
                        target.target_quantity = Decimal(quantity)
                elif (
                    fill is not None
                    and fill.status == FillStatus.PENDING
                    and signal.entry_order_type == EntryOrderType.BREAKOUT_BUY.value
                    and previous_confirmation is None
                    and observation.is_complete
                    and observation.close > as_decimal(signal.planned_entry_price)
                ):
                    row = self._record_event(
                        signal,
                        lifecycle,
                        SignalEventType.ENTRY_REACHED,
                        observation,
                        source,
                        metadata={
                            "breakout_confirmation": True,
                            "confirmation_mode": BreakoutConfirmationMode.COMPLETED_CLOSE.value,
                            "next_action": "next_valid_observation_open",
                        },
                        suffix=":breakout-confirmed",
                    )
                    if row is not None:
                        event_rows.append(row)

            if lifecycle.status == SignalStatus.EXIT_PENDING:
                remaining = int(signal.remaining_quantity or 0)
                if remaining > 0:
                    still_locked = (
                        observation.lower_limit_locked
                        and (
                            observation.available_buy_quantity is None
                            or observation.available_buy_quantity <= 0
                        )
                    )
                    if still_locked:
                        fill_status = FillStatus.EXIT_PENDING_LIMIT_LOCK
                    else:
                        fill = evaluate_long_exit(
                            ExitOrderType.MANUAL,
                            signal.current_stop_price or signal.stop_price,
                            remaining,
                            observation,
                            policy=self.execution_policy,
                            market_rules=self.market_rules,
                        )
                        fill_status = fill.status
                    if not still_locked and fill.has_fill:
                        new_remaining = max(0, remaining - fill.filled_quantity)
                        if new_remaining == 0:
                            row = self._transition(
                                signal,
                                lifecycle,
                                SignalStatus.STOPPED,
                                SignalEventType.STOP_EXECUTED,
                                observation,
                                source,
                                planned_price=signal.current_stop_price or signal.stop_price,
                                execution_price=fill.actual_execution_price,
                                requested_quantity=remaining,
                                executed_quantity=fill.filled_quantity,
                                metadata={
                                    "delayed_exit": True,
                                    "fill_method": fill.fill_method,
                                    "remaining_quantity": new_remaining,
                                },
                            )
                        else:
                            row = self._record_event(
                                signal,
                                lifecycle,
                                SignalEventType.STOP_EXECUTION_DELAYED,
                                observation,
                                source,
                                execution_price=fill.actual_execution_price,
                                metadata={
                                    "delayed_exit": True,
                                    "partial_exit": True,
                                    "executed_quantity": fill.filled_quantity,
                                    "remaining_quantity": new_remaining,
                                },
                                suffix=":partial-stop-exit",
                            )
                        if row is not None:
                            event_rows.append(row)
                        signal.remaining_quantity = Decimal(new_remaining)
                        if int(signal.remaining_quantity) == 0:
                            signal.closed_at = observation.timestamp

            if (
                not entered_this_observation
                and lifecycle.status in {SignalStatus.ACTIVE, SignalStatus.TP1_HIT, SignalStatus.TP2_HIT}
            ):
                remaining = int(signal.remaining_quantity or 0)
                if remaining > 0:
                    stop_price = signal.current_stop_price or signal.stop_price
                    stop_fill = evaluate_long_exit(
                        ExitOrderType.STOP,
                        stop_price,
                        remaining,
                        observation,
                        policy=self.execution_policy,
                        market_rules=self.market_rules,
                    )
                    if stop_fill.status == FillStatus.EXIT_PENDING_LIMIT_LOCK:
                        fill_status = stop_fill.status
                        row = self._transition(
                            signal,
                            lifecycle,
                            SignalStatus.EXIT_PENDING,
                            SignalEventType.STOP_EXECUTION_DELAYED,
                            observation,
                            source,
                            planned_price=stop_price,
                            requested_quantity=remaining,
                            executed_quantity=0,
                            metadata={"reason": stop_fill.reason, "fill_status": stop_fill.status.value},
                        )
                        if row is not None:
                            event_rows.append(row)
                    elif stop_fill.has_fill:
                        fill_status = stop_fill.status
                        new_remaining = max(0, remaining - stop_fill.filled_quantity)
                        if new_remaining == 0:
                            row = self._transition(
                                signal,
                                lifecycle,
                                SignalStatus.STOPPED,
                                SignalEventType.STOP_EXECUTED,
                                observation,
                                source,
                                planned_price=stop_price,
                                execution_price=stop_fill.actual_execution_price,
                                requested_quantity=remaining,
                                executed_quantity=stop_fill.filled_quantity,
                                metadata={
                                    "fill_method": stop_fill.fill_method,
                                    "fill_status": stop_fill.status.value,
                                    "remaining_quantity": new_remaining,
                                },
                            )
                        else:
                            row = self._transition(
                                signal,
                                lifecycle,
                                SignalStatus.EXIT_PENDING,
                                SignalEventType.STOP_EXECUTION_DELAYED,
                                observation,
                                source,
                                planned_price=stop_price,
                                execution_price=stop_fill.actual_execution_price,
                                requested_quantity=remaining,
                                executed_quantity=stop_fill.filled_quantity,
                                metadata={
                                    "fill_method": stop_fill.fill_method,
                                    "fill_status": stop_fill.status.value,
                                    "partial_exit": True,
                                    "remaining_quantity": new_remaining,
                                },
                            )
                        if row is not None:
                            event_rows.append(row)
                        signal.remaining_quantity = Decimal(new_remaining)
                        if int(signal.remaining_quantity) == 0:
                            signal.closed_at = observation.timestamp

                # Conservative OHLC ordering: when stop and targets coexist in
                # one candle, the stop branch above wins and targets are skipped.
                if lifecycle.status in {SignalStatus.ACTIVE, SignalStatus.TP1_HIT, SignalStatus.TP2_HIT}:
                    for target in targets:
                        if target.status == "EXECUTED":
                            continue
                        # TP2 cannot be completed before TP1, nor TP3 before TP2.
                        if target.target_number > 1 and targets[target.target_number - 2].status != "EXECUTED":
                            break
                        target_remaining = int(target.target_quantity or 0) - int(target.realized_quantity or 0)
                        if target_remaining <= 0:
                            continue
                        target_fill = evaluate_long_exit(
                            ExitOrderType.TARGET,
                            target.target_price,
                            target_remaining,
                            observation,
                            policy=self.execution_policy,
                            market_rules=self.market_rules,
                        )
                        if not target_fill.has_fill:
                            break
                        fill_status = target_fill.status
                        fill_quantity = target_fill.filled_quantity
                        fill_price = as_decimal(target_fill.actual_execution_price)
                        self._mark_target_fill(signal, target, fill_price, fill_quantity)
                        if int(target.realized_quantity or 0) < int(target.target_quantity or 0):
                            target.status = "PARTIALLY_FILLED"
                            partial_row = self._persist_partial_target_event(
                                signal, target, observation, source, fill_price, fill_quantity
                            )
                            event_rows.append(partial_row)
                            break

                        target.status = "EXECUTED"
                        target.reached_at = observation.timestamp
                        target.executed_at = observation.timestamp
                        status = {
                            1: SignalStatus.TP1_HIT,
                            2: SignalStatus.TP2_HIT,
                            3: SignalStatus.TP3_HIT,
                        }[target.target_number]
                        event_type = {
                            1: SignalEventType.TP1_REACHED,
                            2: SignalEventType.TP2_REACHED,
                            3: SignalEventType.TP3_REACHED,
                        }[target.target_number]
                        row = self._transition(
                            signal,
                            lifecycle,
                            status,
                            event_type,
                            observation,
                            source,
                            planned_price=target.target_price,
                            execution_price=target.execution_price,
                            requested_quantity=int(target.target_quantity or 0),
                            executed_quantity=int(target.realized_quantity or 0),
                            metadata={
                                "target_number": target.target_number,
                                "gross_pnl": str(target.gross_pnl),
                                "costs": str(target.costs),
                                "net_pnl": str(target.net_pnl),
                                "remaining_quantity": str(signal.remaining_quantity or 0),
                            },
                            target_number=target.target_number,
                        )
                        if row is not None:
                            event_rows.append(row)
                        moved = self._move_stop_after_target(
                            signal,
                            lifecycle,
                            target.target_number,
                            targets,
                            observation,
                            source,
                        )
                        if moved is not None:
                            event_rows.append(moved)
                        if status == SignalStatus.TP3_HIT:
                            signal.remaining_quantity = Decimal("0")
                            signal.closed_at = observation.timestamp
                            break

            signal.data_timestamp = observation.timestamp
            signal.provider = observation.provider[:32]
            signal.source = source
            self.db.commit()
            return ObservationProcessResult(
                signal.id,
                _domain_status(signal.state),
                bool(event_rows),
                False,
                False,
                tuple(row.id for row in event_rows),
                tuple(row.event_type for row in event_rows),
                fill_status,
                "Observation islendi.",
            )
        except IntegrityError as exc:
            self.db.rollback()
            # Concurrent workers may race on the unique event key. If another
            # transaction already advanced this observation, treat it as the
            # required idempotent no-op; otherwise surface the integrity error.
            signal = self._owned_signal(signal_id, user_id)
            last_timestamp = _utc(signal.data_timestamp) if signal.data_timestamp else None
            if last_timestamp is not None and last_timestamp >= observation.timestamp:
                return ObservationProcessResult(
                    signal.id,
                    _domain_status(signal.state),
                    False,
                    True,
                    last_timestamp > observation.timestamp,
                    detail="Observation eszamanli bir calisan tarafindan islenmis.",
                )
            raise BistSignalRuntimeError("Sinyal olayi atomik olarak kaydedilemedi.") from exc
        except Exception:
            self.db.rollback()
            raise

    def expire_pending(
        self,
        signal_id: int,
        user_id: int,
        *,
        as_of: datetime,
        source: str = "bist_signal_monitor",
    ) -> ObservationProcessResult:
        """Expire a due entry plan without requiring a market-data quote.

        Expiry is a wall-clock lifecycle event, therefore it is allowed while
        Borsa Istanbul is closed.  The immutable event carries a dedicated
        ``system_clock`` provider and the configured expiry timestamp.
        """

        if not source.strip():
            raise BistSignalConfigurationError("Expiry source bos olamaz.")
        source = source.strip()[:48]
        evaluated_at = _utc(as_of)
        try:
            signal = self._owned_signal(signal_id, user_id, lock=True)
            current_status = _domain_status(signal.state)
            if current_status == SignalStatus.EXPIRED:
                return ObservationProcessResult(
                    signal.id,
                    current_status,
                    applied=False,
                    duplicate=True,
                    out_of_order=False,
                    detail="Sinyal daha once expire edildi.",
                )
            if signal.expires_at is None:
                raise BistSignalConfigurationError("Sinyalin expires_at degeri yok.")
            expires_at = _utc(signal.expires_at)
            if evaluated_at < expires_at:
                return ObservationProcessResult(
                    signal.id,
                    current_status,
                    applied=False,
                    duplicate=False,
                    out_of_order=False,
                    detail="Sinyalin suresi henuz dolmadi.",
                )

            lifecycle = self.restore_lifecycle(signal, user_id=user_id)
            price = signal.planned_entry_price or signal.entry_trigger or Decimal("0.01")
            observation = self._manual_observation(signal, expires_at, price, "system_clock")
            row = self._transition(
                signal,
                lifecycle,
                SignalStatus.EXPIRED,
                SignalEventType.SIGNAL_EXPIRED,
                observation,
                source,
                planned_price=price,
                metadata={
                    "expires_at": expires_at.isoformat(),
                    "evaluated_at": evaluated_at.isoformat(),
                    "reason": "Giris gerceklesmeden sinyal gecerlilik suresi doldu.",
                    "remaining_quantity": "0",
                },
            )
            if row is None:
                return ObservationProcessResult(
                    signal.id,
                    _domain_status(signal.state),
                    applied=False,
                    duplicate=True,
                    out_of_order=False,
                    detail="Expiry olayi daha once kaydedildi.",
                )
            signal.monitoring_enabled = False
            signal.closed_at = observation.timestamp
            signal.data_timestamp = observation.timestamp
            signal.provider = observation.provider[:32]
            signal.source = source
            self.db.commit()
            self.db.refresh(row)
            return ObservationProcessResult(
                signal.id,
                SignalStatus.EXPIRED,
                applied=True,
                duplicate=False,
                out_of_order=False,
                event_ids=(row.id,),
                event_types=(row.event_type,),
                detail="Sinyal giris gerceklesmeden expire edildi.",
            )
        except IntegrityError as exc:
            self.db.rollback()
            signal = self._owned_signal(signal_id, user_id)
            if _domain_status(signal.state) == SignalStatus.EXPIRED:
                return ObservationProcessResult(
                    signal.id,
                    SignalStatus.EXPIRED,
                    applied=False,
                    duplicate=True,
                    out_of_order=False,
                    detail="Expiry eszamanli bir calisan tarafindan kaydedildi.",
                )
            raise BistSignalRuntimeError("Expiry olayi atomik olarak kaydedilemedi.") from exc
        except Exception:
            self.db.rollback()
            raise

    @staticmethod
    def _manual_observation(
        signal: Signal,
        event_time: datetime,
        price: DecimalLike,
        provider: str,
    ) -> CandleObservation:
        timestamp = _utc(event_time)
        if signal.data_timestamp is not None:
            last = _utc(signal.data_timestamp)
            if timestamp <= last:
                timestamp = last + timedelta(microseconds=1)
        value = as_decimal(price, field_name="manual_price")
        return CandleObservation(
            symbol=signal.symbol,
            timestamp=timestamp,
            open=value,
            high=value,
            low=value,
            close=value,
            volume=Decimal("0"),
            timeframe="manual",
            provider=provider,
            is_complete=True,
            is_session_open=True,
            safe_for_live_trigger=True,
            valid_transaction=True,
            trading_state=TradingState.CONTINUOUS,
        )

    def set_monitoring(self, signal_id: int, user_id: int, enabled: bool) -> Signal:
        signal = self._owned_signal(signal_id, user_id, lock=True)
        if _domain_status(signal.state) in {
            SignalStatus.TP3_HIT, SignalStatus.STOPPED, SignalStatus.EXPIRED,
            SignalStatus.INVALIDATED, SignalStatus.CANCELLED,
            SignalStatus.CLOSED_MANUALLY, SignalStatus.UNFILLED,
        } and enabled:
            raise BistSignalTransitionError("Nihai durumdaki sinyal yeniden takibe alınamaz.")
        signal.monitoring_enabled = bool(enabled)
        signal.row_version = int(signal.row_version or 0) + 1
        self.db.commit(); self.db.refresh(signal)
        return signal

    def cancel_pending(
        self,
        signal_id: int,
        user_id: int,
        *,
        event_time: datetime,
        source: str = "telegram_manual",
    ) -> SignalEvent:
        signal = self._owned_signal(signal_id, user_id, lock=True)
        lifecycle = self.restore_lifecycle(signal, user_id=user_id)
        if lifecycle.status != SignalStatus.PENDING_ENTRY:
            raise BistSignalTransitionError("Yalnız PENDING_ENTRY sinyali iptal edilebilir.")
        price = signal.planned_entry_price or signal.entry_trigger
        observation = self._manual_observation(signal, event_time, price, "manual")
        row = self._transition(
            signal, lifecycle, SignalStatus.CANCELLED, SignalEventType.SIGNAL_CANCELLED,
            observation, source, planned_price=price,
            metadata={"manual": True, "reason": "Kullanıcı iptali", "remaining_quantity": "0"},
        )
        if row is None:
            raise BistSignalTransitionError("İptal olayı yinelendi.")
        signal.monitoring_enabled = False
        signal.closed_at = observation.timestamp
        signal.data_timestamp = observation.timestamp
        signal.provider = observation.provider[:32]
        signal.source = source[:48]
        self.db.commit(); self.db.refresh(row)
        return row

    def move_stop_to_breakeven(
        self,
        signal_id: int,
        user_id: int,
        *,
        event_time: datetime,
        source: str = "telegram_manual",
    ) -> SignalEvent:
        signal = self._owned_signal(signal_id, user_id, lock=True)
        lifecycle = self.restore_lifecycle(signal, user_id=user_id)
        if lifecycle.status not in {SignalStatus.ACTIVE, SignalStatus.TP1_HIT, SignalStatus.TP2_HIT}:
            raise BistSignalTransitionError("Stop yalnız aktif pozisyonda girişe taşınabilir.")
        if signal.average_fill_price is None:
            raise BistSignalConfigurationError("Gerçekleşmiş giriş fiyatı bulunamadı.")
        old_stop = as_decimal(signal.current_stop_price or signal.stop_price)
        new_stop = self.market_rules.round_price(
            signal.average_fill_price, PricePurpose.PROTECTIVE_STOP_LONG,
        ).rounded_order_price
        new_stop = self.market_rules.validate_long_stop_move(old_stop, new_stop)
        if new_stop <= old_stop:
            raise BistSignalTransitionError("Stop zaten giriş fiyatında veya daha yukarıda.")
        observation = self._manual_observation(signal, event_time, new_stop, "manual")
        signal.current_stop_price = new_stop
        signal.row_version = int(signal.row_version or 0) + 1
        row = self._record_event(
            signal, lifecycle, SignalEventType.STOP_MOVED, observation, source,
            execution_price=new_stop,
            metadata={"manual": True, "old_stop": str(old_stop), "new_stop": str(new_stop)},
            suffix=":manual-breakeven",
        )
        if row is None:
            raise BistSignalTransitionError("Stop taşıma olayı yinelendi.")
        signal.data_timestamp = observation.timestamp
        signal.provider = observation.provider[:32]
        signal.source = source[:48]
        self.db.commit(); self.db.refresh(row)
        return row

    def close_manually(
        self,
        signal_id: int,
        user_id: int,
        *,
        execution_price: DecimalLike,
        event_time: datetime,
        provider: str,
        source: str = "telegram_manual",
    ) -> SignalEvent:
        signal = self._owned_signal(signal_id, user_id, lock=True)
        lifecycle = self.restore_lifecycle(signal, user_id=user_id)
        if lifecycle.status not in {
            SignalStatus.ACTIVE, SignalStatus.TP1_HIT,
            SignalStatus.TP2_HIT, SignalStatus.EXIT_PENDING,
        }:
            raise BistSignalTransitionError("Yalnız açık pozisyon manuel kapatılabilir.")
        remaining = int(signal.remaining_quantity or 0)
        if remaining <= 0:
            raise BistSignalTransitionError("Kapatılacak kalan lot bulunamadı.")
        price = as_decimal(execution_price, field_name="execution_price")
        observation = self._manual_observation(signal, event_time, price, provider)
        row = self._transition(
            signal, lifecycle, SignalStatus.CLOSED_MANUALLY,
            SignalEventType.POSITION_CLOSED_MANUALLY, observation, source,
            execution_price=price, requested_quantity=remaining,
            executed_quantity=remaining,
            metadata={
                "manual": True,
                "remaining_quantity_before": remaining,
                "remaining_quantity": "0",
            },
        )
        if row is None:
            raise BistSignalTransitionError("Manuel kapanış olayı yinelendi.")
        signal.remaining_quantity = Decimal("0")
        signal.monitoring_enabled = False
        signal.closed_at = observation.timestamp
        signal.data_timestamp = observation.timestamp
        signal.provider = observation.provider[:32]
        signal.source = source[:48]
        self.db.commit(); self.db.refresh(row)
        return row
