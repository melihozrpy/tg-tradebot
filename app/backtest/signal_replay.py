"""Point-in-time historical replay for one persisted BIST signal plan.

The production ``Signal`` row is never attached to the replay session.  A
minimal, owned copy of its immutable plan is created in an in-memory database
and is advanced through :class:`BistSignalRuntimeService`.  This keeps live and
historical execution semantics identical without rewriting the production
audit trail.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, Signal, SignalEvent, SignalTarget, User, build_engine
from app.services.bist_signal_runtime_service import (
    BistSignalRuntimeService,
    CreateBistSignalRequest,
)
from app.signals import (
    CandleObservation,
    EntryOrderType,
    ExecutionPolicy,
    FillModel,
    SignalStatus,
    TradingState,
    TransactionCostModel,
)


ISTANBUL = ZoneInfo("Europe/Istanbul")
MONEY = Decimal("0.01")
TERMINAL_STATES = {
    SignalStatus.TP3_HIT.value,
    SignalStatus.STOPPED.value,
    SignalStatus.EXPIRED.value,
    SignalStatus.INVALIDATED.value,
    SignalStatus.CANCELLED.value,
    SignalStatus.CLOSED_MANUALLY.value,
    SignalStatus.UNFILLED.value,
}
_TIMEFRAME_ALIASES = {
    "5d": "5m",
    "15d": "15m",
    "1s": "1h",
    "4s": "4h",
    "1g": "1d",
    "1hf": "1wk",
    "1w": "1wk",
}
_TIMEFRAME_DURATION = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "1wk": timedelta(days=7),
}


class SignalReplayError(ValueError):
    """The saved plan or its historical input is not replayable safely."""


@dataclass(frozen=True, slots=True)
class SignalReplayPlan:
    source_signal_id: int
    source_owner_user_id: int | None
    symbol: str
    timeframe: str
    entry_order_type: EntryOrderType
    entry_price: Decimal
    entry_zone_low: Decimal | None
    entry_zone_high: Decimal | None
    stop_price: Decimal
    targets: tuple[Decimal, Decimal, Decimal]
    target_allocations: tuple[Decimal, Decimal, Decimal]
    requested_quantity: int
    quantity_assumption: str | None
    created_at: datetime
    data_timestamp: datetime
    valid_from: datetime
    expires_at: datetime | None
    provider: str
    source: str
    strategy_version: str
    score: Decimal
    confidence: str
    risk_reward: Decimal | None
    price_adjustment_mode: str

    @property
    def knowledge_at(self) -> datetime:
        """Earliest instant at which this exact plan could have been known."""

        return max(self.created_at, self.data_timestamp, self.valid_from)


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    event_type: str
    timestamp: datetime
    planned_price: Decimal | None
    execution_price: Decimal | None
    executed_quantity: Decimal | None
    to_state: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SignalReplayResult:
    plan: SignalReplayPlan
    provider_name: str
    timeframe: str
    final_status: str
    events: tuple[ReplayEvent, ...]
    completed_bars: int
    excluded_incomplete_bars: int
    unsafe_bars: int
    first_bar_at: datetime | None
    last_bar_at: datetime | None
    actual_entry_price: Decimal | None
    filled_quantity: int
    remaining_quantity: int
    realized_gross_pnl: Decimal
    total_cost: Decimal
    realized_net_pnl: Decimal
    unrealized_gross_pnl: Decimal | None
    mfe_percent: Decimal | None
    mae_percent: Decimal | None
    commission_description: str
    microstructure_columns: tuple[str, ...]
    derived_unfilled_at_end: bool


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _decimal(value: Any, field: str) -> Decimal:
    if value is None:
        raise SignalReplayError(f"Sinyalde {field} seviyesi eksik.")
    try:
        number = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - input boundary
        raise SignalReplayError(f"Sinyalde {field} seviyesi sayisal degil.") from exc
    if not number.is_finite() or number <= 0:
        raise SignalReplayError(f"Sinyalde {field} seviyesi pozitif ve sonlu olmali.")
    return number


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _optional_decimal(value: Any) -> Decimal | None:
    if _is_missing(value):
        return None
    try:
        number = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - input boundary
        raise SignalReplayError("OHLCV mikro yapi alani sayisal degil.") from exc
    if not number.is_finite():
        raise SignalReplayError("OHLCV mikro yapi alani sonlu degil.")
    return number


def _as_bool(value: Any, *, default: bool) -> bool:
    if _is_missing(value):
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "evet", "aktif", "open", "acik", "açık"}:
        return True
    if normalized in {"0", "false", "no", "hayir", "hayır", "pasif", "closed", "kapali", "kapalı"}:
        return False
    return default


def _normalize_timeframe(value: str) -> str:
    normalized = value.strip().casefold()
    return _TIMEFRAME_ALIASES.get(normalized, normalized)


def _entry_type(signal: Signal) -> EntryOrderType:
    if signal.entry_order_type:
        try:
            return EntryOrderType(str(signal.entry_order_type))
        except ValueError as exc:
            raise SignalReplayError("Kayitli giris emir tipi desteklenmiyor.") from exc
    if signal.entry_zone_low is not None and signal.entry_zone_high is not None:
        return EntryOrderType.ENTRY_ZONE
    if signal.entry_trigger is not None:
        return EntryOrderType.BREAKOUT_BUY
    if signal.planned_entry_price is not None:
        return EntryOrderType.LIMIT_BUY
    raise SignalReplayError("Sinyalde tekrar oynatilabilir giris seviyesi yok.")


def build_replay_plan(signal: Signal, target_rows: Sequence[SignalTarget] = ()) -> SignalReplayPlan:
    """Detach and validate the saved plan before leaving the production DB."""

    order_type = _entry_type(signal)
    zone_low = _optional_decimal(signal.entry_zone_low)
    zone_high = _optional_decimal(signal.entry_zone_high)
    raw_entry = signal.raw_planned_entry_price
    if raw_entry is None:
        raw_entry = signal.planned_entry_price
    if raw_entry is None:
        raw_entry = signal.entry_trigger
    if raw_entry is None and order_type == EntryOrderType.ENTRY_ZONE:
        raw_entry = zone_high
    entry = _decimal(raw_entry, "giris")
    # ``current_stop_price`` may already have moved after TP1/TP2.  A replay
    # must start from the immutable original plan, which is ``stop_price``.
    stop = _decimal(signal.stop_price, "stop")

    ordered_rows = sorted(target_rows, key=lambda row: row.target_number)
    if len(ordered_rows) == 3 and [row.target_number for row in ordered_rows] == [1, 2, 3]:
        target_values = tuple(
            _decimal(row.raw_target_price or row.target_price, f"TP{row.target_number}")
            for row in ordered_rows
        )
        allocations = tuple(Decimal(str(row.allocation_percent)) for row in ordered_rows)
    else:
        target_values = tuple(
            _decimal(value, f"TP{index}")
            for index, value in enumerate((signal.target_1, signal.target_2, signal.target_3), start=1)
        )
        allocations = (Decimal("40"), Decimal("35"), Decimal("25"))
    if len(target_values) != 3 or sum(allocations) != Decimal("100"):
        raise SignalReplayError("TP1-TP3 seviyeleri veya dagilimlari gecersiz.")

    raw_quantity = Decimal(str(signal.requested_quantity or 0))
    quantity_assumption = None
    if raw_quantity <= 0:
        requested_quantity = 1
        quantity_assumption = "Eski analiz sinyalinde lot yoktu; K/Z 1 lota normalize edildi."
    elif raw_quantity != raw_quantity.to_integral_value():
        raise SignalReplayError("Kayitli sinyal miktari tam lot degil.")
    else:
        requested_quantity = int(raw_quantity)

    data_timestamp = _utc(signal.data_timestamp)
    created_at = _utc(signal.created_at or signal.data_timestamp)
    valid_from = _utc(signal.valid_from or created_at)
    expires_at = _utc(signal.expires_at) if signal.expires_at else None
    timeframe = _normalize_timeframe(signal.timeframe)
    if timeframe not in _TIMEFRAME_DURATION:
        raise SignalReplayError(f"Sinyalin zaman dilimi tarihsel replay icin desteklenmiyor: {signal.timeframe}")
    return SignalReplayPlan(
        source_signal_id=signal.id,
        source_owner_user_id=signal.user_id,
        symbol=signal.symbol.strip().upper().removesuffix(".IS"),
        timeframe=timeframe,
        entry_order_type=order_type,
        entry_price=entry,
        entry_zone_low=zone_low,
        entry_zone_high=zone_high,
        stop_price=stop,
        targets=target_values,  # type: ignore[arg-type]
        target_allocations=allocations,  # type: ignore[arg-type]
        requested_quantity=requested_quantity,
        quantity_assumption=quantity_assumption,
        created_at=created_at,
        data_timestamp=data_timestamp,
        valid_from=valid_from,
        expires_at=expires_at,
        provider=str(signal.provider or "unknown"),
        source=str(signal.source or "legacy_analysis"),
        strategy_version=str(signal.strategy_version or "legacy")[:16],
        score=Decimal(str(signal.score or 0)),
        confidence=str(signal.confidence or "belirsiz"),
        risk_reward=Decimal(str(signal.risk_reward)) if signal.risk_reward is not None else None,
        price_adjustment_mode=str(signal.price_adjustment_mode or "belirtilmemis"),
    )


def transaction_cost_model_from_settings(settings: Any) -> tuple[TransactionCostModel, str]:
    fields = (
        getattr(settings, "backtest_commission_rate", None),
        getattr(settings, "backtest_commission_minimum", None),
        getattr(settings, "backtest_commission_tax_rate", None),
    )
    if all(value is None for value in fields):
        return TransactionCostModel(), "Komisyon ayarlanmamış; replay'de 0 kullanıldı."
    values = tuple(Decimal(str(value)) if value is not None else Decimal("0") for value in fields)
    missing = sum(value is None for value in fields)
    detail = "Ortamda tanimli komisyon/vergi modeli kullanildi."
    if missing:
        detail = f"Komisyon modelinde {missing} bos kalem 0 kabul edildi; tanimli kalemler aynen kullanildi."
    return TransactionCostModel(values[0], values[1], values[2]), detail


def execution_policy_from_settings(settings: Any) -> ExecutionPolicy:
    raw_fill = str(getattr(settings, "backtest_fill_model", FillModel.CONSERVATIVE_VOLUME_LIMITED.value))
    try:
        fill_model = FillModel(raw_fill)
    except ValueError as exc:
        raise SignalReplayError(f"BACKTEST_FILL_MODEL desteklenmiyor: {raw_fill}") from exc
    return ExecutionPolicy(
        fill_model=fill_model,
        max_volume_participation_percent=Decimal(
            str(getattr(settings, "max_daily_volume_participation_percent", 1))
        ),
        conservative_limit_lock=str(getattr(settings, "backtest_limit_lock_mode", "conservative")).lower()
        == "conservative",
        allow_delayed_data_for_live_trigger=False,
        require_valid_transaction=True,
        breakout_minimum_volume_ratio=Decimal(str(getattr(settings, "breakout_minimum_volume_ratio", 1.2))),
    )


def _bar_is_complete(row: pd.Series, timestamp: datetime, timeframe: str, as_of: datetime) -> bool:
    if "is_complete" in row.index and not pd.isna(row.get("is_complete")):
        return _as_bool(row.get("is_complete"), default=False) and timestamp <= as_of
    # Providers without a completion flag are accepted only after the entire
    # nominal interval has elapsed.  This is deliberately conservative for EOD.
    return timestamp + _TIMEFRAME_DURATION[timeframe] <= as_of


def _trading_state(value: Any) -> TradingState:
    if _is_missing(value):
        return TradingState.CONTINUOUS
    normalized = str(getattr(value, "value", value)).strip().upper()
    try:
        return TradingState(normalized)
    except ValueError as exc:
        raise SignalReplayError(f"OHLCV trading_state degeri desteklenmiyor: {normalized}") from exc


def prepare_observations(
    frame: pd.DataFrame,
    plan: SignalReplayPlan,
    *,
    provider_name: str,
    as_of: datetime,
) -> tuple[list[CandleObservation], int, int, tuple[str, ...]]:
    """Validate bars and expose only information available in chronological order."""

    if frame is None or frame.empty:
        raise SignalReplayError("Sinyal sonrasinda tarihsel mum bulunamadi.")
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise SignalReplayError(f"OHLCV alanlari eksik: {', '.join(sorted(missing))}")
    work = frame.copy(deep=True)
    try:
        work["timestamp"] = pd.to_datetime(work["timestamp"], utc=True, errors="raise")
    except Exception as exc:  # noqa: BLE001 - pandas boundary
        raise SignalReplayError("OHLCV zaman damgasi gecersiz.") from exc
    work = work.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)

    modes = {
        str(value).strip().lower()
        for value in work.get("price_mode", pd.Series(dtype=str)).dropna().unique()
    }
    if len(modes) > 1:
        raise SignalReplayError("Replay serisinde birden fazla fiyat duzeltme modu var.")
    expected_mode = plan.price_adjustment_mode.strip().lower()
    mode_alias = {"raw": "unadjusted", "split_adjusted": "adjusted"}
    normalized_modes = {mode_alias.get(value, value) for value in modes}
    normalized_expected = mode_alias.get(expected_mode, expected_mode)
    if (
        normalized_modes
        and normalized_expected not in {"", "belirtilmemis"}
        and normalized_expected not in normalized_modes
    ):
        raise SignalReplayError(
            f"Fiyat modu uyusmuyor: sinyal={plan.price_adjustment_mode}, veri={next(iter(modes))}."
        )

    cutoff = plan.knowledge_at
    as_of = _utc(as_of)
    observations: list[CandleObservation] = []
    excluded_incomplete = 0
    unsafe = 0
    microstructure = tuple(
        name
        for name in (
            "trading_state",
            "upper_limit",
            "lower_limit",
            "upper_limit_locked",
            "lower_limit_locked",
            "available_buy_quantity",
            "available_sell_quantity",
            "volume_ratio",
        )
        if name in work.columns
    )
    for _, row in work.iterrows():
        timestamp = row["timestamp"].to_pydatetime()
        if timestamp <= cutoff or timestamp > as_of:
            continue
        if not _bar_is_complete(row, timestamp, plan.timeframe, as_of):
            excluded_incomplete += 1
            continue
        raw_quality = row.get("data_quality", "VALID")
        quality = "VALID" if _is_missing(raw_quality) else str(raw_quality).strip().upper()
        valid_transaction = _as_bool(row.get("valid_transaction"), default=True)
        safe = _as_bool(row.get("safe_for_live_trigger"), default=True) and quality not in {
            "INVALID",
            "ERROR",
            "UNSAFE",
        }
        if not valid_transaction or not safe:
            unsafe += 1
        try:
            raw_provider = row.get("provider")
            observation = CandleObservation(
                symbol=plan.symbol,
                timestamp=timestamp,
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                timeframe=plan.timeframe,
                provider=provider_name if _is_missing(raw_provider) else str(raw_provider),
                is_complete=True,
                is_session_open=_as_bool(row.get("is_session_open"), default=True),
                is_delayed=False,
                safe_for_live_trigger=safe,
                valid_transaction=valid_transaction,
                trading_state=_trading_state(row.get("trading_state")),
                upper_limit=_optional_decimal(row.get("upper_limit")),
                lower_limit=_optional_decimal(row.get("lower_limit")),
                upper_limit_locked=_as_bool(row.get("upper_limit_locked"), default=False),
                lower_limit_locked=_as_bool(row.get("lower_limit_locked"), default=False),
                available_buy_quantity=_optional_decimal(row.get("available_buy_quantity")),
                available_sell_quantity=_optional_decimal(row.get("available_sell_quantity")),
                volume_ratio=_optional_decimal(row.get("volume_ratio")),
            )
        except Exception as exc:  # noqa: BLE001 - domain validation boundary
            raise SignalReplayError(f"{timestamp.isoformat()} mumunda gecersiz OHLCV/mikro yapi verisi.") from exc
        observations.append(observation)
    # Detect providers that silently clamp an old intraday request.  Replaying
    # after a large missing prefix could skip an entry or stop and is rejected.
    if observations:
        first_gap = observations[0].timestamp - cutoff
        max_gap = {
            "5m": timedelta(days=4),
            "15m": timedelta(days=4),
            "1h": timedelta(days=4),
            "4h": timedelta(days=5),
            "1d": timedelta(days=10),
            "1wk": timedelta(days=21),
        }[plan.timeframe]
        if first_gap > max_gap:
            raise SignalReplayError(
                "Saglayici sinyalin hemen sonrasindaki mumlari dondurmedi; eksik baslangicla yanli replay yapilmadi."
            )
    return observations, excluded_incomplete, unsafe, microstructure


def _pending_like(db, signal: Signal) -> bool:
    state = str(getattr(signal.state, "value", signal.state))
    if state == SignalStatus.PENDING_ENTRY.value:
        return True
    if state != SignalStatus.SUSPENDED.value:
        return False
    row = (
        db.query(SignalEvent)
        .filter(
            SignalEvent.signal_id == signal.id,
            SignalEvent.event_type.in_(["TRADING_SUSPENDED", "CIRCUIT_BREAKER_STARTED"]),
        )
        .order_by(SignalEvent.id.desc())
        .first()
    )
    try:
        metadata = json.loads(row.metadata_json or "{}") if row is not None else {}
    except (TypeError, ValueError):
        metadata = {}
    return metadata.get("resume_status") == SignalStatus.PENDING_ENTRY.value


def _event_snapshot(row: SignalEvent) -> ReplayEvent:
    try:
        metadata = json.loads(row.metadata_json or "{}")
    except (TypeError, ValueError):
        metadata = {}
    timestamp = (
        row.created_at
        if row.event_type == "SIGNAL_CREATED"
        else row.candle_open_time or row.trading_date or row.created_at
    )
    return ReplayEvent(
        event_type=str(row.event_type or "STATE_CHANGED"),
        timestamp=_utc(timestamp),
        planned_price=_optional_decimal(row.planned_price),
        execution_price=_optional_decimal(row.execution_price),
        executed_quantity=_optional_decimal(row.executed_quantity),
        to_state=str(row.to_state),
        metadata=metadata,
    )


def replay_signal_plan(
    plan: SignalReplayPlan,
    frame: pd.DataFrame,
    *,
    provider_name: str,
    as_of: datetime,
    execution_policy: ExecutionPolicy = ExecutionPolicy(),
    cost_model: TransactionCostModel = TransactionCostModel(),
    commission_description: str = "Komisyon ayarlanmamış; replay'de 0 kullanıldı.",
) -> SignalReplayResult:
    """Replay a detached plan in a disposable DB, never mutating production."""

    observations, excluded_incomplete, unsafe_bars, microstructure = prepare_observations(
        frame, plan, provider_name=provider_name, as_of=as_of
    )
    engine = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    db = factory()
    try:
        owner = User(telegram_user_id=1, total_capital=100_000)
        db.add(owner)
        db.commit()
        service = BistSignalRuntimeService(
            db,
            execution_policy=execution_policy,
            cost_model=cost_model,
        )
        replayed = service.create_pending_signal(
            CreateBistSignalRequest(
                user_id=owner.id,
                symbol=plan.symbol,
                timeframe=plan.timeframe,
                creation_price=plan.entry_price,
                entry_order_type=plan.entry_order_type,
                raw_entry_price=plan.entry_price,
                raw_entry_zone_low=plan.entry_zone_low,
                raw_entry_zone_high=plan.entry_zone_high,
                raw_stop_price=plan.stop_price,
                raw_target_prices=plan.targets,
                requested_quantity=plan.requested_quantity,
                created_at=plan.created_at,
                data_timestamp=plan.data_timestamp,
                provider=provider_name,
                source=f"historical_replay:{plan.source_signal_id}",
                strategy_version=plan.strategy_version,
                score=plan.score,
                confidence=plan.confidence,
                risk_reward=plan.risk_reward,
                target_allocations=plan.target_allocations,
                valid_from=plan.valid_from,
                expires_at=plan.expires_at,
                price_adjustment_mode=plan.price_adjustment_mode,
                idempotency_key=f"historical-replay:{plan.source_signal_id}",
            )
        )

        for observation in observations:
            db.refresh(replayed)
            state = str(getattr(replayed.state, "value", replayed.state))
            if state in TERMINAL_STATES:
                break
            if plan.expires_at and observation.timestamp >= plan.expires_at and _pending_like(db, replayed):
                service.expire_pending(
                    replayed.id,
                    owner.id,
                    as_of=plan.expires_at,
                    source="historical_replay_clock",
                )
                break
            service.process_observation(
                replayed.id,
                owner.id,
                observation,
                source="historical_replay",
            )
        db.refresh(replayed)
        state = str(getattr(replayed.state, "value", replayed.state))
        if (
            state not in TERMINAL_STATES
            and plan.expires_at
            and _utc(as_of) >= plan.expires_at
            and _pending_like(db, replayed)
        ):
            service.expire_pending(
                replayed.id,
                owner.id,
                as_of=plan.expires_at,
                source="historical_replay_clock",
            )
            db.refresh(replayed)
            state = str(getattr(replayed.state, "value", replayed.state))
        pending_at_end = _pending_like(db, replayed)

        event_rows = db.query(SignalEvent).filter_by(signal_id=replayed.id).order_by(SignalEvent.id).all()
        events = tuple(_event_snapshot(row) for row in event_rows)
        target_rows = (
            db.query(SignalTarget)
            .filter_by(signal_id=replayed.id)
            .order_by(SignalTarget.target_number)
            .all()
        )
        entry_price = _optional_decimal(replayed.average_fill_price or replayed.actual_entry_price)
        filled_quantity = int(replayed.filled_quantity or 0)
        remaining_quantity = int(replayed.remaining_quantity or 0)

        target_gross = sum((_optional_decimal(row.gross_pnl) or Decimal("0")) for row in target_rows)
        target_cost = sum((_optional_decimal(row.costs) or Decimal("0")) for row in target_rows)
        stop_gross = Decimal("0")
        stop_cost = Decimal("0")
        stop_quantity = 0
        if entry_price is not None and filled_quantity:
            entry_cost_total = cost_model.calculate(entry_price * filled_quantity).total
            for row in event_rows:
                if row.event_type not in {"STOP_EXECUTED", "STOP_EXECUTION_DELAYED"}:
                    continue
                quantity = int(row.executed_quantity or 0)
                price = _optional_decimal(row.execution_price)
                if quantity <= 0 or price is None:
                    continue
                stop_quantity += quantity
                stop_gross += (price - entry_price) * quantity
                stop_cost += cost_model.calculate(price * quantity).total
            if stop_quantity:
                stop_cost += entry_cost_total * Decimal(stop_quantity) / Decimal(filled_quantity)
        realized_gross = _money(target_gross + stop_gross)
        total_cost = _money(target_cost + stop_cost)
        realized_net = _money(realized_gross - total_cost)

        unrealized = None
        mfe = mae = None
        if entry_price is not None:
            entry_events = [
                event
                for event in events
                if event.event_type in {"ENTRY_FILLED", "ENTRY_PARTIALLY_FILLED"}
            ]
            entry_at = entry_events[0].timestamp if entry_events else None
            closed_at = (
                _utc(replayed.closed_at)
                if replayed.closed_at
                else observations[-1].timestamp if observations else plan.knowledge_at
            )
            excursion_bars = [
                bar for bar in observations
                if entry_at is not None and entry_at <= bar.timestamp <= closed_at
            ]
            if excursion_bars:
                highest = max(bar.high for bar in excursion_bars)
                lowest = min(bar.low for bar in excursion_bars)
                mfe = _money((highest - entry_price) / entry_price * Decimal("100"))
                mae = _money((lowest - entry_price) / entry_price * Decimal("100"))
            if remaining_quantity and observations:
                unrealized = _money((observations[-1].close - entry_price) * remaining_quantity)

        return SignalReplayResult(
            plan=plan,
            provider_name=provider_name,
            timeframe=plan.timeframe,
            final_status=state,
            events=events,
            completed_bars=len(observations),
            excluded_incomplete_bars=excluded_incomplete,
            unsafe_bars=unsafe_bars,
            first_bar_at=observations[0].timestamp if observations else None,
            last_bar_at=observations[-1].timestamp if observations else None,
            actual_entry_price=entry_price,
            filled_quantity=filled_quantity,
            remaining_quantity=remaining_quantity,
            realized_gross_pnl=realized_gross,
            total_cost=total_cost,
            realized_net_pnl=realized_net,
            unrealized_gross_pnl=unrealized,
            mfe_percent=mfe,
            mae_percent=mae,
            commission_description=commission_description,
            microstructure_columns=microstructure,
            derived_unfilled_at_end=pending_at_end,
        )
    finally:
        db.close()
        engine.dispose()


_EVENT_LABELS = {
    "SIGNAL_CREATED": "PENDING_ENTRY / plan olusturuldu",
    "ENTRY_FILLED": "Giris gerceklesti",
    "ENTRY_PARTIALLY_FILLED": "Giris kismen gerceklesti",
    "ORDER_REMAINED_UNFILLED": "Giris tavan/likidite nedeniyle dolmadi",
    "SIGNAL_EXPIRED": "Sinyalin suresi doldu",
    "TP1_REACHED": "TP1 gerceklesti",
    "TP2_REACHED": "TP2 gerceklesti",
    "TP3_REACHED": "TP3 gerceklesti",
    "TARGET_PARTIALLY_FILLED": "Hedef kismen gerceklesti",
    "STOP_EXECUTED": "Stop gerceklesti",
    "STOP_EXECUTION_DELAYED": "Stop taban/likidite nedeniyle gecikti",
    "STOP_MOVED": "Stop seviyesi tasindi",
    "TRADING_SUSPENDED": "Islem sirasi kapali / takip askida",
    "CIRCUIT_BREAKER_STARTED": "Devre kesici / takip askida",
    "TRADING_RESUMED": "Islemler yeniden basladi",
    "CIRCUIT_BREAKER_ENDED": "Devre kesici sona erdi",
}


def _format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "-"
    return _utc(value).astimezone(ISTANBUL).strftime("%d.%m.%Y %H:%M")


def _format_price(value: Decimal | None) -> str:
    return "-" if value is None else f"{value:.2f} TL"


def format_signal_replay_report(result: SignalReplayResult) -> str:
    plan = result.plan
    lines = [
        f"🧪 SİNYAL #{plan.source_signal_id} TARİHSEL REPLAY",
        f"{plan.symbol} | {result.timeframe} | {result.provider_name}",
        "",
        f"Plan: giriş {_format_price(plan.entry_price)} | stop {_format_price(plan.stop_price)}",
        "Hedefler: " + " | ".join(f"TP{i} {_format_price(price)}" for i, price in enumerate(plan.targets, 1)),
        f"Bilgi anı: {_format_timestamp(plan.knowledge_at)}",
        f"Veri: {_format_timestamp(result.first_bar_at)} → "
        f"{_format_timestamp(result.last_bar_at)} | {result.completed_bars} tamamlanmış mum",
        "",
        "Olaylar:",
    ]
    visible_events: list[ReplayEvent | None] = list(result.events)
    if len(visible_events) > 18:
        visible_events = visible_events[:9] + [None] + visible_events[-9:]
    for event in visible_events:
        if event is None:
            lines.append(f"• … {len(result.events) - 18} ara olay kısaltıldı")
            continue
        label = _EVENT_LABELS.get(event.event_type, event.event_type)
        price = event.execution_price or event.planned_price
        suffix = f" @ {_format_price(price)}" if price is not None else ""
        if event.executed_quantity is not None and event.executed_quantity > 0:
            suffix += f" | {event.executed_quantity:g} lot"
        lines.append(f"• {_format_timestamp(event.timestamp)} — {label}{suffix}")
    if result.derived_unfilled_at_end:
        lines.append(f"• {_format_timestamp(result.last_bar_at)} — Dönem sonuna kadar giriş dolmadı")

    lines.extend(
        [
            "",
            f"Son durum: {result.final_status}",
            f"Brüt gerçekleşen K/Z: {result.realized_gross_pnl:.2f} TL",
            f"Gerçekleşen K/Z maliyeti: {result.total_cost:.2f} TL",
            f"Net gerçekleşen K/Z: {result.realized_net_pnl:.2f} TL",
        ]
    )
    if result.unrealized_gross_pnl is not None:
        lines.append(
            f"Açık {result.remaining_quantity} lotun son kapanışa göre gerçekleşmemiş brüt K/Z'si: "
            f"{result.unrealized_gross_pnl:.2f} TL"
        )
    if result.mfe_percent is not None and result.mae_percent is not None:
        lines.append(f"MFE: %{result.mfe_percent:.2f} | MAE: %{result.mae_percent:.2f}")
    lines.append(result.commission_description)
    if plan.quantity_assumption:
        lines.append(plan.quantity_assumption)
    if result.excluded_incomplete_bars:
        lines.append(f"Tamamlanmamış {result.excluded_incomplete_bars} mum dışlandı.")
    if result.unsafe_bars:
        lines.append(f"Güvensiz/geçersiz {result.unsafe_bars} mum tetik üretmeden atlandı.")
    micro = ", ".join(result.microstructure_columns) if result.microstructure_columns else "sağlayıcıda yok"
    lines.extend(
        [
            "",
            f"Mikro yapı alanları: {micro}.",
            f"Fiyat modu: {plan.price_adjustment_mode}.",
            "Not: Yalnız o anda tamamlanmış mumlar işlendi. OHLC içi sıra bilinmediğinde stop önce uygulanır; "
            "tavan/taban, likidite ve seans durumu yalnız veri sağlayıcı sunduysa hesaba katılır.",
            "Geçmiş performans gelecek sonucu garanti etmez; bu bir emir veya yatırım tavsiyesi değildir.",
        ]
    )
    return "\n".join(lines)


def replay_from_provider(
    plan: SignalReplayPlan,
    provider: Any,
    settings: Any,
    *,
    as_of: datetime | None = None,
) -> SignalReplayResult:
    now = _utc(as_of or datetime.now(timezone.utc))
    if plan.knowledge_at >= now:
        raise SignalReplayError("Sinyalin bilgi anindan sonra henuz replay araligi olusmadi.")
    provider_name = str(getattr(provider, "name", type(provider).__name__))
    if provider_name.strip().lower() == "mock":
        raise SignalReplayError("Gercek sinyal replay'i mock veriyle calistirilmaz.")
    frame = provider.get_ohlcv(
        plan.symbol,
        plan.timeframe,
        plan.knowledge_at - timedelta(seconds=1),
        now,
    )
    metadata = getattr(provider, "last_fetch_metadata", {})
    fetch_meta = metadata.get((plan.symbol.upper(), plan.timeframe), {}) if isinstance(metadata, dict) else {}
    if isinstance(fetch_meta, dict) and fetch_meta.get("provider"):
        provider_name = str(fetch_meta["provider"])
        if fetch_meta.get("fallback_used"):
            provider_name += " (fallback)"
        elif fetch_meta.get("cache_used"):
            provider_name += " (cache)"
    elif "provider" in frame.columns:
        providers = {str(value).strip() for value in frame["provider"].dropna().unique() if str(value).strip()}
        if len(providers) == 1:
            provider_name = next(iter(providers))
    cost_model, cost_description = transaction_cost_model_from_settings(settings)
    return replay_signal_plan(
        plan,
        frame,
        provider_name=provider_name,
        as_of=now,
        execution_policy=execution_policy_from_settings(settings),
        cost_model=cost_model,
        commission_description=cost_description,
    )
