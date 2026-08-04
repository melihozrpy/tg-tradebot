from __future__ import annotations

"""Batch-friendly full-universe EMA/RSI and intraday VWAP/VP scanner."""

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
)
from app.data.base_provider import BaseMarketDataProvider
from app.models.database import EmaCrossState, RsiAlertState

logger = logging.getLogger("mergen_quant.screener")


@dataclass(frozen=True)
class SymbolTechnicalState:
    symbol: str
    price: float
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
    vwap: float
    poc: float | None
    vah: float | None
    val: float | None
    atr: float
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


def analyze_symbol_frame(
    symbol: str,
    frame: pd.DataFrame,
    *,
    rsi_overbought: float = 75.0,
    rsi_oversold: float = 25.0,
    minimum_confluence: int = 3,
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
    atr_values = atr(data, 14)
    return SymbolTechnicalState(
        symbol=symbol.upper().removesuffix(".IS"),
        price=float(last["close"]),
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
        vwap=float(last["vwap"]),
        poc=bundle.volume_profile.poc,
        vah=bundle.volume_profile.vah,
        val=bundle.volume_profile.val,
        atr=float(atr_values.iloc[-1]),
        timestamp=last["timestamp"],
    )


def _fetch_states(
    symbols: Iterable[str],
    *,
    provider_factory: Callable[[], BaseMarketDataProvider],
    workers: int,
    timeframe: str,
    settings,
) -> tuple[list[SymbolTechnicalState], int]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=540 if timeframe == "1d" else 58)
    local = threading.local()

    def fetch(symbol: str) -> SymbolTechnicalState:
        provider = getattr(local, "provider", None)
        if provider is None:
            provider = provider_factory()
            local.provider = provider
        frame = provider.get_ohlcv(symbol, timeframe, start, end)
        return analyze_symbol_frame(
            symbol,
            frame,
            rsi_overbought=settings.rsi_overbought,
            rsi_oversold=settings.rsi_oversold,
            minimum_confluence=settings.technical_screener_min_confluence,
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
    return "\n".join(
        [
            f"📊 30dk Otomatik Tarama — {local:%H:%M}",
            f"Taranan evren: {report.scanned} | Veri hatası/atlanan: {report.failed}",
            "",
            "VWAP Üzerinde Güçlü: " + (", ".join(report.strong_above_vwap) or "yok"),
            "VWAP Altında Zayıf: " + (", ".join(report.weak_below_vwap) or "yok"),
            "POC Seviyesinde Tepki: " + (", ".join(report.poc_reactions) or "yok"),
            "",
            "Not: Listeye girmek için en az 3 bağımsız indikatör teyidi gerekir.",
            f"⏱ Sonraki tarama: {next_time:%H:%M}",
        ]
    )[:4096]
