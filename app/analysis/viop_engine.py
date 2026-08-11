"""VIOP education, eligible-underlying configuration and risk-first scenarios.

The module intentionally analyses the *spot underlying* only.  A freely
available OHLCV feed is not a trustworthy source for the live futures price,
open interest, basis or broker-specific initial margin.  Those values must be
confirmed in the investor's broker screen before an order is considered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from app.analysis.screener_engine import (
    SymbolTechnicalState,
    TradeScenario,
    analyze_symbol_frame,
    build_trade_scenario,
)
from app.config.settings import BASE_DIR
from app.data.base_provider import BaseMarketDataProvider

ViopHorizon = Literal["gunici", "haftalik", "aylik"]


@dataclass(frozen=True)
class ViopUnderlying:
    symbol: str
    market_maker_group: int
    standard_contract_multiplier: int


@dataclass(frozen=True)
class ViopUniverse:
    source_url: str
    verified_on: str
    notice: str
    underlyings: tuple[ViopUnderlying, ...]


@dataclass(frozen=True)
class ViopRiskEstimate:
    capital: float
    risk_percent: float
    risk_budget: float
    estimated_loss_per_contract: float
    maximum_contracts_by_stop: int
    multiplier: int
    requires_live_margin_check: bool = True


@dataclass(frozen=True)
class ViopSpotAnalysis:
    underlying: ViopUnderlying
    horizon: ViopHorizon
    state: SymbolTechnicalState
    scenario: TradeScenario | None


_HORIZON_ALIASES: dict[str, ViopHorizon] = {
    "gunici": "gunici",
    "günici": "gunici",
    "intraday": "gunici",
    "haftalik": "haftalik",
    "hafta": "haftalik",
    "aylik": "aylik",
    "ay": "aylik",
}


def _resolve_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else BASE_DIR / candidate


def load_viop_universe(path: str | Path) -> ViopUniverse:
    """Load a dated, reviewable VIOP underlying watch universe from JSON."""

    payload = json.loads(_resolve_path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("underlyings"), list):
        raise ValueError("VIOP dayanak listesi gecersiz.")
    values: list[ViopUnderlying] = []
    seen: set[str] = set()
    for item in payload["underlyings"]:
        symbol = str(item.get("symbol") or "").strip().upper().removesuffix(".IS")
        group = int(item.get("market_maker_group") or 0)
        multiplier = int(item.get("standard_contract_multiplier") or 0)
        if not symbol or symbol in seen or group not in {1, 2, 3} or multiplier <= 0:
            continue
        seen.add(symbol)
        values.append(ViopUnderlying(symbol, group, multiplier))
    if not values:
        raise ValueError("VIOP dayanak listesi bos.")
    return ViopUniverse(
        source_url=str(payload.get("source_url") or ""),
        verified_on=str(payload.get("verified_on") or ""),
        notice=str(payload.get("important_notice") or ""),
        underlyings=tuple(values),
    )


def parse_viop_horizon(raw: str | None) -> ViopHorizon | None:
    if raw is None or not str(raw).strip():
        return "gunici"
    return _HORIZON_ALIASES.get(str(raw).strip().casefold())


def priority_viop_symbols(universe: ViopUniverse, *, maximum: int = 15) -> list[str]:
    """Return group-1 names first; this is a watch priority, not liquidity proof."""

    ordered = sorted(universe.underlyings, key=lambda item: (item.market_maker_group, item.symbol))
    return [item.symbol for item in ordered[: max(1, int(maximum))]]


def find_viop_underlying(universe: ViopUniverse, symbol: str) -> ViopUnderlying | None:
    normalized = str(symbol or "").strip().upper().removesuffix(".IS")
    return next((item for item in universe.underlyings if item.symbol == normalized), None)


def _fetch_window(horizon: ViopHorizon) -> tuple[str, datetime]:
    end = datetime.now(timezone.utc)
    if horizon == "gunici":
        return "1h", end - timedelta(days=120)
    # Weekly/monthly decisions need daily closed bars and enough history for EMA200.
    return "1d", end - timedelta(days=540)


def analyze_viop_spot_underlying(
    underlying: ViopUnderlying,
    *,
    provider: BaseMarketDataProvider,
    settings,
    horizon: ViopHorizon,
) -> ViopSpotAnalysis:
    """Create a conditional scenario from the underlying's completed spot bars."""

    timeframe, start = _fetch_window(horizon)
    frame = provider.get_ohlcv(underlying.symbol, timeframe, start, datetime.now(timezone.utc))
    state = analyze_symbol_frame(
        underlying.symbol,
        frame,
        rsi_overbought=settings.rsi_overbought,
        rsi_oversold=settings.rsi_oversold,
        minimum_confluence=settings.technical_screener_min_confluence,
        ten_indicator_minimum=8,
        timeframe=timeframe,
    )
    scenario = build_trade_scenario(state, minimum_core_confirmations=3, minimum_ten_confirmations=8)
    return ViopSpotAnalysis(underlying=underlying, horizon=horizon, state=state, scenario=scenario)


def estimate_viop_contract_risk(
    *,
    capital: float,
    entry_spot: float,
    stop_spot: float,
    multiplier: int,
    risk_percent: float = 0.5,
) -> ViopRiskEstimate:
    """Estimate stop loss on the standard multiplier, never margin affordability.

    Futures basis means this is deliberately an estimate based on the spot plan.
    The result is rounded *down* to whole contracts and zero is a valid, useful
    outcome for a small balance.
    """

    if capital <= 0 or entry_spot <= 0 or multiplier <= 0:
        raise ValueError("Sermaye, giris ve kontrat carpani pozitif olmalidir.")
    if not 0 < risk_percent <= 1:
        raise ValueError("VIOP risk yuzdesi 0 ile 1 arasinda olmalidir.")
    stop_distance = abs(entry_spot - stop_spot)
    if stop_distance <= 0:
        raise ValueError("Stop mesafesi sifir olamaz.")
    risk_budget = capital * risk_percent / 100.0
    loss_per_contract = stop_distance * multiplier
    contracts = int(risk_budget // loss_per_contract)
    return ViopRiskEstimate(
        capital=round(capital, 2),
        risk_percent=risk_percent,
        risk_budget=round(risk_budget, 2),
        estimated_loss_per_contract=round(loss_per_contract, 2),
        maximum_contracts_by_stop=max(0, contracts),
        multiplier=multiplier,
    )


def horizon_guidance(horizon: ViopHorizon) -> str:
    if horizon == "gunici":
        return "Gün içi: 1s yapıyı izle; plana aykırıysa taşıma. Akşam seansı/boşluk riski ayrıca kontrol edilir."
    if horizon == "haftalik":
        return "Haftalık: Her günlük kapanışta stop, teminat ve vade mesafesi yeniden değerlendirilir; bu bir taşıma vaadi değildir."
    return "Aylık: Yeni başlayan veya küçük teminat için varsayılan değildir. Vade, fiziki teslimat ve günlük teminat çağrısı ayrı ayrı yönetilmeden taşınmaz."


def spot_direction_label(state: SymbolTechnicalState) -> str:
    bull, bear = state.bullish_ten_confluence, state.bearish_ten_confluence
    if bull >= 8 and bull > bear:
        return "LONG İÇİN KOŞULLU İZLEME"
    if bear >= 8 and bear > bull:
        return "SHORT İÇİN KOŞULLU İZLEME"
    return "BEKLE — 8/10 UYUM YOK"
