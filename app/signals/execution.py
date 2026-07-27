"""Pure BIST entry/exit execution, partial-fill and Decimal sizing rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from typing import Iterable

from app.signals.enums import (
    BreakoutConfirmationMode,
    EntryOrderType,
    ExitOrderType,
    FillModel,
    FillStatus,
    PricePurpose,
    TradingState,
)
from app.signals.market_rules import (
    DEFAULT_BIST_MARKET_RULES,
    BistMarketRules,
    DecimalLike,
    MarketRuleError,
    as_decimal,
)


MONEY_QUANTUM = Decimal("0.01")


class ExecutionError(ValueError):
    pass


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class CandleObservation:
    symbol: str
    timestamp: datetime
    open: DecimalLike
    high: DecimalLike
    low: DecimalLike
    close: DecimalLike
    volume: DecimalLike
    timeframe: str = "1d"
    provider: str = "unknown"
    is_complete: bool = True
    is_session_open: bool = False
    is_delayed: bool = False
    safe_for_live_trigger: bool = True
    valid_transaction: bool = True
    trading_state: TradingState = TradingState.CONTINUOUS
    upper_limit: DecimalLike | None = None
    lower_limit: DecimalLike | None = None
    upper_limit_locked: bool = False
    lower_limit_locked: bool = False
    available_buy_quantity: DecimalLike | None = None
    available_sell_quantity: DecimalLike | None = None
    volume_ratio: DecimalLike | None = None

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper().removesuffix(".IS")
        if not symbol:
            raise ExecutionError("Sembol bos olamaz.")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "timestamp", _utc(self.timestamp))
        for name in ("open", "high", "low", "close", "volume"):
            object.__setattr__(self, name, as_decimal(getattr(self, name), field_name=name))
        for name in (
            "upper_limit",
            "lower_limit",
            "available_buy_quantity",
            "available_sell_quantity",
            "volume_ratio",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, as_decimal(value, field_name=name))
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ExecutionError("OHLC fiyatlari sifirdan buyuk olmalidir.")
        if self.high < self.low:
            raise ExecutionError("Mum high degeri low degerinden kucuk olamaz.")
        if not self.low <= self.open <= self.high or not self.low <= self.close <= self.high:
            raise ExecutionError("Open/close mumun high-low araligi disinda olamaz.")
        if self.volume < 0:
            raise ExecutionError("Hacim negatif olamaz.")
        if self.available_buy_quantity is not None and self.available_buy_quantity < 0:
            raise ExecutionError("Alis likiditesi negatif olamaz.")
        if self.available_sell_quantity is not None and self.available_sell_quantity < 0:
            raise ExecutionError("Satis likiditesi negatif olamaz.")


@dataclass(frozen=True, slots=True)
class EntryPlan:
    order_type: EntryOrderType
    requested_quantity: int
    created_at: datetime
    planned_entry_price: DecimalLike | None = None
    entry_zone_low: DecimalLike | None = None
    entry_zone_high: DecimalLike | None = None
    breakout_level: DecimalLike | None = None
    breakout_confirmation: BreakoutConfirmationMode = BreakoutConfirmationMode.COMPLETED_CLOSE
    manual_entry_price: DecimalLike | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", _utc(self.created_at))
        if self.requested_quantity <= 0:
            raise ExecutionError("Istenen lot sifirdan buyuk olmalidir.")
        for name in ("planned_entry_price", "entry_zone_low", "entry_zone_high", "breakout_level", "manual_entry_price"):
            value = getattr(self, name)
            if value is not None:
                number = as_decimal(value, field_name=name)
                if number <= 0:
                    raise ExecutionError(f"{name} sifirdan buyuk olmalidir.")
                object.__setattr__(self, name, number)

        if self.order_type == EntryOrderType.LIMIT_BUY and self.planned_entry_price is None:
            raise ExecutionError("LIMIT_BUY icin planned_entry_price gereklidir.")
        if self.order_type == EntryOrderType.BREAKOUT_BUY and self.breakout_level is None:
            raise ExecutionError("BREAKOUT_BUY icin breakout_level gereklidir.")
        if self.order_type == EntryOrderType.ENTRY_ZONE:
            if self.entry_zone_low is None or self.entry_zone_high is None:
                raise ExecutionError("ENTRY_ZONE icin alt ve ust sinir gereklidir.")
            if self.entry_zone_low > self.entry_zone_high:
                raise ExecutionError("Giris bolgesi alt siniri ust sinirdan buyuk olamaz.")
        if self.order_type == EntryOrderType.MANUAL_ENTRY and self.manual_entry_price is None:
            raise ExecutionError("MANUAL_ENTRY icin manual_entry_price gereklidir.")


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    fill_model: FillModel = FillModel.CONSERVATIVE_VOLUME_LIMITED
    max_volume_participation_percent: Decimal = Decimal("1")
    conservative_limit_lock: bool = True
    allow_delayed_data_for_live_trigger: bool = False
    require_valid_transaction: bool = True
    breakout_minimum_volume_ratio: Decimal = Decimal("1.2")
    lot_size: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "max_volume_participation_percent",
            as_decimal(self.max_volume_participation_percent, field_name="max_volume_participation_percent"),
        )
        object.__setattr__(
            self,
            "breakout_minimum_volume_ratio",
            as_decimal(self.breakout_minimum_volume_ratio, field_name="breakout_minimum_volume_ratio"),
        )
        if not Decimal("0") < self.max_volume_participation_percent <= Decimal("100"):
            raise ExecutionError("Hacim katilim yuzdesi 0-100 arasinda olmalidir.")
        if self.breakout_minimum_volume_ratio <= 0:
            raise ExecutionError("Breakout hacim orani sifirdan buyuk olmalidir.")
        if self.lot_size < 1:
            raise ExecutionError("Lot buyuklugu en az 1 olmalidir.")


@dataclass(frozen=True, slots=True)
class FillResult:
    status: FillStatus
    requested_quantity: int
    filled_quantity: int
    remaining_quantity: int
    planned_execution_price: Decimal | None
    actual_execution_price: Decimal | None
    trigger_price: Decimal | None
    timestamp: datetime
    reason: str
    fill_method: str
    fill_source: str
    execution_confidence: str
    candle_open: Decimal
    candle_high: Decimal
    candle_low: Decimal
    candle_close: Decimal
    candle_volume: Decimal

    @property
    def has_fill(self) -> bool:
        return self.filled_quantity > 0 and self.actual_execution_price is not None


_BLOCKED_STATES = {
    TradingState.SUSPENDED,
    TradingState.CIRCUIT_BREAKER,
    TradingState.ORDER_COLLECTION,
    TradingState.CLOSED,
    TradingState.NO_VALID_TRADE,
}


def _result(
    observation: CandleObservation,
    requested: int,
    status: FillStatus,
    *,
    planned: Decimal | None = None,
    actual: Decimal | None = None,
    trigger: Decimal | None = None,
    filled: int = 0,
    reason: str,
    method: str,
    confidence: str = "DUSUK",
) -> FillResult:
    return FillResult(
        status=status,
        requested_quantity=requested,
        filled_quantity=filled,
        remaining_quantity=requested - filled,
        planned_execution_price=planned,
        actual_execution_price=actual,
        trigger_price=trigger,
        timestamp=observation.timestamp,
        reason=reason,
        fill_method=method,
        fill_source=f"{observation.provider}:{observation.timeframe}",
        execution_confidence=confidence,
        candle_open=observation.open,
        candle_high=observation.high,
        candle_low=observation.low,
        candle_close=observation.close,
        candle_volume=observation.volume,
    )


def _market_block(
    observation: CandleObservation,
    requested: int,
    policy: ExecutionPolicy,
    planned: Decimal | None,
) -> FillResult | None:
    if observation.is_delayed and not policy.allow_delayed_data_for_live_trigger:
        return _result(
            observation,
            requested,
            FillStatus.UNSAFE_DATA,
            planned=planned,
            reason="Gecikmeli veriyle canli tetiklemeye izin verilmiyor.",
            method="data_freshness_gate",
        )
    if not observation.safe_for_live_trigger:
        return _result(
            observation,
            requested,
            FillStatus.UNSAFE_DATA,
            planned=planned,
            reason="Gozlem canli tetikleme icin guvenli degil.",
            method="data_quality_gate",
        )
    if observation.trading_state in _BLOCKED_STATES:
        return _result(
            observation,
            requested,
            FillStatus.SUSPENDED,
            planned=planned,
            reason=f"Islem durumu gerceklesmeye uygun degil: {observation.trading_state.value}.",
            method="market_state_gate",
        )
    if policy.require_valid_transaction and not observation.valid_transaction:
        return _result(
            observation,
            requested,
            FillStatus.UNFILLED,
            planned=planned,
            reason="Gecerli piyasa islemi bulunmuyor.",
            method="valid_trade_gate",
        )
    return None


def _round_trade_price(price: Decimal, rules: BistMarketRules) -> Decimal:
    if rules.is_valid_price(price):
        return price
    return rules.round_price(price, PricePurpose.REFERENCE_PRICE).rounded_order_price


def _available_lots(
    requested: int,
    observation: CandleObservation,
    policy: ExecutionPolicy,
    *,
    buy_order: bool,
) -> int:
    lot = policy.lot_size
    capacity = requested
    if policy.fill_model != FillModel.FULL_FILL:
        raw_capacity = observation.volume * policy.max_volume_participation_percent / Decimal("100")
        capacity = min(capacity, int(raw_capacity // lot) * lot)
    explicit = observation.available_sell_quantity if buy_order else observation.available_buy_quantity
    if explicit is not None:
        capacity = min(capacity, int(explicit // lot) * lot)
    return max(0, capacity - (capacity % lot))


def _filled_result(
    observation: CandleObservation,
    requested: int,
    planned: Decimal,
    actual: Decimal,
    trigger: Decimal,
    method: str,
    policy: ExecutionPolicy,
    *,
    buy_order: bool,
    first_tradable_price: bool = False,
) -> FillResult:
    filled = _available_lots(requested, observation, policy, buy_order=buy_order)
    if filled <= 0:
        return _result(
            observation,
            requested,
            FillStatus.UNFILLED,
            planned=planned,
            trigger=trigger,
            reason="Hacim/likidite modeli gerceklesebilir lot bulamadi.",
            method=method,
        )
    if first_tradable_price and filled == requested:
        status = FillStatus.FILLED_AT_FIRST_TRADABLE_PRICE
    else:
        status = FillStatus.FILLED if filled == requested else FillStatus.PARTIALLY_FILLED
    reason = "Emir tamamen gerceklesti." if filled == requested else "Emir hacim/likidite nedeniyle kismen gerceklesti."
    return _result(
        observation,
        requested,
        status,
        planned=planned,
        actual=actual,
        trigger=trigger,
        filled=filled,
        reason=reason,
        method=method,
        confidence="YUKSEK" if observation.is_complete or observation.timeframe.endswith("m") else "ORTA",
    )


def evaluate_entry(
    plan: EntryPlan,
    observation: CandleObservation,
    *,
    previous_observation: CandleObservation | None = None,
    policy: ExecutionPolicy = ExecutionPolicy(),
    market_rules: BistMarketRules = DEFAULT_BIST_MARKET_RULES,
) -> FillResult:
    """Evaluate one observation without mutating a signal or persistence state."""

    if plan.requested_quantity % policy.lot_size:
        raise ExecutionError("Istenen miktar lot buyuklugunun kati olmalidir.")

    planned: Decimal | None
    if plan.order_type == EntryOrderType.LIMIT_BUY:
        planned = market_rules.round_price(plan.planned_entry_price, PricePurpose.BUY_LIMIT).rounded_order_price
    elif plan.order_type == EntryOrderType.BREAKOUT_BUY:
        planned = market_rules.round_price(plan.breakout_level, PricePurpose.BREAKOUT_TRIGGER).rounded_order_price
    elif plan.order_type == EntryOrderType.ENTRY_ZONE:
        planned = market_rules.round_price(
            plan.planned_entry_price if plan.planned_entry_price is not None else plan.entry_zone_high,
            PricePurpose.BUY_LIMIT,
        ).rounded_order_price
    elif plan.order_type == EntryOrderType.MANUAL_ENTRY:
        planned = market_rules.round_price(plan.manual_entry_price, PricePurpose.REFERENCE_PRICE).rounded_order_price
    else:
        planned = None

    blocked = _market_block(observation, plan.requested_quantity, policy, planned)
    if blocked is not None:
        return blocked
    if observation.upper_limit_locked and policy.conservative_limit_lock:
        sell_liquidity = observation.available_sell_quantity
        if sell_liquidity is None or sell_liquidity <= 0:
            return _result(
                observation,
                plan.requested_quantity,
                FillStatus.UNFILLED_LIMIT_LOCK,
                planned=planned,
                reason="Tavan kilidinde gercekci satis likiditesi yok; alim gerceklesmedi.",
                method="upper_limit_lock",
            )

    actual: Decimal | None = None
    trigger: Decimal | None = planned
    method = ""

    if plan.order_type == EntryOrderType.LIMIT_BUY:
        assert planned is not None
        if observation.is_session_open and observation.open <= planned:
            actual = observation.open  # price improvement; never worse than the limit
            method = "limit_open_price_improvement"
        elif observation.low <= planned <= observation.high:
            actual = planned
            method = "limit_range_touch"

    elif plan.order_type == EntryOrderType.ENTRY_ZONE:
        low = market_rules.round_price(plan.entry_zone_low, PricePurpose.ENTRY_ZONE_LOW).rounded_order_price
        high = market_rules.round_price(plan.entry_zone_high, PricePurpose.ENTRY_ZONE_HIGH).rounded_order_price
        if low > high:
            raise ExecutionError("Tick yuvarlamasindan sonra giris bolgesi gecersiz oldu.")
        trigger = high
        if low <= observation.open <= high:
            actual = observation.open
            method = "entry_zone_open_inside"
        elif observation.open < low and observation.high >= low:
            actual = low
            trigger = low
            method = "entry_zone_cross_from_below"
        elif observation.open > high and observation.low <= high:
            actual = high
            method = "entry_zone_cross_from_above"

    elif plan.order_type == EntryOrderType.NEXT_OPEN:
        if observation.timestamp > plan.created_at:
            actual = observation.open
            trigger = observation.open
            planned = observation.open
            method = "next_valid_session_open"

    elif plan.order_type == EntryOrderType.MANUAL_ENTRY:
        assert planned is not None
        if observation.low <= planned <= observation.high:
            actual = planned
            method = "manual_entry_observed"

    elif plan.order_type == EntryOrderType.BREAKOUT_BUY:
        assert planned is not None
        mode = plan.breakout_confirmation
        previous = previous_observation
        if mode == BreakoutConfirmationMode.PRICE_TOUCH and observation.high >= planned:
            actual = max(observation.open, planned)
            method = "breakout_price_touch"
        elif mode == BreakoutConfirmationMode.INTRADAY_CROSS:
            previous_price = previous.close if previous is not None else observation.open
            if previous_price <= planned and observation.high >= planned:
                actual = max(observation.open, planned)
                method = "breakout_intraday_cross"
        elif mode == BreakoutConfirmationMode.COMPLETED_CLOSE:
            # The close is only known after the confirming candle has ended.
            # Filling at that already-finished close would be look-ahead.  The
            # first executable price is therefore the next observation's open.
            if (
                previous is not None
                and previous.is_complete
                and previous.close > planned
                and observation.timestamp > previous.timestamp
            ):
                actual = observation.open
                method = "breakout_next_open_after_completed_close"
        elif mode == BreakoutConfirmationMode.COMPLETED_CLOSE_VOLUME:
            ratio = previous.volume_ratio if previous is not None else None
            if (
                previous is not None
                and previous.is_complete
                and previous.close > planned
                and (ratio or Decimal("0")) >= policy.breakout_minimum_volume_ratio
                and observation.timestamp > previous.timestamp
            ):
                actual = observation.open
                method = "breakout_next_open_after_completed_close_volume"
        elif mode == BreakoutConfirmationMode.BREAKOUT_RETEST:
            if (
                previous is not None
                and previous.is_complete
                and previous.close > planned
                and observation.low <= planned <= observation.high
                and observation.close >= planned
            ):
                actual = planned
                method = "breakout_retest"
        elif mode == BreakoutConfirmationMode.NEXT_CANDLE_OPEN:
            if previous is not None and previous.is_complete and previous.close > planned and observation.timestamp > previous.timestamp:
                actual = observation.open
                method = "breakout_next_candle_open"

    if actual is None:
        return _result(
            observation,
            plan.requested_quantity,
            FillStatus.PENDING,
            planned=planned,
            trigger=trigger,
            reason="Giris kosulu bu gozlemde gerceklesmedi.",
            method="condition_not_reached",
        )
    actual = _round_trade_price(actual, market_rules)
    return _filled_result(
        observation,
        plan.requested_quantity,
        planned or actual,
        actual,
        trigger or actual,
        method,
        policy,
        buy_order=True,
    )


def evaluate_long_exit(
    order_type: ExitOrderType,
    planned_price: DecimalLike,
    quantity: int,
    observation: CandleObservation,
    *,
    policy: ExecutionPolicy = ExecutionPolicy(),
    market_rules: BistMarketRules = DEFAULT_BIST_MARKET_RULES,
) -> FillResult:
    if quantity <= 0 or quantity % policy.lot_size:
        raise ExecutionError("Cikis miktari pozitif ve lot buyuklugunun kati olmalidir.")
    purpose = PricePurpose.PROTECTIVE_STOP_LONG if order_type == ExitOrderType.STOP else PricePurpose.TARGET_LONG
    planned = market_rules.round_price(planned_price, purpose).rounded_order_price
    blocked = _market_block(observation, quantity, policy, planned)
    if blocked is not None:
        return blocked

    if order_type == ExitOrderType.STOP:
        if observation.lower_limit_locked and policy.conservative_limit_lock:
            buy_liquidity = observation.available_buy_quantity
            if buy_liquidity is None or buy_liquidity <= 0:
                return _result(
                    observation,
                    quantity,
                    FillStatus.EXIT_PENDING_LIMIT_LOCK,
                    planned=planned,
                    trigger=planned,
                    reason="Taban kilidinde alis likiditesi yok; stop cikisi beklemede.",
                    method="lower_limit_lock",
                )
        if observation.open < planned:
            actual = _round_trade_price(observation.open, market_rules)
            return _filled_result(
                observation,
                quantity,
                planned,
                actual,
                planned,
                "stop_gap_first_tradable_price",
                policy,
                buy_order=False,
                first_tradable_price=True,
            )
        if observation.low <= planned <= observation.high:
            return _filled_result(
                observation,
                quantity,
                planned,
                planned,
                planned,
                "stop_range_touch",
                policy,
                buy_order=False,
            )
    elif order_type == ExitOrderType.TARGET:
        if observation.open > planned:
            actual = _round_trade_price(observation.open, market_rules)
            return _filled_result(
                observation,
                quantity,
                planned,
                actual,
                planned,
                "target_open_price_improvement",
                policy,
                buy_order=False,
            )
        if observation.high >= planned:
            return _filled_result(
                observation,
                quantity,
                planned,
                planned,
                planned,
                "target_range_touch",
                policy,
                buy_order=False,
            )
    else:
        actual = _round_trade_price(observation.open, market_rules)
        return _filled_result(
            observation,
            quantity,
            planned,
            actual,
            observation.open,
            "manual_first_tradable_price",
            policy,
            buy_order=False,
            first_tradable_price=actual != planned,
        )

    return _result(
        observation,
        quantity,
        FillStatus.PENDING,
        planned=planned,
        trigger=planned,
        reason="Cikis seviyesi bu gozlemde gorulmedi.",
        method="condition_not_reached",
    )


def allocate_target_lots(
    total_quantity: int,
    allocation_percentages: Iterable[DecimalLike] = (Decimal("40"), Decimal("35"), Decimal("25")),
    *,
    lot_size: int = 1,
) -> tuple[int, ...]:
    percentages = tuple(as_decimal(value, field_name="allocation") for value in allocation_percentages)
    if not percentages or any(value < 0 for value in percentages):
        raise ExecutionError("Hedef dagilimlari negatif olamaz.")
    if sum(percentages) != Decimal("100"):
        raise ExecutionError("Hedef dagilimlarinin toplami tam olarak %100 olmalidir.")
    if total_quantity <= 0 or lot_size < 1 or total_quantity % lot_size:
        raise ExecutionError("Toplam miktar pozitif ve lot buyuklugunun kati olmalidir.")
    total_units = total_quantity // lot_size
    raw_units = [Decimal(total_units) * value / Decimal("100") for value in percentages]
    units = [int(value.to_integral_value(rounding=ROUND_FLOOR)) for value in raw_units]
    remainder = total_units - sum(units)
    order = sorted(range(len(units)), key=lambda index: (raw_units[index] - units[index], -index), reverse=True)
    for index in order[:remainder]:
        units[index] += 1
    return tuple(value * lot_size for value in units)


@dataclass(frozen=True, slots=True)
class TransactionCosts:
    commission: Decimal
    commission_tax: Decimal
    total: Decimal


@dataclass(frozen=True, slots=True)
class TransactionCostModel:
    commission_rate: Decimal = Decimal("0")
    minimum_commission: Decimal = Decimal("0")
    commission_tax_rate: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for name in ("commission_rate", "minimum_commission", "commission_tax_rate"):
            value = as_decimal(getattr(self, name), field_name=name)
            if value < 0:
                raise ExecutionError(f"{name} negatif olamaz.")
            object.__setattr__(self, name, value)

    def calculate(self, notional: DecimalLike) -> TransactionCosts:
        amount = as_decimal(notional, field_name="notional")
        if amount < 0:
            raise ExecutionError("Islem tutari negatif olamaz.")
        commission = _money(max(amount * self.commission_rate, self.minimum_commission if amount else Decimal("0")))
        tax = _money(commission * self.commission_tax_rate)
        return TransactionCosts(commission, tax, commission + tax)


@dataclass(frozen=True, slots=True)
class TargetExecution:
    target_number: int
    target_price: Decimal
    quantity: int
    gross_pnl: Decimal
    allocated_entry_costs: Decimal
    exit_costs: TransactionCosts
    net_pnl: Decimal
    dedup_key: str


@dataclass(frozen=True, slots=True)
class TargetExecutionOutcome:
    execution: TargetExecution
    applied: bool
    duplicate: bool


@dataclass(slots=True)
class PositionLedger:
    entry_price: DecimalLike
    original_quantity: int
    target_prices: tuple[DecimalLike, DecimalLike, DecimalLike]
    allocations: tuple[DecimalLike, DecimalLike, DecimalLike] = (
        Decimal("40"),
        Decimal("35"),
        Decimal("25"),
    )
    cost_model: TransactionCostModel = TransactionCostModel()
    remaining_quantity: int = field(init=False)
    target_quantities: tuple[int, int, int] = field(init=False)
    executions: list[TargetExecution] = field(default_factory=list)
    _by_key: dict[str, TargetExecution] = field(init=False, default_factory=dict, repr=False)
    _by_target: dict[int, TargetExecution] = field(init=False, default_factory=dict, repr=False)
    entry_costs: TransactionCosts = field(init=False)

    def __post_init__(self) -> None:
        self.entry_price = as_decimal(self.entry_price, field_name="entry_price")
        if self.entry_price <= 0 or self.original_quantity <= 0:
            raise ExecutionError("Giris fiyati ve miktar pozitif olmalidir.")
        self.target_prices = tuple(as_decimal(value, field_name="target_price") for value in self.target_prices)  # type: ignore[assignment]
        if len(self.target_prices) != 3 or any(value <= self.entry_price for value in self.target_prices):
            raise ExecutionError("Uc hedef de long giris fiyatinin ustunde olmalidir.")
        if tuple(sorted(self.target_prices)) != self.target_prices:
            raise ExecutionError("Hedefler artan fiyat sirasinda olmalidir.")
        self.allocations = tuple(as_decimal(value, field_name="allocation") for value in self.allocations)  # type: ignore[assignment]
        if len(self.allocations) != 3:
            raise ExecutionError("TP1, TP2 ve TP3 icin tam uc dagilim yuzdesi gereklidir.")
        self.target_quantities = allocate_target_lots(self.original_quantity, self.allocations)  # type: ignore[assignment]
        self.remaining_quantity = self.original_quantity
        self.entry_costs = self.cost_model.calculate(self.entry_price * self.original_quantity)
        restored = list(self.executions)
        self.executions = []
        for execution in restored:
            self._restore_execution(execution)

    def _restore_execution(self, execution: TargetExecution) -> None:
        if execution.target_number not in {1, 2, 3}:
            raise ExecutionError("Geri yuklenen hedef numarasi gecersiz.")
        if execution.dedup_key in self._by_key or execution.target_number in self._by_target:
            raise ExecutionError("Geri yuklenen hedeflerde yinelenen olay var.")
        if execution.target_number != len(self._by_target) + 1:
            raise ExecutionError("Geri yuklenen hedefler TP1, TP2, TP3 sirasinda olmalidir.")
        expected = self.target_quantities[execution.target_number - 1]
        if execution.quantity != expected or execution.quantity > self.remaining_quantity:
            raise ExecutionError("Geri yuklenen hedef miktari planla uyusmuyor.")
        self.executions.append(execution)
        self._by_key[execution.dedup_key] = execution
        self._by_target[execution.target_number] = execution
        self.remaining_quantity -= execution.quantity

    def execute_target(
        self,
        target_number: int,
        *,
        execution_price: DecimalLike | None = None,
        dedup_key: str,
    ) -> TargetExecutionOutcome:
        if target_number not in {1, 2, 3}:
            raise ExecutionError("Hedef numarasi 1, 2 veya 3 olmalidir.")
        dedup_key = dedup_key.strip()
        if not dedup_key:
            raise ExecutionError("Hedef dedup anahtari bos olamaz.")
        existing = self._by_key.get(dedup_key)
        if existing is not None:
            if existing.target_number != target_number:
                raise ExecutionError("Ayni dedup anahtari farkli hedefte kullanilamaz.")
            return TargetExecutionOutcome(existing, applied=False, duplicate=True)
        existing_target = self._by_target.get(target_number)
        if existing_target is not None:
            return TargetExecutionOutcome(existing_target, applied=False, duplicate=True)
        next_target = len(self._by_target) + 1
        if target_number != next_target:
            raise ExecutionError(f"Hedefler sirayla gerceklesmelidir; beklenen TP{next_target}.")

        planned = self.target_prices[target_number - 1]
        price = as_decimal(execution_price, field_name="execution_price") if execution_price is not None else planned
        if price <= 0:
            raise ExecutionError("Hedef gerceklesme fiyati pozitif olmalidir.")
        quantity = self.target_quantities[target_number - 1]
        if target_number == 3:
            quantity = self.remaining_quantity
        gross = _money((price - self.entry_price) * quantity)
        if target_number == 3:
            # Allocate any cent-rounding remainder to the final target so the
            # sum of per-target entry costs exactly equals the entry invoice.
            entry_cost = self.entry_costs.total - sum(
                (item.allocated_entry_costs for item in self.executions), Decimal("0")
            )
        else:
            entry_cost = _money(self.entry_costs.total * Decimal(quantity) / Decimal(self.original_quantity))
        exit_costs = self.cost_model.calculate(price * quantity)
        net = _money(gross - entry_cost - exit_costs.total)
        execution = TargetExecution(
            target_number,
            price,
            quantity,
            gross,
            entry_cost,
            exit_costs,
            net,
            dedup_key,
        )
        self.executions.append(execution)
        self._by_key[dedup_key] = execution
        self._by_target[target_number] = execution
        self.remaining_quantity -= quantity
        return TargetExecutionOutcome(execution, applied=True, duplicate=False)

    @property
    def gross_realized_pnl(self) -> Decimal:
        return _money(sum((item.gross_pnl for item in self.executions), Decimal("0")))

    @property
    def net_realized_pnl(self) -> Decimal:
        return _money(sum((item.net_pnl for item in self.executions), Decimal("0")))

    @property
    def weighted_average_exit_price(self) -> Decimal | None:
        quantity = sum(item.quantity for item in self.executions)
        if quantity == 0:
            return None
        value = sum((item.target_price * item.quantity for item in self.executions), Decimal("0")) / quantity
        return _money(value)


@dataclass(frozen=True, slots=True)
class PositionSizingRequest:
    portfolio_balance: DecimalLike
    risk_percent: DecimalLike
    entry_price: DecimalLike
    stop_price: DecimalLike
    available_cash: DecimalLike
    maximum_position_percent: DecimalLike = Decimal("20")
    daily_volume: DecimalLike | None = None
    maximum_volume_participation_percent: DecimalLike = Decimal("1")
    commission_reserve_rate: DecimalLike = Decimal("0")
    estimated_gap_stop_price: DecimalLike | None = None
    target_prices: tuple[DecimalLike, DecimalLike, DecimalLike] | None = None
    target_allocations: tuple[DecimalLike, DecimalLike, DecimalLike] = (
        Decimal("40"),
        Decimal("35"),
        Decimal("25"),
    )
    lot_size: int = 1


@dataclass(frozen=True, slots=True)
class PositionSizeResult:
    suggested_lots: int
    required_cash: Decimal
    planned_risk_budget: Decimal
    loss_at_planned_stop: Decimal
    estimated_gap_loss: Decimal | None
    portfolio_percent: Decimal
    risk_limited_lots: int
    cash_limited_lots: int
    position_limited_lots: int
    liquidity_limited_lots: int | None
    target_lots: tuple[int, int, int]
    tp1_profit: Decimal | None
    tp2_profit: Decimal | None
    tp3_profit: Decimal | None
    weighted_target_profit: Decimal | None


def _whole_lots(value: Decimal, lot_size: int) -> int:
    return int(value // lot_size) * lot_size


def calculate_position_size(request: PositionSizingRequest) -> PositionSizeResult:
    balance = as_decimal(request.portfolio_balance, field_name="portfolio_balance")
    risk_percent = as_decimal(request.risk_percent, field_name="risk_percent")
    entry = as_decimal(request.entry_price, field_name="entry_price")
    stop = as_decimal(request.stop_price, field_name="stop_price")
    cash = as_decimal(request.available_cash, field_name="available_cash")
    max_position = as_decimal(request.maximum_position_percent, field_name="maximum_position_percent")
    participation = as_decimal(
        request.maximum_volume_participation_percent,
        field_name="maximum_volume_participation_percent",
    )
    reserve_rate = as_decimal(request.commission_reserve_rate, field_name="commission_reserve_rate")
    if min(balance, entry, cash) <= 0:
        raise ExecutionError("Bakiye, giris ve kullanilabilir nakit pozitif olmalidir.")
    if stop <= 0 or stop >= entry:
        raise ExecutionError("Long pozisyonda stop sifirdan buyuk ve girisin altinda olmalidir.")
    if not Decimal("0") < risk_percent <= Decimal("100"):
        raise ExecutionError("Risk yuzdesi 0-100 arasinda olmalidir.")
    if not Decimal("0") < max_position <= Decimal("100"):
        raise ExecutionError("Maksimum pozisyon yuzdesi 0-100 arasinda olmalidir.")
    if not Decimal("0") < participation <= Decimal("100"):
        raise ExecutionError("Hacim katilim yuzdesi 0-100 arasinda olmalidir.")
    if reserve_rate < 0:
        raise ExecutionError("Komisyon rezervi negatif olamaz.")
    if request.lot_size < 1:
        raise ExecutionError("Lot buyuklugu en az 1 olmalidir.")

    risk_budget = balance * risk_percent / Decimal("100")
    risk_per_share = entry - stop
    risk_lots = _whole_lots(risk_budget / risk_per_share, request.lot_size)
    cash_per_share = entry * (Decimal("1") + reserve_rate)
    cash_lots = _whole_lots(cash / cash_per_share, request.lot_size)
    position_lots = _whole_lots(
        (balance * max_position / Decimal("100")) / entry,
        request.lot_size,
    )
    liquidity_lots: int | None = None
    if request.daily_volume is not None:
        daily_volume = as_decimal(request.daily_volume, field_name="daily_volume")
        if daily_volume < 0:
            raise ExecutionError("Gunluk hacim negatif olamaz.")
        liquidity_lots = _whole_lots(
            daily_volume * participation / Decimal("100"),
            request.lot_size,
        )
    caps = [risk_lots, cash_lots, position_lots]
    if liquidity_lots is not None:
        caps.append(liquidity_lots)
    lots = max(0, min(caps))
    required_cash = _money(Decimal(lots) * cash_per_share)
    stop_loss = _money(Decimal(lots) * risk_per_share)
    portfolio_percent = (
        (Decimal(lots) * entry / balance * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if lots
        else Decimal("0.00")
    )
    gap_loss = None
    if request.estimated_gap_stop_price is not None:
        gap = as_decimal(request.estimated_gap_stop_price, field_name="estimated_gap_stop_price")
        if gap <= 0:
            raise ExecutionError("Tahmini gap stop fiyati pozitif olmalidir.")
        gap_loss = _money(Decimal(lots) * max(Decimal("0"), entry - gap))

    if len(request.target_allocations) != 3:
        raise ExecutionError("TP1, TP2 ve TP3 icin tam uc dagilim yuzdesi gereklidir.")
    allocation_lots = allocate_target_lots(lots, request.target_allocations, lot_size=request.lot_size) if lots else (0, 0, 0)
    profits: tuple[Decimal | None, Decimal | None, Decimal | None] = (None, None, None)
    weighted = None
    if request.target_prices is not None:
        targets = tuple(as_decimal(value, field_name="target_price") for value in request.target_prices)
        if len(targets) != 3 or any(value <= entry for value in targets):
            raise ExecutionError("Uc hedef de giris fiyatinin ustunde olmalidir.")
        profits = tuple(_money((target - entry) * quantity) for target, quantity in zip(targets, allocation_lots))  # type: ignore[assignment]
        weighted = _money(sum(profits, Decimal("0")))  # type: ignore[arg-type]

    return PositionSizeResult(
        suggested_lots=lots,
        required_cash=required_cash,
        planned_risk_budget=_money(risk_budget),
        loss_at_planned_stop=stop_loss,
        estimated_gap_loss=gap_loss,
        portfolio_percent=portfolio_percent,
        risk_limited_lots=risk_lots,
        cash_limited_lots=cash_lots,
        position_limited_lots=position_lots,
        liquidity_limited_lots=liquidity_lots,
        target_lots=allocation_lots,  # type: ignore[arg-type]
        tp1_profit=profits[0],
        tp2_profit=profits[1],
        tp3_profit=profits[2],
        weighted_target_profit=weighted,
    )
