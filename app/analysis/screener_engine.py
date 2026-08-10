from __future__ import annotations

"""Batch-friendly full-universe confluence, scenario and VWAP scanners."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Literal

import pandas as pd
from sqlalchemy.orm import Session

from app.analysis.indicator_engine import (
    atr,
    compute_indicator_bundle,
    evaluate_indicator_confluence,
    evaluate_ten_indicator_confluence,
    pivot_support_resistance,
)
from app.analysis.pattern_engine import ChartPattern, detect_chart_patterns
from app.data.base_provider import BaseMarketDataProvider
from app.models.database import EmaCrossState, RsiAlertState

logger = logging.getLogger("mergen_quant.screener")


@dataclass(frozen=True)
class SymbolTechnicalState:
    symbol: str
    price: float
    ema20: float
    ema50: float
    ema100: float
    relation: Literal["above", "below", "equal"]
    crossover: Literal["golden", "death"] | None
    rsi: float
    rsi_state: Literal["overbought", "oversold", "normal"]
    adx: float
    relative_volume: float
    supertrend_direction: Literal["up", "down"]
    bullish_confluence: int
    bearish_confluence: int
    bullish_qualified: bool
    bearish_qualified: bool
    bullish_ten_confluence: int
    bearish_ten_confluence: int
    bullish_ten_qualified: bool
    bearish_ten_qualified: bool
    vwap: float
    macd_histogram: float
    obv_rising: bool
    poc: float | None
    vah: float | None
    val: float | None
    atr: float
    support: float | None
    resistance: float | None
    timestamp: datetime | pd.Timestamp
    patterns: tuple[ChartPattern, ...]
    bullish_ten_confirmation_labels: tuple[str, ...] = ()
    bearish_ten_confirmation_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScreenerAlert:
    kind: Literal["golden_cross", "death_cross", "rsi_overbought", "rsi_oversold"]
    symbol: str
    price: float
    rsi: float
    adx: float
    relative_volume: float
    supertrend_direction: str
    confluence_count: int


@dataclass(frozen=True)
class ScreenerRunResult:
    scanned: int
    failed: int
    alerts: tuple[ScreenerAlert, ...]
    states: tuple[SymbolTechnicalState, ...]


@dataclass(frozen=True)
class IntradayScanReport:
    scanned: int
    failed: int
    strong_above_vwap: tuple[str, ...]
    weak_below_vwap: tuple[str, ...]
    poc_reactions: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True)
class TradeScenario:
    """A compact, confirmation-first 15-minute setup.

    ``entry_low`` / ``entry_high`` is always a retest zone derived from VWAP
    and EMA20.  It is deliberately not copied from the current last price.
    """

    symbol: str
    action: Literal["AL", "SAT", "BEKLE"]
    direction: Literal["bullish", "bearish"]
    score: int
    confirmation_count: int
    core_confirmation_count: int
    core_checks: tuple[tuple[str, bool], ...]
    ten_confirmation_count: int
    ten_confirmation_labels: tuple[str, ...]
    price: float
    entry_low: float
    entry_high: float
    stop: float
    tp1: float
    tp2: float
    rr: float
    atr_percent: float
    reasons: tuple[str, ...]
    confirmation_instruction: str
    confirmed_patterns: tuple[ChartPattern, ...] = ()


@dataclass(frozen=True)
class TradeScenarioRunResult:
    scanned: int
    failed: int
    scenarios: tuple[TradeScenario, ...]
    created_at: datetime
    interval_minutes: int = 15


@dataclass(frozen=True)
class DailyTopPick:
    """A daily-long candidate with a real resistance-based potential target.

    ``target_potential_percent`` is an opportunity filter, never a return
    promise.  The card explicitly tells the user that no purchase is due until
    price revisits the calculated zone and gives a closing confirmation.
    """

    symbol: str
    score: int
    technical_confirmations: int
    price: float
    entry_low: float
    entry_high: float
    stop: float
    tp1: float
    tp2: float
    rr: float
    target_potential_percent: float
    pattern: ChartPattern
    reasons: tuple[str, ...]
    confirmation_instruction: str
    fundamental_score: int | None
    fundamental_status: str
    fundamental_source: str | None


@dataclass(frozen=True)
class DailyTopPicksRunResult:
    scanned: int
    failed: int
    picks: tuple[DailyTopPick, ...]
    fundamental_checked: int
    fundamental_verified: int
    created_at: datetime


@dataclass(frozen=True)
class MarketOpportunityReport:
    scanned: int
    failed: int
    timeframe: Literal["5m", "1h", "4h"]
    minimum_confluence: int
    al_sat_uygun: tuple[SymbolTechnicalState, ...]
    kisa_vade: tuple[SymbolTechnicalState, ...]
    uzun_vade_teknik: tuple[SymbolTechnicalState, ...]
    spekulatif_uyari: tuple[SymbolTechnicalState, ...]
    created_at: datetime


def analyze_symbol_frame(
    symbol: str,
    frame: pd.DataFrame,
    *,
    rsi_overbought: float = 75.0,
    rsi_oversold: float = 25.0,
    minimum_confluence: int = 3,
    ten_indicator_minimum: int = 5,
    timeframe: str = "1d",
) -> SymbolTechnicalState:
    bundle = compute_indicator_bundle(frame, symbol=symbol, timeframe=timeframe)
    data = bundle.frame
    last, previous = data.iloc[-1], data.iloc[-2]
    current_diff = float(last["ema50"] - last["ema100"])
    previous_diff = float(previous["ema50"] - previous["ema100"])
    relation: Literal["above", "below", "equal"] = "above" if current_diff > 0 else "below" if current_diff < 0 else "equal"
    crossover: Literal["golden", "death"] | None = None
    if previous_diff <= 0 < current_diff:
        crossover = "golden"
    elif previous_diff >= 0 > current_diff:
        crossover = "death"
    rsi_value = float(last["rsi14"])
    rsi_state: Literal["overbought", "oversold", "normal"]
    if rsi_value >= rsi_overbought:
        rsi_state = "overbought"
    elif rsi_value <= rsi_oversold:
        rsi_state = "oversold"
    else:
        rsi_state = "normal"
    bullish = evaluate_indicator_confluence(bundle, "bullish", minimum_required=minimum_confluence)
    bearish = evaluate_indicator_confluence(bundle, "bearish", minimum_required=minimum_confluence)
    bullish_ten = evaluate_ten_indicator_confluence(
        bundle, "bullish", minimum_required=ten_indicator_minimum
    )
    bearish_ten = evaluate_ten_indicator_confluence(
        bundle, "bearish", minimum_required=ten_indicator_minimum
    )
    atr_values = atr(data, 14)
    support, resistance = pivot_support_resistance(data, lookback=20)
    patterns = detect_chart_patterns(frame)
    return SymbolTechnicalState(
        symbol=symbol.upper().removesuffix(".IS"),
        price=float(last["close"]),
        ema20=float(last["ema20"]),
        ema50=float(last["ema50"]),
        ema100=float(last["ema100"]),
        relation=relation,
        crossover=crossover,
        rsi=rsi_value,
        rsi_state=rsi_state,
        adx=float(last["adx14"]),
        relative_volume=float(last["relative_volume"]) if pd.notna(last["relative_volume"]) else 0.0,
        supertrend_direction="up" if int(last["supertrend_direction"]) > 0 else "down",
        bullish_confluence=len(bullish.confirmations),
        bearish_confluence=len(bearish.confirmations),
        bullish_qualified=bullish.qualified,
        bearish_qualified=bearish.qualified,
        bullish_ten_confluence=len(bullish_ten.confirmations),
        bearish_ten_confluence=len(bearish_ten.confirmations),
        bullish_ten_qualified=bullish_ten.qualified,
        bearish_ten_qualified=bearish_ten.qualified,
        vwap=float(last["vwap"]),
        macd_histogram=float(last["macd_histogram"]),
        obv_rising=bool(last["obv"] > previous["obv"]),
        poc=bundle.volume_profile.poc,
        vah=bundle.volume_profile.vah,
        val=bundle.volume_profile.val,
        atr=float(atr_values.iloc[-1]),
        support=support,
        resistance=resistance,
        timestamp=last["timestamp"],
        patterns=patterns,
        bullish_ten_confirmation_labels=tuple(bullish_ten.confirmations),
        bearish_ten_confirmation_labels=tuple(bearish_ten.confirmations),
    )


def _four_hour_bars_from_hourly(frame: pd.DataFrame) -> pd.DataFrame:
    """Aggregate completed consecutive one-hour candles into honest 4H bars.

    Yahoo's free endpoint does not publish a native ``4h`` interval.  We use
    source 1H OHLCV bars and only keep groups with four completed candles,
    rather than pretending an unfinished session is a full four-hour candle.
    This preserves real OHLCV values and also works with licensed providers.
    """

    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if frame.empty or not required.issubset(frame.columns):
        return frame.copy()
    data = frame.loc[:, ["timestamp", "open", "high", "low", "close", "volume"]].copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True, errors="coerce")
    data = data.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
    data = data.sort_values("timestamp").drop_duplicates(subset="timestamp", keep="last").reset_index(drop=True)
    if data.empty:
        return data
    session = data["timestamp"].dt.tz_convert("Europe/Istanbul").dt.date
    data["_session"] = session
    data["_four_hour_bucket"] = data.groupby("_session").cumcount() // 4
    group_keys = ["_session", "_four_hour_bucket"]
    complete = data.groupby(group_keys, sort=False).size()
    complete_keys = complete[complete >= 4].index
    if len(complete_keys) == 0:
        return data.iloc[0:0].loc[:, ["timestamp", "open", "high", "low", "close", "volume"]]
    grouped = data.groupby(group_keys, sort=False).agg(
        timestamp=("timestamp", "last"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    result = grouped.loc[complete_keys].reset_index(drop=True)
    return result.sort_values("timestamp").reset_index(drop=True)


def _scan_start_for_timeframe(timeframe: str, end: datetime) -> datetime:
    """Request enough true source candles for EMA200 without exceeding 5m limits."""

    days = {"5m": 58, "1h": 120, "4h": 430, "1d": 540, "15m": 58}.get(timeframe, 58)
    return end - timedelta(days=days)


def _fetch_states(
    symbols: Iterable[str],
    *,
    provider_factory: Callable[[], BaseMarketDataProvider],
    workers: int,
    timeframe: str,
    settings,
) -> tuple[list[SymbolTechnicalState], int]:
    end = datetime.now(timezone.utc)
    start = _scan_start_for_timeframe(timeframe, end)
    local = threading.local()

    def fetch(symbol: str) -> SymbolTechnicalState:
        provider = getattr(local, "provider", None)
        if provider is None:
            provider = provider_factory()
            local.provider = provider
        source_timeframe = "1h" if timeframe == "4h" else timeframe
        frame = provider.get_ohlcv(symbol, source_timeframe, start, end)
        if timeframe == "4h":
            frame = _four_hour_bars_from_hourly(frame)
        return analyze_symbol_frame(
            symbol,
            frame,
            rsi_overbought=settings.rsi_overbought,
            rsi_oversold=settings.rsi_oversold,
            minimum_confluence=settings.technical_screener_min_confluence,
            ten_indicator_minimum=int(getattr(settings, "market_opportunity_minimum_confluence", 5)),
            timeframe=timeframe,
        )

    clean = list(dict.fromkeys(str(symbol).upper().removesuffix(".IS") for symbol in symbols))
    states: list[SymbolTechnicalState] = []
    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        future_map = {executor.submit(fetch, symbol): symbol for symbol in clean}
        for future in as_completed(future_map):
            symbol = future_map[future]
            try:
                states.append(future.result())
            except Exception as exc:  # noqa: BLE001 - one symbol cannot stop universe scan
                failed += 1
                logger.info("Teknik tarama sembolu atlandi symbol=%s error=%s", symbol, type(exc).__name__)
    states.sort(key=lambda item: item.symbol)
    return states, failed


def _persist_cross_state(db: Session, state: SymbolTechnicalState, timeframe: str) -> ScreenerAlert | None:
    row = (
        db.query(EmaCrossState)
        .filter(EmaCrossState.symbol == state.symbol, EmaCrossState.timeframe == timeframe)
        .first()
    )
    if row is None:
        row = EmaCrossState(
            symbol=state.symbol,
            timeframe=timeframe,
            last_relation=state.relation,
            last_price=state.price,
        )
        db.add(row)
    alert = None
    direction_qualified = state.bullish_qualified if state.crossover == "golden" else state.bearish_qualified
    confluence = state.bullish_confluence if state.crossover == "golden" else state.bearish_confluence
    cross_name = state.crossover
    if cross_name and direction_qualified and row.last_cross != cross_name:
        alert = ScreenerAlert(
            kind="golden_cross" if cross_name == "golden" else "death_cross",
            symbol=state.symbol,
            price=state.price,
            rsi=state.rsi,
            adx=state.adx,
            relative_volume=state.relative_volume,
            supertrend_direction=state.supertrend_direction,
            confluence_count=confluence,
        )
        row.last_cross = cross_name
        row.last_alerted_at = datetime.now(timezone.utc)
    row.last_relation = state.relation
    row.last_price = state.price
    return alert


def _persist_rsi_state(db: Session, state: SymbolTechnicalState, timeframe: str) -> ScreenerAlert | None:
    row = (
        db.query(RsiAlertState)
        .filter(RsiAlertState.symbol == state.symbol, RsiAlertState.timeframe == timeframe)
        .first()
    )
    if row is None:
        row = RsiAlertState(symbol=state.symbol, timeframe=timeframe, state="normal")
        db.add(row)
    alert = None
    if state.rsi_state == "normal":
        row.state = "normal"
    elif row.state != state.rsi_state:
        qualified = state.bullish_qualified if state.rsi_state == "overbought" else state.bearish_qualified
        confluence = state.bullish_confluence if state.rsi_state == "overbought" else state.bearish_confluence
        if qualified:
            alert = ScreenerAlert(
                kind="rsi_overbought" if state.rsi_state == "overbought" else "rsi_oversold",
                symbol=state.symbol,
                price=state.price,
                rsi=state.rsi,
                adx=state.adx,
                relative_volume=state.relative_volume,
                supertrend_direction=state.supertrend_direction,
                confluence_count=confluence,
            )
            row.last_alerted_at = datetime.now(timezone.utc)
        row.state = state.rsi_state
    row.last_rsi = state.rsi
    row.last_price = state.price
    return alert


def run_technical_screener(
    db: Session,
    *,
    symbols: Iterable[str],
    provider_factory: Callable[[], BaseMarketDataProvider],
    settings,
) -> ScreenerRunResult:
    limited = list(symbols)[: settings.technical_screener_max_symbols_per_run]
    states, failed = _fetch_states(
        limited,
        provider_factory=provider_factory,
        workers=settings.technical_screener_workers,
        timeframe="1d",
        settings=settings,
    )
    alerts: list[ScreenerAlert] = []
    for state in states:
        for event in (_persist_cross_state(db, state, "1d"), _persist_rsi_state(db, state, "1d")):
            if event is not None:
                alerts.append(event)
    db.commit()
    return ScreenerRunResult(len(states), failed, tuple(alerts), tuple(states))


def run_intraday_vwap_scan(
    *,
    symbols: Iterable[str],
    provider_factory: Callable[[], BaseMarketDataProvider],
    settings,
) -> IntradayScanReport:
    limited = list(symbols)[: settings.technical_screener_max_symbols_per_run]
    states, failed = _fetch_states(
        limited,
        provider_factory=provider_factory,
        workers=settings.technical_screener_workers,
        timeframe="15m",
        settings=settings,
    )
    strong: list[str] = []
    weak: list[str] = []
    poc: list[str] = []
    for state in states:
        if state.price > state.vwap and state.relative_volume >= 1.2 and state.bullish_qualified:
            strong.append(state.symbol)
        elif state.price < state.vwap and state.relative_volume >= 1.2 and state.bearish_qualified:
            weak.append(state.symbol)
        if state.poc is not None and abs(state.price - state.poc) <= max(state.atr * 0.25, state.price * 0.002):
            side = "destek" if state.price >= state.poc else "direnç"
            poc.append(f"{state.symbol} ({side})")
    return IntradayScanReport(
        scanned=len(states),
        failed=failed,
        strong_above_vwap=tuple(strong[:25]),
        weak_below_vwap=tuple(weak[:25]),
        poc_reactions=tuple(poc[:25]),
        created_at=datetime.now(timezone.utc),
    )


def _direction_for_state(state: SymbolTechnicalState) -> Literal["bullish", "bearish"] | None:
    """Select one unambiguous direction; ties follow the active Supertrend."""

    if not state.bullish_qualified and not state.bearish_qualified:
        return None
    if state.bullish_qualified and not state.bearish_qualified:
        return "bullish"
    if state.bearish_qualified and not state.bullish_qualified:
        return "bearish"
    if state.bullish_confluence > state.bearish_confluence:
        return "bullish"
    if state.bearish_confluence > state.bullish_confluence:
        return "bearish"
    return "bullish" if state.supertrend_direction == "up" else "bearish"


def _core_trade_checks(
    state: SymbolTechnicalState,
    direction: Literal["bullish", "bearish"],
) -> tuple[tuple[str, bool], ...]:
    """Return the five indicators explicitly promised by the 15m radar.

    ATR has no directional sign by itself, so it acts as a tradeability filter:
    it must be large enough to make targets meaningful but not so large that a
    normal intraday stop becomes disproportionately risky.
    """

    bullish = direction == "bullish"
    ema_aligned = (
        state.ema20 > state.ema50 > state.ema100
        if bullish
        else state.ema20 < state.ema50 < state.ema100
    )
    rsi_aligned = 50 <= state.rsi < 75 if bullish else 25 < state.rsi <= 50
    atr_percent = state.atr / state.price * 100 if state.price else 0.0
    atr_tradeable = 0.10 <= atr_percent <= 6.0
    return (
        ("VWAP", (state.price > state.vwap) == bullish),
        ("EMA", ema_aligned),
        ("RSI", rsi_aligned),
        ("ATR", atr_tradeable),
        ("MACD", (state.macd_histogram > 0) == bullish),
    )


def _matching_confirmed_patterns(
    state: SymbolTechnicalState,
    direction: Literal["bullish", "bearish"],
) -> tuple[ChartPattern, ...]:
    """Only a closed-breakout pattern may add weight to a scenario."""

    return tuple(
        pattern
        for pattern in state.patterns
        if pattern.confirmed and pattern.direction == direction
    )


def _scenario_reasons(state: SymbolTechnicalState, direction: Literal["bullish", "bearish"]) -> tuple[str, ...]:
    bullish = direction == "bullish"
    reasons: list[str] = []
    ema_aligned = state.ema20 > state.ema50 > state.ema100 if bullish else state.ema20 < state.ema50 < state.ema100
    if ema_aligned:
        reasons.append("EMA20/50/100 yönü uyumlu")
    if (state.supertrend_direction == "up") == bullish:
        reasons.append(f"Supertrend {'yukarı' if bullish else 'aşağı'}")
    if (state.macd_histogram > 0) == bullish:
        reasons.append(f"MACD momentumu {'pozitif' if bullish else 'negatif'}")
    rsi_ok = 50 <= state.rsi < 75 if bullish else 25 < state.rsi <= 50
    if rsi_ok:
        reasons.append(f"RSI {state.rsi:.0f} yönü destekliyor")
    if state.adx >= 20:
        reasons.append(f"ADX {state.adx:.0f} trend gücü")
    if state.relative_volume >= 1.0 and state.obv_rising == bullish:
        reasons.append("Hacim/OBV teyidi")
    if (state.price > state.vwap) == bullish:
        reasons.append("VWAP konumu uyumlu")
    matching_patterns = _matching_confirmed_patterns(state, direction)
    if matching_patterns:
        reasons.append(f"Formasyon teyidi: {matching_patterns[0].name}")
    return tuple(reasons)


def build_trade_scenario(
    state: SymbolTechnicalState,
    *,
    minimum_core_confirmations: int = 3,
    minimum_ten_confirmations: int = 7,
) -> TradeScenario | None:
    """Build a retest-first plan from a technically qualified state.

    The current price is used only to decide whether the retest zone is close.
    It is never returned as the entry price, preventing last-close entries.
    """

    direction = _direction_for_state(state)
    if direction is None or not state.atr or state.atr <= 0:
        return None
    bullish = direction == "bullish"
    core_checks = _core_trade_checks(state, direction)
    core_confirmation_count = sum(is_confirmed for _, is_confirmed in core_checks)
    ten_confirmation_count = state.bullish_ten_confluence if bullish else state.bearish_ten_confluence
    ten_confirmation_labels = (
        state.bullish_ten_confirmation_labels if bullish else state.bearish_ten_confirmation_labels
    )
    confirmed_patterns = _matching_confirmed_patterns(state, direction)
    minimum_core_confirmations = max(3, min(5, int(minimum_core_confirmations)))
    minimum_ten_confirmations = max(3, min(10, int(minimum_ten_confirmations)))
    if (
        core_confirmation_count < minimum_core_confirmations
        or ten_confirmation_count < minimum_ten_confirmations
    ):
        return None
    confirmation_count = state.bullish_confluence if bullish else state.bearish_confluence
    zone_basis = (state.ema20, state.vwap)
    zone_padding = max(state.atr * 0.08, state.price * 0.0005)
    entry_low = min(zone_basis) - zone_padding
    entry_high = max(zone_basis) + zone_padding
    entry = (entry_low + entry_high) / 2
    stop_buffer = max(state.atr * 0.60, state.price * 0.003)

    if bullish:
        stop = entry_low - stop_buffer
        risk = entry - stop
        raw_resistance = state.resistance or (entry + risk * 2)
        tp1 = raw_resistance if (raw_resistance - entry) / risk >= 2 else entry + risk * 2
        tp2 = max(tp1 + risk, entry + risk * 3)
        near_zone = entry_low - state.atr * 0.20 <= state.price <= entry_high + state.atr * 0.30
        action: Literal["AL", "SAT", "BEKLE"] = "AL" if near_zone else "BEKLE"
        confirmation_instruction = "Bölgede 15dk yeşil kapanış ve hacim teyidi beklenmeli."
    else:
        stop = entry_high + stop_buffer
        risk = stop - entry
        raw_support = state.support or (entry - risk * 2)
        tp1 = raw_support if (entry - raw_support) / risk >= 2 else entry - risk * 2
        tp2 = min(tp1 - risk, entry - risk * 3)
        near_zone = entry_low - state.atr * 0.30 <= state.price <= entry_high + state.atr * 0.20
        action = "SAT" if near_zone else "BEKLE"
        confirmation_instruction = "Bölgede 15dk kırmızı kapanış ve hacim teyidi beklenmeli."

    rr = abs(tp1 - entry) / risk if risk > 0 else 0.0
    score = min(
        99,
        round(
            22
            + core_confirmation_count * 10
            + ten_confirmation_count * 4
            + confirmation_count * 5
            + min(8, max((pattern.confidence for pattern in confirmed_patterns), default=0) / 10)
            + min(state.adx, 30)
            + min(state.relative_volume * 3, 8)
        ),
    )
    return TradeScenario(
        symbol=state.symbol,
        action=action,
        direction=direction,
        score=score,
        confirmation_count=confirmation_count,
        core_confirmation_count=core_confirmation_count,
        core_checks=core_checks,
        ten_confirmation_count=ten_confirmation_count,
        ten_confirmation_labels=ten_confirmation_labels,
        price=state.price,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        rr=rr,
        atr_percent=(state.atr / state.price * 100) if state.price else 0.0,
        reasons=_scenario_reasons(state, direction),
        confirmation_instruction=confirmation_instruction,
        confirmed_patterns=confirmed_patterns,
    )


def run_intraday_trade_scenario_scan(
    *,
    symbols: Iterable[str],
    provider_factory: Callable[[], BaseMarketDataProvider],
    settings,
) -> TradeScenarioRunResult:
    """Scan the BIST universe using all ten indicators plus a five-core gate."""

    limited = list(symbols)[: settings.technical_screener_max_symbols_per_run]
    states, failed = _fetch_states(
        limited,
        provider_factory=provider_factory,
        workers=settings.technical_screener_workers,
        timeframe="15m",
        settings=settings,
    )
    minimum_core = max(3, min(5, int(getattr(settings, "trade_scenario_minimum_core_confirmations", 3))))
    # The automatic radar is intentionally stricter than manual exploration:
    # at least 8 of the 10 independent inputs must agree before a card is sent.
    minimum_ten = max(8, min(10, int(getattr(settings, "trade_scenario_minimum_ten_confirmations", 8))))
    scenarios = [
        scenario
        for state in states
        if (
            scenario := build_trade_scenario(
                state,
                minimum_core_confirmations=minimum_core,
                minimum_ten_confirmations=minimum_ten,
            )
        )
        is not None
    ]
    action_priority = {"AL": 0, "SAT": 1, "BEKLE": 2}
    scenarios.sort(key=lambda item: (action_priority[item.action], -item.score, item.symbol))
    maximum = max(1, min(5, int(getattr(settings, "trade_scenario_max_results", 5))))
    return TradeScenarioRunResult(
        scanned=len(states),
        failed=failed,
        scenarios=tuple(scenarios[:maximum]),
        created_at=datetime.now(timezone.utc),
        interval_minutes=180,
    )


def _daily_top_pick_checks(state: SymbolTechnicalState) -> tuple[tuple[str, bool], ...]:
    """Independent daily-long checks; daily VWAP is not counted as session VWAP.

    A one-day candle cannot recreate an intraday session VWAP accurately, so
    the hourly *daily* list deliberately relies on daily trend, momentum,
    participation and a confirmed chart pattern instead of pretending that a
    daily typical-price fallback is a session VWAP confirmation.
    """

    atr_percent = state.atr / state.price * 100 if state.price else 0.0
    return (
        ("EMA20/50/100", state.ema20 > state.ema50 > state.ema100),
        ("Fiyat > EMA100", state.price > state.ema100),
        ("Supertrend", state.supertrend_direction == "up"),
        ("RSI", 50 <= state.rsi <= 68),
        ("MACD", state.macd_histogram > 0),
        ("ADX", state.adx >= 20),
        ("Hacim/OBV", state.relative_volume >= 0.80 and state.obv_rising),
        ("ATR", 0.8 <= atr_percent <= 8.0),
    )


def _build_daily_top_pick(state: SymbolTechnicalState, *, minimum_confirmations: int) -> DailyTopPick | None:
    """Build a conservative daily-long plan from confirmed evidence only."""

    if state.price <= 0 or state.atr <= 0:
        return None
    patterns = _matching_confirmed_patterns(state, "bullish")
    if not patterns:
        return None
    checks = _daily_top_pick_checks(state)
    technical_confirmations = sum(passed for _, passed in checks)
    if technical_confirmations < max(5, min(8, int(minimum_confirmations))):
        return None

    pattern = max(patterns, key=lambda item: (item.confidence, item.name))
    support = state.support if state.support is not None and state.support < state.price else None
    zone_center = state.ema20
    if support is not None and abs(support - state.ema20) <= state.atr * 2.5:
        zone_center = (support + state.ema20) / 2.0
    zone_padding = max(state.atr * 0.15, state.price * 0.001)
    entry_low = max(0.0001, zone_center - zone_padding)
    entry_high = zone_center + zone_padding
    entry = (entry_low + entry_high) / 2.0
    stop_reference = min(entry_low, support) if support is not None else entry_low
    stop = max(0.0001, stop_reference - max(state.atr * 0.75, entry * 0.004))
    risk = entry - stop
    if risk <= 0:
        return None

    resistance = state.resistance if state.resistance is not None and state.resistance > entry else None
    tp1 = resistance or pattern.target
    if tp1 is None or tp1 <= entry:
        return None
    target_potential_percent = (tp1 / entry - 1.0) * 100
    rr = (tp1 - entry) / risk
    # A target should be a real next resistance/pattern objective.  We do not
    # invent a three-percent level simply to fill the hourly report.
    if target_potential_percent < 3.0 or rr < 2.0:
        return None
    tp2 = max(
        float(pattern.target) if pattern.target is not None else 0.0,
        tp1 + risk,
    )
    score = min(
        99,
        round(
            28
            + technical_confirmations * 7
            + min(pattern.confidence, 90) * 0.12
            + min(state.adx, 35) * 0.25
            + min(state.relative_volume, 2.0) * 4
        ),
    )
    reasons = tuple(name for name, passed in checks if passed)
    return DailyTopPick(
        symbol=state.symbol,
        score=score,
        technical_confirmations=technical_confirmations,
        price=state.price,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        rr=rr,
        target_potential_percent=target_potential_percent,
        pattern=pattern,
        reasons=reasons,
        confirmation_instruction="Giriş ancak bölgeye geri çekilme ve günlük/15dk yeşil kapanış teyidiyle düşünülür.",
        fundamental_score=None,
        fundamental_status="DOĞRULANMADI",
        fundamental_source=None,
    )


def _verify_pick_fundamentals(pick: DailyTopPick, provider) -> DailyTopPick | None:
    """Allow only verified, sufficiently complete fundamentals into the strong list."""

    from app.services.company_analysis_service import analyze_company

    try:
        analysis = analyze_company(pick.symbol, fundamental_provider=provider)
    except Exception as exc:  # noqa: BLE001 - a single company must not stop the universe scan
        logger.info("Gunluk ilk 5 temel dogrulama atlandi symbol=%s error=%s", pick.symbol, type(exc).__name__)
        return None
    status = str(analysis.status)
    if status in {"RİSKLİ", "VERİ YETERSİZ"} or analysis.score < 65 or analysis.data_coverage < 60:
        return None
    final_score = min(99, round(pick.score * 0.68 + analysis.score * 0.32))
    return replace(
        pick,
        score=final_score,
        fundamental_score=int(analysis.score),
        fundamental_status=status,
        fundamental_source=str(analysis.source),
    )


def run_daily_top_picks_scan(
    *,
    symbols: Iterable[str],
    provider_factory: Callable[[], BaseMarketDataProvider],
    settings,
    fundamental_provider_factory: Callable[[], object] | None = None,
) -> DailyTopPicksRunResult:
    """Scan the BIST universe for up to five verified daily-long candidates.

    Technical screening runs on every configured symbol.  The optional
    fundamental provider is queried only for the best technical short-list,
    never for 600 symbols at once.  If strict fundamental verification is
    enabled and a source is unavailable, the report sends an explicit no-pick
    notice rather than labeling an unverified company as financially strong.
    """

    limited = list(symbols)[: settings.technical_screener_max_symbols_per_run]
    states, failed = _fetch_states(
        limited,
        provider_factory=provider_factory,
        workers=settings.technical_screener_workers,
        timeframe="1d",
        settings=settings,
    )
    minimum = max(5, min(8, int(getattr(settings, "daily_top_picks_minimum_confirmations", 6))))
    candidates = [
        candidate
        for state in states
        if (candidate := _build_daily_top_pick(state, minimum_confirmations=minimum)) is not None
    ]
    candidates.sort(key=lambda item: (-item.score, -item.target_potential_percent, item.symbol))
    inspect_limit = max(5, min(40, int(getattr(settings, "daily_top_picks_fundamental_candidates", 20))))
    shortlist = candidates[:inspect_limit]
    require_fundamental = bool(getattr(settings, "daily_top_picks_require_fundamental", True))
    verified: list[DailyTopPick] = []
    checked = 0
    if fundamental_provider_factory is not None and shortlist:
        try:
            fundamental_provider = fundamental_provider_factory()
        except Exception as exc:  # noqa: BLE001
            logger.info("Gunluk ilk 5 temel saglayici olusturulamadi: %s", type(exc).__name__)
            fundamental_provider = None
        if fundamental_provider is not None:
            for candidate in shortlist:
                checked += 1
                verified_pick = _verify_pick_fundamentals(candidate, fundamental_provider)
                if verified_pick is not None:
                    verified.append(verified_pick)
    selected = verified if require_fundamental else (verified or shortlist)
    selected.sort(key=lambda item: (-item.score, -item.target_potential_percent, item.symbol))
    maximum = max(1, min(10, int(getattr(settings, "daily_top_picks_max_results", 5))))
    return DailyTopPicksRunResult(
        scanned=len(states),
        failed=failed,
        picks=tuple(selected[:maximum]),
        fundamental_checked=checked,
        fundamental_verified=len(verified),
        created_at=datetime.now(timezone.utc),
    )


def _state_rank(state: SymbolTechnicalState, *, use_ten_indicator_score: bool = False) -> float:
    confirmations = (
        max(state.bullish_ten_confluence, state.bearish_ten_confluence)
        if use_ten_indicator_score
        else max(state.bullish_confluence, state.bearish_confluence)
    )
    return (
        confirmations * 20
        + min(state.adx, 40)
        + min(state.relative_volume * 5, 15)
    )


def _market_direction_for_state(state: SymbolTechnicalState) -> Literal["bullish", "bearish"] | None:
    """Choose a direction only when the strict ten-indicator rule qualifies."""

    if not state.bullish_ten_qualified and not state.bearish_ten_qualified:
        return None
    if state.bullish_ten_qualified and not state.bearish_ten_qualified:
        return "bullish"
    if state.bearish_ten_qualified and not state.bullish_ten_qualified:
        return "bearish"
    if state.bullish_ten_confluence > state.bearish_ten_confluence:
        return "bullish"
    if state.bearish_ten_confluence > state.bullish_ten_confluence:
        return "bearish"
    return "bullish" if state.supertrend_direction == "up" else "bearish"


def run_market_opportunity_scan(
    *,
    symbols: Iterable[str],
    provider_factory: Callable[[], BaseMarketDataProvider],
    settings,
    timeframe: Literal["5m", "1h", "4h"] = "1h",
) -> MarketOpportunityReport:
    """Classify the complete BIST universe with a strict ten-indicator filter.

    ``timeframe`` is deliberately one period per command.  Scanning all three
    periods together would make over 1,700 data requests for a 571-share
    universe, which is slow and can hit free-data rate limits.  Users can run
    `/firsatlar 5dk`, `/firsatlar 1s` and `/firsatlar 4s` independently.
    """

    if timeframe not in {"5m", "1h", "4h"}:
        raise ValueError("Firsatlar taramasi yalnizca 5m, 1h veya 4h destekler.")

    limited = list(symbols)[: settings.technical_screener_max_symbols_per_run]
    states, failed = _fetch_states(
        limited,
        provider_factory=provider_factory,
        workers=settings.technical_screener_workers,
        timeframe=timeframe,
        settings=settings,
    )
    minimum = max(3, min(10, int(getattr(settings, "market_opportunity_minimum_confluence", 5))))

    def atr_percent(item: SymbolTechnicalState) -> float:
        return item.atr / item.price * 100 if item.price else 0.0

    eligible = [
        state
        for state in states
        if _market_direction_for_state(state) is not None
        and max(state.bullish_ten_confluence, state.bearish_ten_confluence) >= minimum
        and state.adx >= 20
        and atr_percent(state) <= 8
    ]
    al_sat = [state for state in eligible if state.relative_volume >= 0.70]
    short_term = [
        state
        for state in eligible
        if max(state.bullish_ten_confluence, state.bearish_ten_confluence) >= min(10, minimum + 1)
    ]
    long_term = [
        state
        for state in states
        if state.price > state.ema100
        and state.ema20 > state.ema50 > state.ema100
        and state.adx >= 20
        and 48 <= state.rsi <= 70
        and state.bullish_ten_confluence >= minimum
    ]
    speculative = [
        state
        for state in states
        if atr_percent(state) >= 6.5 or (state.relative_volume >= 3.0 and atr_percent(state) >= 3.0)
    ]
    limit = max(3, min(12, int(getattr(settings, "market_opportunity_max_results", 8))))
    order = lambda items: tuple(
        sorted(items, key=lambda state: _state_rank(state, use_ten_indicator_score=True), reverse=True)[:limit]
    )
    return MarketOpportunityReport(
        scanned=len(states),
        failed=failed,
        timeframe=timeframe,
        minimum_confluence=minimum,
        al_sat_uygun=order(al_sat),
        kisa_vade=order(short_term),
        uzun_vade_teknik=order(long_term),
        spekulatif_uyari=order(speculative),
        created_at=datetime.now(timezone.utc),
    )


def format_screener_alert(alert: ScreenerAlert) -> str:
    volume = "yüksek" if alert.relative_volume >= 1.2 else "normal"
    if alert.kind in {"golden_cross", "death_cross"}:
        golden = alert.kind == "golden_cross"
        return (
            f"{'🟢 GOLDEN CROSS' if golden else '🔴 DEATH CROSS'} — {alert.symbol}\n"
            f"EMA50 / EMA100 kesişimi ({'yukarı' if golden else 'aşağı'})\n"
            f"Fiyat: {alert.price:.2f} | ADX: {alert.adx:.1f} | Hacim: {volume}\n"
            f"🧩 Confluence: {alert.confluence_count} bağımsız teyit"
        )
    overbought = alert.kind == "rsi_overbought"
    return (
        f"🔥 RSI EKSTREM — {alert.symbol}\n"
        f"RSI(14): {alert.rsi:.1f} ({'Aşırı Alım' if overbought else 'Aşırı Satım'} Bölgesi)\n"
        f"Fiyat: {alert.price:.2f} | Supertrend: {'Yukarı' if alert.supertrend_direction == 'up' else 'Aşağı'}\n"
        f"🧩 Confluence: {alert.confluence_count} bağımsız teyit"
    )


def format_intraday_scan_report(report: IntradayScanReport, *, timezone_name: str = "Europe/Istanbul") -> str:
    from zoneinfo import ZoneInfo

    local = report.created_at.astimezone(ZoneInfo(timezone_name))
    next_time = local + timedelta(minutes=30)

    def compact(items: tuple[str, ...], maximum: int = 6) -> str:
        selected = list(items[:maximum])
        if not selected:
            return "yok"
        remainder = len(items) - len(selected)
        return " • ".join(selected) + (f" • +{remainder} diğer" if remainder > 0 else "")

    return "\n".join(
        [
            "┏━━ 📊 VWAP & HACİM ÖZETİ ━━┓",
            f"🕒 {local:%H:%M}  •  {report.scanned} hisse tarandı  •  {report.failed} atlandı",
            "",
            "🟢 VWAP üstünde güçlenenler",
            compact(report.strong_above_vwap),
            "",
            "🔴 VWAP altında zayıflayanlar",
            compact(report.weak_below_vwap),
            "",
            "🟠 POC yakınında tepki",
            compact(report.poc_reactions),
            "",
            "ℹ️ Bu özet tek başına işlem sinyali değildir; giriş için senaryo teyidi gerekir.",
            f"⏱ Sonraki tarama: {next_time:%H:%M}",
        ]
    )[:4096]


def _format_price(value: float) -> str:
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def format_trade_scenario_report(
    report: TradeScenarioRunResult,
    *,
    timezone_name: str = "Europe/Istanbul",
) -> str:
    """Render a deliberately short Telegram card, never a 571-symbol dump."""

    from zoneinfo import ZoneInfo

    local = report.created_at.astimezone(ZoneInfo(timezone_name))
    interval_label = (
        f"{report.interval_minutes // 60} SAATLİK"
        if report.interval_minutes >= 60 and report.interval_minutes % 60 == 0
        else f"{report.interval_minutes} DK"
    )
    lines = [
        f"┏━━ ✨ {interval_label} 10 İNDİKATÖRLÜ FIRSAT RADARI ━━┓",
        f"🕒 {local:%H:%M}  •  {report.scanned} hisse tarandı  •  {report.failed} atlandı",
        "🧩 VWAP • Anchored VWAP • EMA • Supertrend • RSI • MACD • ADX • Bollinger • OBV • VP/POC",
        "✅ 10 GÖSTERGE UYUMLU: en az 8 aynı yön + 5 çekirdek göstergeden en az 3 teyit.",
        "📌 Giriş yalnız retest bölgesinden; güncel fiyattan otomatik giriş yoktur.",
    ]
    if not report.scenarios:
        lines.extend(
            [
                "",
                "🟡 Şu an 10 göstergeli kalite filtresini geçen temiz bir senaryo yok.",
                "⏳ Zorla işlem yok: sonraki radar turu bekleniyor.",
            ]
        )
        return "\n".join(lines)

    action_icon = {"AL": "🟢", "SAT": "🔴", "BEKLE": "🟡"}
    action_title = {"AL": "AL ADAYI", "SAT": "SAT / KORUMA", "BEKLE": "BEKLE"}
    reason_label = {
        "AL": "Neden giriş düşünülebilir",
        "SAT": "Neden azaltma düşünülebilir",
        "BEKLE": "Neden beklenmeli",
    }
    for rank, scenario in enumerate(report.scenarios, start=1):
        direction_name = "yukarı" if scenario.direction == "bullish" else "aşağı"
        direction_label = "LONG" if scenario.direction == "bullish" else "SHORT / KORUMA"
        reasons = " • ".join(scenario.reasons[:4]) or "Teknik teyitler izleniyor"
        lines.extend(
            [
                "",
                f"{rank}. {action_icon[scenario.action]} {scenario.symbol} — {scenario.ten_confirmation_count}/10 gösterge uyumlu",
                f"   Yön: {direction_label} • Güç skoru: {scenario.score}/100 • Durum: {action_title[scenario.action]}",
                f"   Kısa gerekçe: {reasons}",
                f"   Giriş bölgesi: {_format_price(scenario.entry_low)}–{_format_price(scenario.entry_high)} • Stop: {_format_price(scenario.stop)}",
                f"   Hedef: TP1 {_format_price(scenario.tp1)} / TP2 {_format_price(scenario.tp2)} • RR 1:{scenario.rr:.1f}",
                f"   Teknik teyit: {scenario.core_confirmation_count}/5 çekirdek • ATR %{scenario.atr_percent:.1f} • {direction_name} yapı",
                *(
                    [
                        f"📐 Formasyon: {scenario.confirmed_patterns[0].name} ✅ "
                        f"({scenario.confirmed_patterns[0].detail})"
                    ]
                    if scenario.confirmed_patterns
                    else []
                ),
                f"   {reason_label[scenario.action]}: {scenario.confirmation_instruction}",
            ]
        )
    lines.extend(
        [
            "",
            "ℹ️ AL/SAT bir koşullu teknik senaryodur; fiyatın güncel seviyesinden otomatik giriş değildir.",
            "⚠️ SAT, BIST spotta açığa satış çağrısı değil; eldeki pozisyon için azaltma/koruma çerçevesidir.",
        ]
    )
    return "\n".join(lines)[:4096]


def format_daily_top_picks_report(
    report: DailyTopPicksRunResult,
    *,
    timezone_name: str = "Europe/Istanbul",
) -> str:
    """Render the hourly daily-long report without claiming a guaranteed return."""

    from zoneinfo import ZoneInfo

    local = report.created_at.astimezone(ZoneInfo(timezone_name))
    lines = [
        "┏━━ 🏆 GÜNLÜK İLK 5 KALİTELİ LONG RADARI ━━┓",
        f"🕒 {local:%H:%M}  •  {report.scanned} hisse tarandı  •  {report.failed} veri yetersiz",
        (
            f"🧾 Temel doğrulama: {report.fundamental_verified}/{report.fundamental_checked} teknik aday geçti"
            if report.fundamental_checked
            else "🧾 Temel doğrulama: kaynak sonucu bekleniyor"
        ),
        "🛡 Şart: günlük çoklu teyit + kapanışla doğrulanmış formasyon + gerçek dirençe kadar ≥%3 potansiyel + RR ≥1:2.",
        "📌 Giriş yalnız retest bölgesinden; güncel fiyattan otomatik AL yok.",
    ]
    if not report.picks:
        lines.extend(
            [
                "",
                "🟡 Bugün bu standartları birlikte geçen doğrulanmış aday yok.",
                "Zorla 5 hisse üretmem: teknik/formasyon veya temel doğrulama eksikse sonraki saat beklenir.",
            ]
        )
        return "\n".join(lines)

    for rank, pick in enumerate(report.picks, start=1):
        reasons = " • ".join(pick.reasons[:5])
        fundamental = (
            f"{pick.fundamental_status} {pick.fundamental_score}/100"
            if pick.fundamental_score is not None
            else "doğrulanmadı"
        )
        lines.extend(
            [
                "",
                f"{rank}. 🟢 {pick.symbol}  •  KALİTE {pick.score}/100  •  {pick.technical_confirmations}/8 teknik teyit",
                f"📐 Formasyon: {pick.pattern.name} ✅ — {pick.pattern.detail}",
                f"🏢 Temel görünüm: {fundamental}",
                f"📍 Fiyat: {_format_price(pick.price)}  →  Giriş bölgesi: {_format_price(pick.entry_low)}–{_format_price(pick.entry_high)}",
                f"🛑 Geçersizlik/Stop: {_format_price(pick.stop)}",
                f"🎯 TP1: {_format_price(pick.tp1)}  •  TP2: {_format_price(pick.tp2)}  •  Potansiyel: %{pick.target_potential_percent:.1f}",
                f"⚖️ RR 1:{pick.rr:.1f}  •  Neden: {reasons}",
                f"⏳ Teyit: {pick.confirmation_instruction}",
            ]
        )
    lines.extend(
        [
            "",
            "ℹ️ ‘Potansiyel’ geçmiş destek/direnç ve formasyon hedefinden hesaplanır; %3 kâr garantisi değildir.",
            "⚠️ Bu koşullu teknik izleme listesidir; bilanço/KAP, likidite ve piyasa riski ayrıca kontrol edilmelidir.",
        ]
    )
    return "\n".join(lines)[:4096]


def format_market_opportunity_report(
    report: MarketOpportunityReport,
    *,
    timezone_name: str = "Europe/Istanbul",
) -> str:
    """Explain categories without portraying volatility as manipulation evidence."""

    from zoneinfo import ZoneInfo

    local = report.created_at.astimezone(ZoneInfo(timezone_name))
    timeframe_label = {"5m": "5 DK", "1h": "1 SAAT", "4h": "4 SAAT"}[report.timeframe]

    def item(state: SymbolTechnicalState, *, with_signal: bool = False) -> str:
        direction = _market_direction_for_state(state)
        label = "AL" if direction == "bullish" else "SAT/KORUMA" if direction == "bearish" else "İZLE"
        atr_percent = state.atr / state.price * 100 if state.price else 0.0
        prefix = f"{label} · " if with_signal else ""
        pattern = next((row for row in state.patterns if row.confirmed), None)
        pattern_text = f" • 📐 {pattern.name}" if pattern is not None else ""
        return (
            f"• {prefix}{state.symbol} — {max(state.bullish_ten_confluence, state.bearish_ten_confluence)}/10 teyit"
            f", ADX {state.adx:.0f}, ATR %{atr_percent:.1f}{pattern_text}"
        )

    def section(title: str, states: tuple[SymbolTechnicalState, ...], *, with_signal: bool = False) -> list[str]:
        rendered = [title]
        rendered.extend(item(state, with_signal=with_signal) for state in states)
        if not states:
            rendered.append("• Bugünkü taramada öne çıkan aday yok")
        return rendered

    lines = [
        "┏━━ 🧭 FIRSAT & RİSK LİSTESİ ━━┓",
        f"🕒 {local:%d.%m %H:%M}  •  {report.scanned} hisse tarandı  •  {report.failed} atlandı",
        "",
        *section("🎯 AL-SAT için teknik adaylar", report.al_sat_uygun, with_signal=True),
        "",
        *section("⚡ Kısa vade momentumu güçlü", report.kisa_vade, with_signal=True),
        "",
        *section("🌱 Trend takip adayları", report.uzun_vade_teknik),
        "",
        *section("⚠️ Spekülatif / yüksek oynaklık uyarısı", report.spekulatif_uyari),
        "",
        "ℹ️ Son bölüm manipülasyon iddiası değildir; yüksek ATR ve olağandışı hacim kaynaklı teknik risk uyarısıdır.",
        "🧾 Trend takip bölümü yalnız tekniktir; bilanço, borçluluk ve değerleme ayrıca doğrulanmalıdır.",
    ]
    lines.insert(2, f"⏱ Zaman dilimi: {timeframe_label}  •  10 gösterge  •  Eşik: {report.minimum_confluence}/10 teyit")
    lines.append("📌 Giriş için retest ve mum/yapı teyidi gerekir; bu kart güncel fiyattan otomatik giriş önermez.")
    return "\n".join(lines)[:4096]
