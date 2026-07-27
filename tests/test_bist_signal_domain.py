from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.signals import (
    BreakoutConfirmationMode,
    CandleObservation,
    EntryOrderType,
    EntryPlan,
    ExecutionError,
    ExecutionPolicy,
    ExitOrderType,
    FillModel,
    FillStatus,
    MarketRuleError,
    PositionLedger,
    PositionSizingRequest,
    PricePurpose,
    SignalEventType,
    SignalLifecycle,
    SignalStatus,
    TradingState,
    TransactionCostModel,
    allocate_target_lots,
    build_event_dedup_key,
    calculate_position_size,
    evaluate_entry,
    evaluate_long_exit,
)
from app.signals.market_rules import DEFAULT_BIST_MARKET_RULES


BASE_TIME = datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc)


def _candle(
    offset: int,
    *,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: str = "1000000",
    **kwargs,
) -> CandleObservation:
    return CandleObservation(
        symbol="TEST",
        timestamp=BASE_TIME + timedelta(days=offset),
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        provider="test",
        **kwargs,
    )


def _event_key(event: SignalEventType, offset: int, target: int | None = None) -> str:
    return build_event_dedup_key("TEST-1", event, f"test:TEST:1d:{offset}", target_number=target)


def test_official_bist_tick_bands_and_directional_rounding():
    rules = DEFAULT_BIST_MARKET_RULES
    assert rules.tick_size_for("19.999") == Decimal("0.010")
    assert rules.tick_size_for("20.000") == Decimal("0.020")
    assert rules.tick_size_for("50") == Decimal("0.050")
    assert rules.tick_size_for("100") == Decimal("0.100")
    assert rules.tick_size_for("250") == Decimal("0.250")
    assert rules.tick_size_for("500") == Decimal("0.500")
    assert rules.tick_size_for("1000") == Decimal("1.000")
    assert rules.tick_size_for("2500") == Decimal("2.500")

    buy = rules.round_price("45.517", PricePurpose.BUY_LIMIT)
    stop = rules.round_price("44.607", PricePurpose.PROTECTIVE_STOP_LONG)
    target = rules.round_price("47.019", PricePurpose.TARGET_LONG)
    breakout = rules.round_price("45.501", PricePurpose.BREAKOUT_TRIGGER)
    assert buy.rounded_order_price == Decimal("45.50")
    assert stop.rounded_order_price == Decimal("44.62")
    assert target.rounded_order_price == Decimal("47.00")
    assert breakout.rounded_order_price == Decimal("45.52")
    assert all(
        rules.is_valid_price(price)
        for price in (
            buy.rounded_order_price,
            stop.rounded_order_price,
            target.rounded_order_price,
            breakout.rounded_order_price,
        )
    )
    with pytest.raises(MarketRuleError):
        rules.validate_long_stop_move("44.60", "44.40")


def test_daily_limits_stay_inside_configured_band_and_on_valid_ticks():
    with pytest.raises(MarketRuleError, match="enstruman/pazar"):
        DEFAULT_BIST_MARKET_RULES.daily_price_limits("45.50")

    limits = DEFAULT_BIST_MARKET_RULES.daily_price_limits("45.50", limit_percent="10")
    assert limits.lower_limit == Decimal("40.96")
    assert limits.upper_limit == Decimal("50.05")
    assert limits.lower_limit >= limits.raw_lower_limit
    assert limits.upper_limit <= limits.raw_upper_limit
    assert DEFAULT_BIST_MARKET_RULES.is_valid_price(limits.lower_limit)
    assert DEFAULT_BIST_MARKET_RULES.is_valid_price(limits.upper_limit)


def test_lifecycle_rejects_invalid_transition_and_keeps_state_intact(caplog):
    lifecycle = SignalLifecycle("TEST-1")
    outcome = lifecycle.transition(
        SignalStatus.TP2_HIT,
        SignalEventType.TP2_REACHED,
        event_time=BASE_TIME,
        dedup_key=_event_key(SignalEventType.TP2_REACHED, 1, 2),
    )
    assert not outcome.applied
    assert lifecycle.status == SignalStatus.PENDING_ENTRY
    assert lifecycle.events == []
    assert len(lifecycle.errors) == 1
    assert "Gecersiz sinyal gecisi" in caplog.text


def test_lifecycle_event_is_idempotent_after_restart():
    lifecycle = SignalLifecycle("TEST-1")
    key = _event_key(SignalEventType.ENTRY_FILLED, 2)
    first = lifecycle.transition(
        SignalStatus.ACTIVE,
        SignalEventType.ENTRY_FILLED,
        event_time=BASE_TIME,
        dedup_key=key,
        execution_price="45.50",
        executed_quantity=1000,
    )
    assert first.applied
    restored = SignalLifecycle.restore("TEST-1", lifecycle.status, lifecycle.events)
    duplicate = restored.transition(
        SignalStatus.ACTIVE,
        SignalEventType.ENTRY_FILLED,
        event_time=BASE_TIME,
        dedup_key=key,
        execution_price="45.50",
        executed_quantity=1000,
    )
    assert duplicate.duplicate and not duplicate.applied
    assert len(restored.events) == 1


def test_limit_buy_waits_for_touch_but_next_open_can_improve_price():
    plan = EntryPlan(EntryOrderType.LIMIT_BUY, 1000, BASE_TIME, planned_entry_price="45.50")
    below = _candle(1, open_="44.80", high="45.40", low="44.70", close="45.10")
    pending = evaluate_entry(plan, below)
    assert pending.status == FillStatus.PENDING

    opening = _candle(
        2,
        open_="45.20",
        high="45.40",
        low="45.10",
        close="45.30",
        is_session_open=True,
    )
    improved = evaluate_entry(plan, opening)
    assert improved.status == FillStatus.FILLED
    assert improved.actual_execution_price == Decimal("45.20")
    assert improved.actual_execution_price <= Decimal("45.50")


def test_entry_zone_next_open_and_breakout_rules_are_distinct():
    zone = EntryPlan(
        EntryOrderType.ENTRY_ZONE,
        100,
        BASE_TIME,
        planned_entry_price="45.50",
        entry_zone_low="45.30",
        entry_zone_high="45.60",
    )
    zone_fill = evaluate_entry(zone, _candle(1, open_="45.80", high="45.90", low="45.50", close="45.70"))
    assert zone_fill.actual_execution_price == Decimal("45.60")

    next_open = EntryPlan(EntryOrderType.NEXT_OPEN, 100, BASE_TIME)
    same_time = _candle(0, open_="45.00", high="45.20", low="44.80", close="45.10")
    assert evaluate_entry(next_open, same_time).status == FillStatus.PENDING
    next_bar = _candle(1, open_="46.00", high="46.20", low="45.80", close="46.10")
    assert evaluate_entry(next_open, next_bar).actual_execution_price == Decimal("46.00")

    breakout = EntryPlan(
        EntryOrderType.BREAKOUT_BUY,
        100,
        BASE_TIME,
        breakout_level="45.50",
        breakout_confirmation=BreakoutConfirmationMode.COMPLETED_CLOSE,
    )
    incomplete = _candle(
        1,
        open_="45.40",
        high="45.90",
        low="45.30",
        close="45.70",
        is_complete=False,
    )
    assert evaluate_entry(breakout, incomplete).status == FillStatus.PENDING
    complete = _candle(2, open_="45.40", high="45.90", low="45.30", close="45.70")
    assert evaluate_entry(breakout, complete).status == FillStatus.PENDING
    after_confirmation = _candle(3, open_="45.84", high="46.10", low="45.70", close="46.00")
    confirmed_fill = evaluate_entry(
        breakout,
        after_confirmation,
        previous_observation=complete,
    )
    assert confirmed_fill.actual_execution_price == Decimal("45.84")
    assert confirmed_fill.fill_method == "breakout_next_open_after_completed_close"

    gap_touch = EntryPlan(
        EntryOrderType.BREAKOUT_BUY,
        100,
        BASE_TIME,
        breakout_level="45.50",
        breakout_confirmation=BreakoutConfirmationMode.PRICE_TOUCH,
    )
    gap = _candle(4, open_="46.00", high="46.40", low="45.90", close="46.20")
    assert evaluate_entry(gap_touch, gap).actual_execution_price == Decimal("46.00")


def test_tavan_taban_suspension_and_gap_stop_rules():
    plan = EntryPlan(EntryOrderType.LIMIT_BUY, 1000, BASE_TIME, planned_entry_price="45.50")
    tavan = _candle(
        1,
        open_="45.50",
        high="45.50",
        low="45.50",
        close="45.50",
        upper_limit="45.50",
        upper_limit_locked=True,
        available_sell_quantity="0",
    )
    assert evaluate_entry(plan, tavan).status == FillStatus.UNFILLED_LIMIT_LOCK

    suspended = _candle(
        2,
        open_="45.50",
        high="45.50",
        low="45.50",
        close="45.50",
        trading_state=TradingState.SUSPENDED,
        valid_transaction=False,
    )
    assert evaluate_entry(plan, suspended).status == FillStatus.SUSPENDED

    taban = _candle(
        3,
        open_="40.00",
        high="40.00",
        low="40.00",
        close="40.00",
        lower_limit="40.00",
        lower_limit_locked=True,
        available_buy_quantity="0",
    )
    pending_exit = evaluate_long_exit(ExitOrderType.STOP, "44.60", 1000, taban)
    assert pending_exit.status == FillStatus.EXIT_PENDING_LIMIT_LOCK

    gap = _candle(4, open_="44.00", high="44.40", low="43.80", close="44.20")
    gap_exit = evaluate_long_exit(ExitOrderType.STOP, "44.60", 1000, gap)
    assert gap_exit.status == FillStatus.FILLED_AT_FIRST_TRADABLE_PRICE
    assert gap_exit.actual_execution_price == Decimal("44.00")


def test_conservative_volume_model_creates_partial_fill():
    plan = EntryPlan(EntryOrderType.LIMIT_BUY, 1000, BASE_TIME, planned_entry_price="45.50")
    candle = _candle(1, open_="45.60", high="45.70", low="45.40", close="45.55", volume="50000")
    policy = ExecutionPolicy(
        fill_model=FillModel.CONSERVATIVE_VOLUME_LIMITED,
        max_volume_participation_percent=Decimal("1"),
    )
    result = evaluate_entry(plan, candle, policy=policy)
    assert result.status == FillStatus.PARTIALLY_FILLED
    assert result.filled_quantity == 500
    assert result.remaining_quantity == 500


def test_exact_4550_three_target_acceptance_and_exactly_once_events():
    plan = EntryPlan(EntryOrderType.LIMIT_BUY, 1000, BASE_TIME, planned_entry_price="45.50")
    lifecycle = SignalLifecycle("TEST-1")

    below = _candle(1, open_="44.80", high="45.40", low="44.70", close="45.10")
    assert evaluate_entry(plan, below).status == FillStatus.PENDING
    assert lifecycle.status == SignalStatus.PENDING_ENTRY

    entry_candle = _candle(2, open_="45.60", high="45.70", low="45.40", close="45.56")
    entry = evaluate_entry(plan, entry_candle)
    assert entry.actual_execution_price == Decimal("45.50")
    entry_key = _event_key(SignalEventType.ENTRY_FILLED, 2)
    assert lifecycle.transition(
        SignalStatus.ACTIVE,
        SignalEventType.ENTRY_FILLED,
        event_time=entry.timestamp,
        dedup_key=entry_key,
        planned_price="45.50",
        execution_price=entry.actual_execution_price,
        requested_quantity=1000,
        executed_quantity=1000,
    ).applied

    costs = TransactionCostModel(
        commission_rate=Decimal("0.001"),
        commission_tax_rate=Decimal("0.05"),
    )
    ledger = PositionLedger(
        entry_price="45.50",
        original_quantity=1000,
        target_prices=("47.00", "48.20", "49.40"),
        allocations=("40", "35", "25"),
        cost_model=costs,
    )
    assert ledger.target_quantities == (400, 350, 250)

    target_specs = (
        (1, "47.00", SignalStatus.TP1_HIT, SignalEventType.TP1_REACHED, 3),
        (2, "48.20", SignalStatus.TP2_HIT, SignalEventType.TP2_REACHED, 4),
        (3, "49.40", SignalStatus.TP3_HIT, SignalEventType.TP3_REACHED, 5),
    )
    for target_number, price, status, event_type, offset in target_specs:
        candle = _candle(
            offset,
            open_=str(Decimal(price) - Decimal("0.20")),
            high=price,
            low=str(Decimal(price) - Decimal("0.30")),
            close=str(Decimal(price) - Decimal("0.10")),
        )
        exit_fill = evaluate_long_exit(
            ExitOrderType.TARGET,
            price,
            ledger.target_quantities[target_number - 1],
            candle,
        )
        assert exit_fill.has_fill
        key = _event_key(event_type, offset, target_number)
        target_outcome = ledger.execute_target(
            target_number,
            execution_price=exit_fill.actual_execution_price,
            dedup_key=key,
        )
        assert target_outcome.applied
        assert lifecycle.transition(
            status,
            event_type,
            event_time=candle.timestamp,
            dedup_key=key,
            planned_price=price,
            execution_price=exit_fill.actual_execution_price,
            requested_quantity=target_outcome.execution.quantity,
            executed_quantity=target_outcome.execution.quantity,
        ).applied

    assert [item.quantity for item in ledger.executions] == [400, 350, 250]
    assert ledger.remaining_quantity == 0
    assert lifecycle.status == SignalStatus.TP3_HIT
    assert ledger.gross_realized_pnl == Decimal("2520.00")
    assert ledger.net_realized_pnl == Decimal("2421.80")
    assert ledger.weighted_average_exit_price == Decimal("48.02")
    assert len(lifecycle.events) == 4  # entry + each target separately

    # Rehydration simulates an application restart. Reprocessing TP3 changes
    # neither money nor notification/event counts.
    restored_lifecycle = SignalLifecycle.restore("TEST-1", lifecycle.status, lifecycle.events)
    duplicate_event = restored_lifecycle.transition(
        SignalStatus.TP3_HIT,
        SignalEventType.TP3_REACHED,
        event_time=BASE_TIME + timedelta(days=5),
        dedup_key=_event_key(SignalEventType.TP3_REACHED, 5, 3),
    )
    assert duplicate_event.duplicate
    restored_ledger = PositionLedger(
        entry_price="45.50",
        original_quantity=1000,
        target_prices=("47.00", "48.20", "49.40"),
        cost_model=costs,
        executions=list(ledger.executions),
    )
    duplicate_target = restored_ledger.execute_target(
        3,
        execution_price="49.40",
        dedup_key=_event_key(SignalEventType.TP3_REACHED, 5, 3),
    )
    assert duplicate_target.duplicate
    assert restored_ledger.remaining_quantity == 0
    assert restored_ledger.gross_realized_pnl == Decimal("2520.00")


def test_decimal_position_sizing_obeys_risk_cash_position_and_liquidity_caps():
    result = calculate_position_size(
        PositionSizingRequest(
            portfolio_balance="100000",
            risk_percent="1",
            entry_price="45.50",
            stop_price="44.60",
            available_cash="100000",
            maximum_position_percent="100",
            daily_volume="200000",
            maximum_volume_participation_percent="1",
            estimated_gap_stop_price="44.00",
            target_prices=("47.00", "48.20", "49.40"),
        )
    )
    assert result.planned_risk_budget == Decimal("1000.00")
    assert result.risk_limited_lots == 1111
    assert result.liquidity_limited_lots == 2000
    assert result.suggested_lots == 1111
    assert result.loss_at_planned_stop == Decimal("999.90")
    assert result.estimated_gap_loss == Decimal("1666.50")
    assert sum(result.target_lots) == 1111
    assert result.weighted_target_profit == result.tp1_profit + result.tp2_profit + result.tp3_profit


def test_target_allocation_requires_exactly_one_hundred_percent():
    assert allocate_target_lots(1000, (40, 35, 25)) == (400, 350, 250)
    with pytest.raises(ExecutionError):
        allocate_target_lots(1000, (40, 35, 20))
