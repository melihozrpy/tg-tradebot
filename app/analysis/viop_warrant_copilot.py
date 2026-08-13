"""Confirmation-first VIOP and warrant scenario engine.

This module intentionally calculates technical levels from completed underlying
OHLC bars.  It never fabricates a futures/warrant quote, margin, open interest,
spread, expiry, or warrant delta.  The Telegram layer supplies verified product
metadata when it is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd

from app.analysis.screener_engine import SymbolTechnicalState, analyze_symbol_frame
from app.analysis.smart_money_engine import SmartMoneyResult, detect_smart_money
from app.analysis.viop_engine import ViopHorizon
from app.data.base_provider import BaseMarketDataProvider

CopilotProduct = Literal["viop", "varant"]


BORSA_COPILOT_SYSTEM_PROMPT = """Sen VIOP ve Varant piyasalarında kurumsal disiplinle çalışan Borsa Copilot'sın.
Kısa ve net konuşursun; yalnızca tamamlanmış mumlardan gelen doğrulanabilir seviyeleri kullanırsın.
HTF önce, sonra LTF; OB/FVG + sweep + CHoCH/BOS teyidi aranır. Altı kontrolün tamamı geçmeden
sinyal vermez, BEKLE dersin. Giriş bölgesi, stop, TP1/TP2/TP3 ve minimum 1:2 R/R zorunludur.
Kaldıraç, vade, teminat, likidite ve varant deltası/veri eksikliği riskini açıkça belirtirsin.
Kesin kazanç veya kesin yön ifadesi kullanmazsın."""


@dataclass(frozen=True)
class NewsRiskCheck:
    clean: bool
    detail: str


@dataclass(frozen=True)
class CopilotChecklistItem:
    key: str
    label: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class CopilotScenario:
    product: CopilotProduct
    symbol: str
    horizon: ViopHorizon
    generated_at: datetime
    bias: str
    current_price: float
    entry_low: float | None
    entry_high: float | None
    stop: float | None
    targets: tuple[float, ...]
    rr: float | None
    pending: bool
    valid_setup: bool
    checklist: tuple[CopilotChecklistItem, ...]
    note: str
    htf: SymbolTechnicalState
    ltf: SymbolTechnicalState


def _price_decimals(symbol: str) -> int:
    clean = symbol.upper().removesuffix(".IS")
    return 5 if clean in {"EURUSD", "GBPUSD", "USDTRY"} else 2


def _timeframe_for_horizon(horizon: ViopHorizon) -> tuple[str, int]:
    if horizon == "gunici":
        return "1h", 120
    return "1h", 260


def _state_direction(state: SymbolTechnicalState) -> str:
    if state.bullish_ten_confluence >= 8 and state.bullish_ten_confluence > state.bearish_ten_confluence:
        return "bullish"
    if state.bearish_ten_confluence >= 8 and state.bearish_ten_confluence > state.bullish_ten_confluence:
        return "bearish"
    return "range"


def _recent_sweep(frame: pd.DataFrame, direction: str, *, lookback: int = 20) -> bool:
    """Detect a completed liquidity grab without claiming one from a vague wick."""

    if len(frame) < lookback + 2:
        return False
    current = frame.iloc[-1]
    history = frame.iloc[-(lookback + 1):-1]
    if direction == "bullish":
        prior_low = float(history["low"].min())
        return float(current["low"]) < prior_low and float(current["close"]) > prior_low
    if direction == "bearish":
        prior_high = float(history["high"].max())
        return float(current["high"]) > prior_high and float(current["close"]) < prior_high
    return False


def _matching_zone(smart: SmartMoneyResult, direction: str, current_price: float):
    candidates = [
        zone
        for zone in (*smart.order_blocks, *smart.fvg)
        if zone.direction == direction and zone.low > 0 and zone.high > 0
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda zone: abs(((float(zone.low) + float(zone.high)) / 2.0) - current_price))


def _price_in_zone(price: float, low: float, high: float) -> bool:
    return min(low, high) <= price <= max(low, high)


def _build_levels(
    *,
    direction: str,
    zone,
    ltf_state: SymbolTechnicalState,
) -> tuple[float, float, float, tuple[float, ...], float] | None:
    """Build a plan from a true zone edge and ATR invalidation, not last close."""

    if zone is None or ltf_state.atr <= 0:
        return None
    low, high = sorted((float(zone.low), float(zone.high)))
    entry = (low + high) / 2.0
    buffer = max(float(ltf_state.atr) * 0.15, abs(entry) * 0.0005)
    if direction == "bullish":
        stop = low - buffer
        risk = entry - stop
        if risk <= 0:
            return None
        targets = (entry + risk * 2.0, entry + risk * 3.0, entry + risk * 4.0)
    else:
        stop = high + buffer
        risk = stop - entry
        if risk <= 0:
            return None
        targets = (entry - risk * 2.0, entry - risk * 3.0, entry - risk * 4.0)
    return low, high, stop, tuple(float(value) for value in targets), 2.0


def build_copilot_scenario(
    *,
    product: CopilotProduct,
    symbol: str,
    horizon: ViopHorizon,
    htf_frame: pd.DataFrame,
    ltf_frame: pd.DataFrame,
    settings,
    news_check: NewsRiskCheck,
    warrant_expiry: date | None = None,
    warrant_delta: float | None = None,
    now: datetime | None = None,
) -> CopilotScenario:
    """Evaluate the non-negotiable six-step setup checklist.

    `valid_setup` is true only when every check passes *and* price has reached
    the calculated zone.  Thus a high-quality zone above/below spot remains a
    pending plan rather than an accidental market-entry instruction.
    """

    if len(htf_frame) < 210 or len(ltf_frame) < 60:
        raise ValueError("VIOP/varant analizi için yeterli kapanmış OHLC mum verisi yok.")
    htf = analyze_symbol_frame(
        symbol, htf_frame, rsi_overbought=settings.rsi_overbought,
        rsi_oversold=settings.rsi_oversold, minimum_confluence=3,
        ten_indicator_minimum=8, timeframe="1d",
    )
    ltf_name = "1h" if horizon == "gunici" else "4h"
    ltf = analyze_symbol_frame(
        symbol, ltf_frame, rsi_overbought=settings.rsi_overbought,
        rsi_oversold=settings.rsi_oversold, minimum_confluence=3,
        ten_indicator_minimum=8, timeframe=ltf_name,
    )
    htf_direction = _state_direction(htf)
    ltf_direction = _state_direction(ltf)
    direction = htf_direction if htf_direction == ltf_direction else "range"
    current = float(ltf.price)
    htf_smart = detect_smart_money(htf_frame)
    ltf_smart = detect_smart_money(ltf_frame)
    htf_zone = _matching_zone(htf_smart, direction, current) if direction != "range" else None
    ltf_zone = _matching_zone(ltf_smart, direction, current) if direction != "range" else None
    zone = ltf_zone or htf_zone
    levels = _build_levels(direction=direction, zone=zone, ltf_state=ltf) if direction != "range" else None
    structure = next((item for item in reversed(ltf_smart.structure) if item.direction == direction), None)
    sweep_confirmed = _recent_sweep(ltf_frame, direction)
    zone_passed = htf_zone is not None and ltf_zone is not None
    core_a_plus = (
        direction != "range"
        and min(
            htf.bullish_ten_confluence if direction == "bullish" else htf.bearish_ten_confluence,
            ltf.bullish_ten_confluence if direction == "bullish" else ltf.bearish_ten_confluence,
        ) >= 8
        and htf.adx >= 20
        and ltf.adx >= 20
    )
    expiry_ok = True
    expiry_detail = ""
    if product == "varant":
        if warrant_expiry is None or warrant_delta is None:
            expiry_ok = False
            expiry_detail = "Varantın son işlem günü ve deltası doğrulanmadı"
        else:
            days_left = (warrant_expiry - (now or datetime.now(timezone.utc)).date()).days
            expiry_ok = days_left >= 14 and 0 < abs(warrant_delta) <= 1
            expiry_detail = f"Vade {days_left} gün · delta {warrant_delta:.2f}"
    rr_passed = bool(levels is not None and levels[-1] >= 2.0)
    checklist = (
        CopilotChecklistItem(
            "daily_bias", "Günlük bias net mi?", direction != "range",
            f"HTF {_state_direction(htf)} · LTF {_state_direction(ltf)}",
        ),
        CopilotChecklistItem(
            "zones", "HTF ve LTF bölgeler çizili mi?", zone_passed,
            "Her iki zaman diliminde yönle uyumlu OB/FVG bulundu" if zone_passed else "HTF/LTF uyumlu çift bölge yok",
        ),
        CopilotChecklistItem(
            "sweep_structure", "Likidite süpürmesi + yapı teyidi var mı?", bool(sweep_confirmed and structure),
            f"Sweep + {structure.kind}" if sweep_confirmed and structure else "Sweep ve BOS/MSS aynı anda doğrulanmadı",
        ),
        CopilotChecklistItem(
            "a_plus", "A+ setup mı?", bool(core_a_plus and expiry_ok),
            expiry_detail or f"10 gösterge uyumu HTF/LTF, ADX {htf.adx:.0f}/{ltf.adx:.0f}",
        ),
        CopilotChecklistItem("news", "Haber riski temiz mi?", news_check.clean, news_check.detail),
        CopilotChecklistItem(
            "rr", "Minimum 1:2 R/R sağlanıyor mu?", rr_passed,
            "R/R 1:2.00" if rr_passed else "Hesaplanabilir en az 1:2 plan yok",
        ),
    )
    entry_low = entry_high = stop = None
    targets: tuple[float, ...] = ()
    rr = None
    if levels is not None:
        entry_low, entry_high, stop, targets, rr = levels
    pending = bool(entry_low is not None and not _price_in_zone(current, entry_low, entry_high))
    valid_setup = all(item.passed for item in checklist) and not pending
    missing = [item.label for item in checklist if not item.passed]
    if direction == "range":
        bias = "YATAY"
    else:
        bias = "YÜKSELİŞ" if direction == "bullish" else "DÜŞÜŞ"
    if valid_setup:
        note = "A+ teyit tamam. Giriş yalnızca bölge içinde, kapanmış LTF mum teyidiyle değerlendirilir."
    elif pending and not missing:
        note = "PENDING: Fiyat giriş bölgesinde değil; güncel fiyattan işlem yok, retest beklenir."
    else:
        note = "BEKLE — Eksik kontrol: " + ", ".join(missing or ["giriş bölgesine retest"])
    return CopilotScenario(
        product=product, symbol=symbol.upper().removesuffix(".IS"), horizon=horizon,
        generated_at=(now or datetime.now(timezone.utc)), bias=bias, current_price=current,
        entry_low=entry_low, entry_high=entry_high, stop=stop, targets=targets, rr=rr,
        pending=pending, valid_setup=valid_setup, checklist=checklist, note=note, htf=htf, ltf=ltf,
    )


def analyze_live_copilot(
    *,
    product: CopilotProduct,
    symbol: str,
    horizon: ViopHorizon,
    provider: BaseMarketDataProvider,
    settings,
    news_check: NewsRiskCheck,
    warrant_expiry: date | None = None,
    warrant_delta: float | None = None,
    now: datetime | None = None,
) -> CopilotScenario:
    """Fetch enough completed source bars then delegate all decision logic."""

    end = now or datetime.now(timezone.utc)
    htf = provider.get_ohlcv(symbol, "1d", end - timedelta(days=540), end)
    timeframe, days = _timeframe_for_horizon(horizon)
    ltf = provider.get_ohlcv(symbol, timeframe, end - timedelta(days=days), end)
    return build_copilot_scenario(
        product=product, symbol=symbol, horizon=horizon, htf_frame=htf, ltf_frame=ltf,
        settings=settings, news_check=news_check, warrant_expiry=warrant_expiry,
        warrant_delta=warrant_delta, now=end,
    )


def format_copilot_scenario(scenario: CopilotScenario, *, timezone_name: str) -> str:
    """Render the mandatory compact Borsa Copilot response template."""

    decimals = _price_decimals(scenario.symbol)
    timestamp = scenario.generated_at.astimezone(ZoneInfo(timezone_name)).strftime("%d.%m.%Y %H:%M")
    product = "VİOP SPOT DAYANAK" if scenario.product == "viop" else "VARANT DAYANAK"
    if scenario.entry_low is None or scenario.entry_high is None:
        entry = stop = targets = "—"
    else:
        entry = f"{scenario.entry_low:.{decimals}f} - {scenario.entry_high:.{decimals}f}"
        stop = f"{scenario.stop:.{decimals}f}" if scenario.stop is not None else "—"
        targets = "  |  ".join(
            f"TP{index}: {value:.{decimals}f}" for index, value in enumerate(scenario.targets[:3], start=1)
        ) or "—"
    rr = f"1:{scenario.rr:.2f}" if scenario.rr is not None else "GEÇERSİZ SETUP"
    checks = "\n".join(
        f"{'✅' if item.passed else '❌'} {index}) {item.label} — {item.detail}"
        for index, item in enumerate(scenario.checklist, start=1)
    )
    status = "A+ SENARYO" if scenario.valid_setup else "PENDING / BEKLE" if scenario.pending else "BEKLE"
    return (
        f"📊 {scenario.symbol} · {product} — {timestamp}\n"
        f"DURUM: {status}\n"
        f"BIAS: {scenario.bias} (HTF Günlük · LTF {'1S' if scenario.horizon == 'gunici' else '4S'})\n"
        f"GÜNCEL SPOT: {scenario.current_price:.{decimals}f}\n"
        f"GİRİŞ BÖLGESİ: {entry}\n"
        f"STOP: {stop}\n"
        f"{targets}\n"
        f"R/R: {rr}\n\n"
        f"6 ADIM:\n{checks}\n\n"
        f"NOT: {scenario.note}\n\n"
        "Risk: İşlem başına en fazla sermayenin %1'i; art arda 2 kayıptan sonra %0,5. "
        "VİOP/varant kaldıraçlıdır; teminat veya yatırılan tutarın tamamı risk altındadır. "
        "Bu yatırım tavsiyesi değil, doğrulanabilir veriyle üretilen koşullu senaryodur."
    )[:4096]
