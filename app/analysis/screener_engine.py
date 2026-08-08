from __future__ import annotations

"""Batch-friendly full-universe confluence, scenario and VWAP scanners."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
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


@dataclass(frozen=True)
class TradeScenarioRunResult:
    scanned: int
    failed: int
    scenarios: tuple[TradeScenario, ...]
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
    return tuple(reasons)


def build_trade_scenario(state: SymbolTechnicalState) -> TradeScenario | None:
    """Build a retest-first plan from a technically qualified state.

    The current price is used only to decide whether the retest zone is close.
    It is never returned as the entry price, preventing last-close entries.
    """

    direction = _direction_for_state(state)
    if direction is None or not state.atr or state.atr <= 0:
        return None
    bullish = direction == "bullish"
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
        round(35 + confirmation_count * 9 + min(state.adx, 35) + min(state.relative_volume * 4, 10)),
    )
    return TradeScenario(
        symbol=state.symbol,
        action=action,
        direction=direction,
        score=score,
        confirmation_count=confirmation_count,
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
    )


def run_intraday_trade_scenario_scan(
    *,
    symbols: Iterable[str],
    provider_factory: Callable[[], BaseMarketDataProvider],
    settings,
) -> TradeScenarioRunResult:
    """Scan the universe every 15 minutes and keep only 3+ confluence setups."""

    limited = list(symbols)[: settings.technical_screener_max_symbols_per_run]
    states, failed = _fetch_states(
        limited,
        provider_factory=provider_factory,
        workers=settings.technical_screener_workers,
        timeframe="15m",
        settings=settings,
    )
    scenarios = [scenario for state in states if (scenario := build_trade_scenario(state)) is not None]
    action_priority = {"AL": 0, "SAT": 1, "BEKLE": 2}
    scenarios.sort(key=lambda item: (action_priority[item.action], -item.score, item.symbol))
    maximum = max(3, min(12, int(getattr(settings, "trade_scenario_max_results", 6))))
    return TradeScenarioRunResult(
        scanned=len(states),
        failed=failed,
        scenarios=tuple(scenarios[:maximum]),
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
    lines = [
        "┏━━ 📡 15 DK FIRSAT RADARI ━━┓",
        f"🕒 {local:%H:%M}  •  {report.scanned} hisse tarandı  •  {report.failed} atlandı",
        "🧩 Yalnızca en az 3 bağımsız teyitli, retest-bazlı senaryolar seçildi.",
    ]
    if not report.scenarios:
        lines.extend(
            [
                "",
                "🟡 Şu an 3+ teyitli temiz bir senaryo yok.",
                "⏳ Zorla işlem yok: sonraki 15dk mum kapanışı bekleniyor.",
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
    for scenario in report.scenarios:
        direction_name = "yukarı" if scenario.direction == "bullish" else "aşağı"
        reasons = " • ".join(scenario.reasons[:4]) or "Teknik teyitler izleniyor"
        lines.extend(
            [
                "",
                f"{action_icon[scenario.action]} {action_title[scenario.action]} · {scenario.symbol}  |  {scenario.score}/100",
                f"🎯 Beklenen giriş: {_format_price(scenario.entry_low)}–{_format_price(scenario.entry_high)}",
                f"🛑 Geçersizlik: {_format_price(scenario.stop)}  |  🎯 Hedef: {_format_price(scenario.tp1)} / {_format_price(scenario.tp2)}",
                f"⚖️ RR 1:{scenario.rr:.1f}  •  ATR %{scenario.atr_percent:.1f}  •  {scenario.confirmation_count} teyit",
                f"✅ {reason_label[scenario.action]} ({direction_name}): {reasons}",
                f"⏳ Teyit: {scenario.confirmation_instruction}",
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
        return (
            f"• {prefix}{state.symbol} — {max(state.bullish_ten_confluence, state.bearish_ten_confluence)}/10 teyit"
            f", ADX {state.adx:.0f}, ATR %{atr_percent:.1f}"
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
