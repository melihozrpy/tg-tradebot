"""Pure signal lifecycle and immutable, idempotent domain events."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

from app.signals.enums import SignalEventType, SignalStatus
from app.signals.market_rules import DecimalLike, as_decimal


logger = logging.getLogger("mergen_quant.signals.lifecycle")


FINAL_STATUSES = frozenset(
    {
        SignalStatus.TP3_HIT,
        SignalStatus.STOPPED,
        SignalStatus.EXPIRED,
        SignalStatus.INVALIDATED,
        SignalStatus.CANCELLED,
        SignalStatus.CLOSED_MANUALLY,
        SignalStatus.UNFILLED,
    }
)

MONITORED_STATUSES = frozenset(
    {
        SignalStatus.PENDING_ENTRY,
        SignalStatus.ACTIVE,
        SignalStatus.TP1_HIT,
        SignalStatus.TP2_HIT,
        SignalStatus.EXIT_PENDING,
    }
)

VALID_TRANSITIONS: Mapping[SignalStatus, frozenset[SignalStatus]] = {
    SignalStatus.PENDING_ENTRY: frozenset(
        {
            SignalStatus.ACTIVE,
            SignalStatus.EXPIRED,
            SignalStatus.INVALIDATED,
            SignalStatus.CANCELLED,
            SignalStatus.UNFILLED,
            SignalStatus.SUSPENDED,
            SignalStatus.CORPORATE_ACTION_ADJUSTED,
        }
    ),
    SignalStatus.ACTIVE: frozenset(
        {
            SignalStatus.TP1_HIT,
            SignalStatus.STOPPED,
            SignalStatus.CLOSED_MANUALLY,
            SignalStatus.EXIT_PENDING,
            SignalStatus.SUSPENDED,
            SignalStatus.CORPORATE_ACTION_ADJUSTED,
        }
    ),
    SignalStatus.TP1_HIT: frozenset(
        {
            SignalStatus.TP2_HIT,
            SignalStatus.STOPPED,
            SignalStatus.CLOSED_MANUALLY,
            SignalStatus.EXIT_PENDING,
            SignalStatus.SUSPENDED,
            SignalStatus.CORPORATE_ACTION_ADJUSTED,
        }
    ),
    SignalStatus.TP2_HIT: frozenset(
        {
            SignalStatus.TP3_HIT,
            SignalStatus.STOPPED,
            SignalStatus.CLOSED_MANUALLY,
            SignalStatus.EXIT_PENDING,
            SignalStatus.SUSPENDED,
            SignalStatus.CORPORATE_ACTION_ADJUSTED,
        }
    ),
    SignalStatus.EXIT_PENDING: frozenset(
        {
            SignalStatus.STOPPED,
            SignalStatus.CLOSED_MANUALLY,
            SignalStatus.SUSPENDED,
            SignalStatus.ACTIVE,
            SignalStatus.TP1_HIT,
            SignalStatus.TP2_HIT,
            SignalStatus.CORPORATE_ACTION_ADJUSTED,
        }
    ),
    SignalStatus.SUSPENDED: frozenset(
        {
            SignalStatus.PENDING_ENTRY,
            SignalStatus.ACTIVE,
            SignalStatus.TP1_HIT,
            SignalStatus.TP2_HIT,
            SignalStatus.EXIT_PENDING,
            SignalStatus.EXPIRED,
            SignalStatus.INVALIDATED,
            SignalStatus.CANCELLED,
            SignalStatus.UNFILLED,
            SignalStatus.CORPORATE_ACTION_ADJUSTED,
        }
    ),
    SignalStatus.CORPORATE_ACTION_ADJUSTED: frozenset(
        {
            SignalStatus.PENDING_ENTRY,
            SignalStatus.ACTIVE,
            SignalStatus.TP1_HIT,
            SignalStatus.TP2_HIT,
            SignalStatus.EXIT_PENDING,
            SignalStatus.SUSPENDED,
            SignalStatus.EXPIRED,
            SignalStatus.INVALIDATED,
            SignalStatus.CANCELLED,
        }
    ),
}


_EXPECTED_STATUS_BY_EVENT: Mapping[SignalEventType, SignalStatus] = {
    SignalEventType.ENTRY_FILLED: SignalStatus.ACTIVE,
    SignalEventType.ENTRY_PARTIALLY_FILLED: SignalStatus.ACTIVE,
    SignalEventType.ENTRY_INVALIDATED: SignalStatus.INVALIDATED,
    SignalEventType.ORDER_REMAINED_UNFILLED: SignalStatus.UNFILLED,
    SignalEventType.SIGNAL_EXPIRED: SignalStatus.EXPIRED,
    SignalEventType.SIGNAL_CANCELLED: SignalStatus.CANCELLED,
    SignalEventType.TP1_REACHED: SignalStatus.TP1_HIT,
    SignalEventType.TP2_REACHED: SignalStatus.TP2_HIT,
    SignalEventType.TP3_REACHED: SignalStatus.TP3_HIT,
    SignalEventType.STOP_REACHED: SignalStatus.STOPPED,
    SignalEventType.STOP_EXECUTED: SignalStatus.STOPPED,
    SignalEventType.POSITION_CLOSED_MANUALLY: SignalStatus.CLOSED_MANUALLY,
    SignalEventType.EXIT_PENDING: SignalStatus.EXIT_PENDING,
    SignalEventType.TRADING_SUSPENDED: SignalStatus.SUSPENDED,
    SignalEventType.CORPORATE_ACTION_APPLIED: SignalStatus.CORPORATE_ACTION_ADJUSTED,
}


class LifecycleError(ValueError):
    pass


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _freeze_metadata_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(
            sorted(
                ((str(key), _freeze_metadata_value(item)) for key, item in value.items()),
                key=lambda pair: pair[0],
            )
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_metadata_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze_metadata_value(item) for item in value), key=repr))
    return value


def _metadata_tuple(metadata: Mapping[str, Any] | None) -> tuple[tuple[str, Any], ...]:
    if not metadata:
        return ()
    return tuple(
        sorted(
            ((str(key), _freeze_metadata_value(value)) for key, value in metadata.items()),
            key=lambda item: item[0],
        )
    )


def build_event_dedup_key(
    signal_key: str,
    event_type: SignalEventType,
    observation_key: str,
    *,
    target_number: int | None = None,
) -> str:
    """Build a stable key suitable for a database unique constraint.

    ``observation_key`` should identify the provider trade/tick or completed
    candle (for example ``provider:symbol:timeframe:timestamp``). It must not be
    the monitor run time, otherwise a restart could create a duplicate event.
    """

    payload = {
        "signal": signal_key.strip(),
        "event": event_type.value,
        "observation": observation_key.strip(),
        "target": target_number,
    }
    if not payload["signal"] or not payload["observation"]:
        raise LifecycleError("Sinyal ve gozlem anahtari bos olamaz.")
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SignalDomainEvent:
    signal_key: str
    event_type: SignalEventType
    previous_status: SignalStatus
    new_status: SignalStatus
    event_time: datetime
    dedup_key: str
    market_price: Decimal | None = None
    planned_price: Decimal | None = None
    execution_price: Decimal | None = None
    requested_quantity: int | None = None
    executed_quantity: int | None = None
    provider: str | None = None
    source: str | None = None
    metadata: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class TransitionErrorRecord:
    signal_key: str
    previous_status: SignalStatus
    attempted_status: SignalStatus
    event_type: SignalEventType
    event_time: datetime
    dedup_key: str
    reason: str


@dataclass(frozen=True, slots=True)
class TransitionOutcome:
    status: SignalStatus
    applied: bool
    duplicate: bool = False
    event: SignalDomainEvent | None = None
    error: TransitionErrorRecord | None = None


@dataclass(slots=True)
class SignalLifecycle:
    signal_key: str
    status: SignalStatus = SignalStatus.PENDING_ENTRY
    events: list[SignalDomainEvent] = field(default_factory=list)
    errors: list[TransitionErrorRecord] = field(default_factory=list)
    _events_by_key: dict[str, SignalDomainEvent] = field(init=False, default_factory=dict, repr=False)
    _error_keys: set[str] = field(init=False, default_factory=set, repr=False)

    def __post_init__(self) -> None:
        self.signal_key = self.signal_key.strip()
        if not self.signal_key:
            raise LifecycleError("Sinyal anahtari bos olamaz.")
        for event in self.events:
            if event.signal_key != self.signal_key:
                raise LifecycleError("Geri yuklenen olay baska bir sinyale ait.")
            existing = self._events_by_key.get(event.dedup_key)
            if existing is not None and existing != event:
                raise LifecycleError("Geri yuklenen olaylarda dedup anahtari cakismasi var.")
            self._events_by_key[event.dedup_key] = event
        self._error_keys.update(error.dedup_key for error in self.errors)

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES

    @property
    def is_monitored(self) -> bool:
        return self.status in MONITORED_STATUSES

    def has_event(self, dedup_key: str) -> bool:
        return dedup_key in self._events_by_key

    def _duplicate_outcome(
        self,
        existing: SignalDomainEvent,
        event_type: SignalEventType,
        new_status: SignalStatus,
        event_time: datetime,
        dedup_key: str,
    ) -> TransitionOutcome:
        if existing.event_type == event_type and existing.new_status == new_status:
            return TransitionOutcome(self.status, applied=False, duplicate=True, event=existing)
        return self._invalid(
            new_status,
            event_type,
            event_time,
            dedup_key,
            "Ayni dedup anahtari farkli bir olay icin kullanilamaz.",
        )

    def _invalid(
        self,
        new_status: SignalStatus,
        event_type: SignalEventType,
        event_time: datetime,
        dedup_key: str,
        reason: str,
    ) -> TransitionOutcome:
        record = TransitionErrorRecord(
            self.signal_key,
            self.status,
            new_status,
            event_type,
            _utc(event_time),
            dedup_key,
            reason,
        )
        if dedup_key not in self._error_keys:
            self.errors.append(record)
            self._error_keys.add(dedup_key)
            logger.warning(
                "Gecersiz sinyal gecisi signal=%s from=%s to=%s event=%s reason=%s",
                self.signal_key,
                self.status.value,
                new_status.value,
                event_type.value,
                reason,
            )
        return TransitionOutcome(self.status, applied=False, error=record)

    def transition(
        self,
        new_status: SignalStatus,
        event_type: SignalEventType,
        *,
        event_time: datetime,
        dedup_key: str,
        market_price: DecimalLike | None = None,
        planned_price: DecimalLike | None = None,
        execution_price: DecimalLike | None = None,
        requested_quantity: int | None = None,
        executed_quantity: int | None = None,
        provider: str | None = None,
        source: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> TransitionOutcome:
        dedup_key = dedup_key.strip()
        if not dedup_key:
            raise LifecycleError("Dedup anahtari bos olamaz.")
        existing = self._events_by_key.get(dedup_key)
        if existing is not None:
            return self._duplicate_outcome(existing, event_type, new_status, event_time, dedup_key)

        expected = _EXPECTED_STATUS_BY_EVENT.get(event_type)
        if expected is not None and expected != new_status:
            return self._invalid(
                new_status,
                event_type,
                event_time,
                dedup_key,
                f"{event_type.value} olayi {expected.value} durumuna gecmelidir.",
            )
        allowed = VALID_TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            return self._invalid(
                new_status,
                event_type,
                event_time,
                dedup_key,
                f"{self.status.value} -> {new_status.value} gecisine izin verilmiyor.",
            )

        event = SignalDomainEvent(
            signal_key=self.signal_key,
            event_type=event_type,
            previous_status=self.status,
            new_status=new_status,
            event_time=_utc(event_time),
            dedup_key=dedup_key,
            market_price=as_decimal(market_price, field_name="market_price") if market_price is not None else None,
            planned_price=as_decimal(planned_price, field_name="planned_price") if planned_price is not None else None,
            execution_price=(
                as_decimal(execution_price, field_name="execution_price")
                if execution_price is not None
                else None
            ),
            requested_quantity=requested_quantity,
            executed_quantity=executed_quantity,
            provider=provider,
            source=source,
            metadata=_metadata_tuple(metadata),
        )
        self.status = new_status
        self.events.append(event)
        self._events_by_key[dedup_key] = event
        return TransitionOutcome(self.status, applied=True, event=event)

    def record_event(
        self,
        event_type: SignalEventType,
        *,
        event_time: datetime,
        dedup_key: str,
        market_price: DecimalLike | None = None,
        execution_price: DecimalLike | None = None,
        provider: str | None = None,
        source: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> TransitionOutcome:
        """Record an immutable event that does not change lifecycle status."""

        dedup_key = dedup_key.strip()
        if not dedup_key:
            raise LifecycleError("Dedup anahtari bos olamaz.")
        existing = self._events_by_key.get(dedup_key)
        if existing is not None:
            if existing.event_type == event_type:
                return TransitionOutcome(self.status, applied=False, duplicate=True, event=existing)
            return self._invalid(
                self.status,
                event_type,
                event_time,
                dedup_key,
                "Ayni dedup anahtari farkli bir olay icin kullanilamaz.",
            )
        event = SignalDomainEvent(
            signal_key=self.signal_key,
            event_type=event_type,
            previous_status=self.status,
            new_status=self.status,
            event_time=_utc(event_time),
            dedup_key=dedup_key,
            market_price=as_decimal(market_price, field_name="market_price") if market_price is not None else None,
            execution_price=(
                as_decimal(execution_price, field_name="execution_price")
                if execution_price is not None
                else None
            ),
            provider=provider,
            source=source,
            metadata=_metadata_tuple(metadata),
        )
        self.events.append(event)
        self._events_by_key[dedup_key] = event
        return TransitionOutcome(self.status, applied=True, event=event)

    @classmethod
    def restore(
        cls,
        signal_key: str,
        status: SignalStatus,
        events: Iterable[SignalDomainEvent],
        errors: Iterable[TransitionErrorRecord] = (),
    ) -> "SignalLifecycle":
        return cls(signal_key, status, list(events), list(errors))
