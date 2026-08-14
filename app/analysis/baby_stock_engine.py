from __future__ import annotations

"""Strict, retest-first BIST spot scanner for the ``/bebekhisse`` command.

This is deliberately a *selection* tool, not a prediction engine.  A share is
only returned when the whole-universe hourly pass, daily trend, 15-minute
retest plan, 5-minute timing and daily liquidity checks all agree.  If nothing
passes, the caller receives an empty result instead of invented "top two"
names.
"""

from copy import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import floor
from typing import Callable, Iterable

import pandas as pd

from app.analysis.liquidity_engine import LiquidityResult, compute_liquidity
from app.analysis.screener_engine import (
    SymbolTechnicalState,
    TradeScenario,
    analyze_symbol_frame,
    build_trade_scenario,
    run_market_opportunity_scan,
)
from app.data.base_provider import BaseMarketDataProvider


@dataclass(frozen=True)
class BabyStockRiskProfile:
    capital: float
    risk_per_trade_percent: float
    daily_loss_limit_percent: float
    max_open_positions: int
    max_position_percent: float
    no_overnight: bool


@dataclass(frozen=True)
class BabyStockPositionPlan:
    entry_reference: float
    maximum_units: int
    maximum_position_value: float
    risk_budget: float
    planned_risk: float
    daily_loss_limit: float


@dataclass(frozen=True)
class BabyStockCandidate:
    symbol: str
    quality_score: int
    hourly: SymbolTechnicalState
    daily: SymbolTechnicalState
    minute_15: SymbolTechnicalState
    minute_5: SymbolTechnicalState
    scenario: TradeScenario
    liquidity: LiquidityResult
    fundamental_score: int | None
    fundamental_status: str
    fundamental_source: str | None
    position_plan: BabyStockPositionPlan
    reasons: tuple[str, ...]
    risk_notes: tuple[str, ...]


@dataclass(frozen=True)
class BabyStockReport:
    scanned: int
    failed: int
    candidates: tuple[BabyStockCandidate, ...]
    created_at: datetime
    risk_profile: BabyStockRiskProfile
    shortlist_size: int
    requested_symbol: str | None = None


def risk_profile_from_settings(settings, *, capital: float | None = None) -> BabyStockRiskProfile:
    """Build an explicit, bounded spot-trading risk profile from settings."""

    selected_capital = float(
        capital if capital is not None else getattr(settings, "baby_stock_default_capital", 200_000.0)
    )
    if selected_capital <= 0:
        raise ValueError("Sermaye sıfırdan büyük olmalı.")
    return BabyStockRiskProfile(
        capital=selected_capital,
        risk_per_trade_percent=float(getattr(settings, "baby_stock_risk_per_trade_percent", 0.5)),
        daily_loss_limit_percent=float(getattr(settings, "baby_stock_daily_loss_limit_percent", 1.5)),
        max_open_positions=int(getattr(settings, "baby_stock_max_open_positions", 2)),
        max_position_percent=float(getattr(settings, "baby_stock_max_position_percent", 20.0)),
        no_overnight=bool(getattr(settings, "baby_stock_no_overnight", True)),
    )


def calculate_baby_position_plan(
    profile: BabyStockRiskProfile,
    *,
    entry_low: float,
    entry_high: float,
    stop: float,
) -> BabyStockPositionPlan:
    """Size a spot position by risk and notional caps, whichever is smaller.

    The calculation never assumes leverage and intentionally rounds down to a
    whole share.  A zero size means the risk plan cannot fund even one share.
    """

    entry = (float(entry_low) + float(entry_high)) / 2
    risk_per_share = abs(entry - float(stop))
    if entry <= 0 or risk_per_share <= 0:
        raise ValueError("Giriş ve stop seviyeleri pozitif ve farklı olmalı.")
    risk_budget = profile.capital * profile.risk_per_trade_percent / 100
    position_cap = profile.capital * profile.max_position_percent / 100
    units_by_risk = floor(risk_budget / risk_per_share)
    units_by_value = floor(position_cap / entry)
    units = max(0, min(units_by_risk, units_by_value))
    return BabyStockPositionPlan(
        entry_reference=entry,
        maximum_units=units,
        maximum_position_value=units * entry,
        risk_budget=risk_budget,
        planned_risk=units * risk_per_share,
        daily_loss_limit=profile.capital * profile.daily_loss_limit_percent / 100,
    )


def _start_for_timeframe(timeframe: str, end: datetime) -> datetime:
    days = {"5m": 58, "15m": 58, "1h": 120, "1d": 540}[timeframe]
    return end - timedelta(days=days)


def _state_for_frame(symbol: str, frame: pd.DataFrame, timeframe: str, settings) -> SymbolTechnicalState:
    if frame is None or frame.empty:
        raise ValueError(f"{timeframe} mum verisi boş")
    return analyze_symbol_frame(
        symbol,
        frame,
        rsi_overbought=settings.rsi_overbought,
        rsi_oversold=settings.rsi_oversold,
        minimum_confluence=settings.technical_screener_min_confluence,
        ten_indicator_minimum=3,
        timeframe=timeframe,
    )


def _is_bullish_daily(state: SymbolTechnicalState) -> bool:
    return (
        state.price > state.ema50
        and state.ema20 > state.ema50
        and state.supertrend_direction == "up"
        and 45 <= state.rsi <= 72
        and state.macd_histogram > 0
    )


def _quality_score(
    *,
    hourly: SymbolTechnicalState,
    minute_15: SymbolTechnicalState,
    minute_5: SymbolTechnicalState,
    scenario: TradeScenario,
    liquidity: LiquidityResult,
    fundamental_score: int | None,
) -> int:
    raw = (
        28
        + hourly.bullish_ten_confluence * 3
        + minute_15.bullish_ten_confluence * 3
        + minute_5.bullish_ten_confluence * 2
        + scenario.core_confirmation_count * 4
        + min(liquidity.score, 100) * 0.18
        + min(hourly.adx, 35) * 0.18
        + (min(fundamental_score, 100) * 0.08 if fundamental_score is not None else 0)
    )
    return max(0, min(99, round(raw)))


def _candidate_reasons(
    hourly: SymbolTechnicalState,
    minute_15: SymbolTechnicalState,
    minute_5: SymbolTechnicalState,
    scenario: TradeScenario,
    liquidity: LiquidityResult,
) -> tuple[str, ...]:
    reasons = [
        f"1s: {hourly.bullish_ten_confluence}/10 teyit • Supertrend yukarı • ADX {hourly.adx:.0f}",
        f"15dk: {minute_15.bullish_ten_confluence}/10 teyit • {', '.join(scenario.ten_confirmation_labels[:3])}",
        f"5dk zamanlama: {minute_5.bullish_ten_confluence}/10 teyit • RSI {minute_5.rsi:.0f}",
        f"Likidite: {liquidity.score:.0f}/100 • 20g ortalama işlem tutarı doğrulandı",
    ]
    if scenario.confirmed_patterns:
        reasons.append(f"Teyitli formasyon: {scenario.confirmed_patterns[0].name}")
    return tuple(reasons)


def _candidate_risk_notes(
    hourly: SymbolTechnicalState,
    minute_15: SymbolTechnicalState,
    liquidity: LiquidityResult,
) -> tuple[str, ...]:
    notes = [
        "Giriş yalnızca belirtilen retest bölgesinde 15dk yeşil kapanış ve hacim teyidiyle geçerlidir.",
        "KAP/haber akışı bu teknik taramada otomatik alım gerekçesi değildir; emir öncesi resmî bildirim kontrol edilmelidir.",
    ]
    if liquidity.risk_note:
        notes.append(liquidity.risk_note)
    if minute_15.rsi >= 68:
        notes.append(f"15dk RSI {minute_15.rsi:.0f}: fiyat kovalanmamalı, sadece bölge retesti izlenmeli.")
    if hourly.relative_volume < 1:
        notes.append("Saatlik hacim ortalama altında; hacim artışı olmadan teyit zayıflar.")
    return tuple(notes)


def _fundamental_check(provider: object | None, symbol: str) -> tuple[int | None, str, str | None, bool]:
    """Return only provider-backed company quality; never fill missing data.

    The boolean says whether the source delivered enough evidence to apply the
    optional financial-health gate.  A disabled or temporarily unavailable
    source cannot silently turn into a negative company assessment.
    """

    if provider is None:
        return None, "DOĞRULANMADI", None, False
    try:
        from app.services.company_analysis_service import analyze_company

        analysis = analyze_company(symbol, fundamental_provider=provider)
    except Exception:
        return None, "DOĞRULANMADI", None, False
    coverage = int(getattr(analysis, "data_coverage", 0) or 0)
    score = int(getattr(analysis, "score", 0) or 0)
    if coverage < 60:
        return None, "VERİ YETERSİZ", str(getattr(analysis, "source", "") or None), False
    return score, str(getattr(analysis, "status", "DOĞRULANMADI")), str(getattr(analysis, "source", "") or None), True


def run_baby_stock_scan(
    *,
    symbols: Iterable[str],
    provider_factory: Callable[[], BaseMarketDataProvider],
    settings,
    capital: float | None = None,
    requested_symbol: str | None = None,
    fundamental_provider_factory: Callable[[], object] | None = None,
) -> BabyStockReport:
    """Return at most two confirmed spot-long plans, or an empty honest report.

    The initial pass remains batch-friendly (one hourly request per BIST share).
    Only its small bullish shortlist receives the more expensive 1d/15m/5m
    multi-timeframe validation.  This prevents a 571 × four-timeframe request
    burst while retaining strict multi-timeframe confirmation for every result.
    """

    profile = risk_profile_from_settings(settings, capital=capital)
    normalized = list(dict.fromkeys(str(item).upper().removesuffix(".IS") for item in symbols))
    if requested_symbol:
        normalized = [requested_symbol.upper().removesuffix(".IS")]
    if not normalized:
        raise ValueError("Taranacak BIST sembolü bulunamadı.")

    hourly_min = max(6, min(10, int(getattr(settings, "baby_stock_hourly_min_confirmations", 8))))
    if hasattr(settings, "model_copy"):
        scan_settings = settings.model_copy(
            update={
                "market_opportunity_minimum_confluence": hourly_min,
                "market_opportunity_max_results": int(getattr(settings, "baby_stock_shortlist_size", 12)),
            }
        )
    else:
        # Keeps this analysis module easy to exercise with a small test settings
        # object, while production uses Pydantic's immutable copy path above.
        scan_settings = copy(settings)
        scan_settings.market_opportunity_minimum_confluence = hourly_min
        scan_settings.market_opportunity_max_results = int(getattr(settings, "baby_stock_shortlist_size", 12))
    hourly_report = run_market_opportunity_scan(
        symbols=normalized,
        provider_factory=provider_factory,
        settings=scan_settings,
        timeframe="1h",
    )
    shortlist_by_symbol: dict[str, SymbolTechnicalState] = {}
    for state in (*hourly_report.al_sat_uygun, *hourly_report.kisa_vade):
        if state.bullish_ten_confluence >= hourly_min and state.supertrend_direction == "up":
            shortlist_by_symbol[state.symbol] = state
    shortlist = sorted(
        shortlist_by_symbol.values(),
        key=lambda item: (item.bullish_ten_confluence, item.adx, item.relative_volume),
        reverse=True,
    )[: int(getattr(settings, "baby_stock_shortlist_size", 12))]

    minimum_15 = max(5, min(10, int(getattr(settings, "baby_stock_15m_min_confirmations", 7))))
    minimum_5 = max(4, min(10, int(getattr(settings, "baby_stock_5m_min_confirmations", 6))))
    minimum_liquidity = float(getattr(settings, "baby_stock_min_liquidity_score", 60.0))
    now = datetime.now(timezone.utc)
    provider = provider_factory()
    try:
        fundamental_provider = fundamental_provider_factory() if fundamental_provider_factory else None
    except Exception:
        fundamental_provider = None
    require_fundamental = bool(getattr(settings, "baby_stock_require_fundamental", False))
    minimum_fundamental = int(getattr(settings, "baby_stock_minimum_fundamental_score", 65))
    candidates: list[BabyStockCandidate] = []
    for hourly in shortlist:
        try:
            frame_1d = provider.get_ohlcv(hourly.symbol, "1d", _start_for_timeframe("1d", now), now)
            frame_15 = provider.get_ohlcv(hourly.symbol, "15m", _start_for_timeframe("15m", now), now)
            frame_5 = provider.get_ohlcv(hourly.symbol, "5m", _start_for_timeframe("5m", now), now)
            daily = _state_for_frame(hourly.symbol, frame_1d, "1d", settings)
            minute_15 = _state_for_frame(hourly.symbol, frame_15, "15m", settings)
            minute_5 = _state_for_frame(hourly.symbol, frame_5, "5m", settings)
            liquidity = compute_liquidity(
                frame_1d,
                {
                    "strong_signal_minimum_score": minimum_liquidity,
                    "maximum_atr_percent": float(getattr(settings, "maximum_atr_percent", 12.0)),
                },
            )
            scenario = build_trade_scenario(
                minute_15,
                minimum_core_confirmations=4,
                minimum_ten_confirmations=minimum_15,
            )
            if (
                not _is_bullish_daily(daily)
                or minute_15.bullish_ten_confluence < minimum_15
                or minute_15.supertrend_direction != "up"
                or minute_5.bullish_ten_confluence < minimum_5
                or minute_5.supertrend_direction != "up"
                or scenario is None
                or scenario.action != "AL"
                or scenario.direction != "bullish"
                or scenario.rr < 2
                or not liquidity.available
                or liquidity.score < minimum_liquidity
                or not liquidity.allow_strong_signal
                or liquidity.manipulation_risk
            ):
                continue
            fundamental_score, fundamental_status, fundamental_source, fundamental_verified = _fundamental_check(
                fundamental_provider, hourly.symbol
            )
            # If a source did verify weak finances, do not call the company
            # "strong" just because the chart is strong.  Operators may also
            # demand a configured and verified source for every candidate.
            if fundamental_verified and fundamental_score is not None and fundamental_score < minimum_fundamental:
                continue
            if require_fundamental and not fundamental_verified:
                continue
            plan = calculate_baby_position_plan(
                profile,
                entry_low=scenario.entry_low,
                entry_high=scenario.entry_high,
                stop=scenario.stop,
            )
            if plan.maximum_units < 1:
                continue
            candidates.append(
                BabyStockCandidate(
                    symbol=hourly.symbol,
                    quality_score=_quality_score(
                        hourly=hourly,
                        minute_15=minute_15,
                        minute_5=minute_5,
                        scenario=scenario,
                        liquidity=liquidity,
                        fundamental_score=fundamental_score,
                    ),
                    hourly=hourly,
                    daily=daily,
                    minute_15=minute_15,
                    minute_5=minute_5,
                    scenario=scenario,
                    liquidity=liquidity,
                    fundamental_score=fundamental_score,
                    fundamental_status=fundamental_status,
                    fundamental_source=fundamental_source,
                    position_plan=plan,
                    reasons=_candidate_reasons(hourly, minute_15, minute_5, scenario, liquidity),
                    risk_notes=_candidate_risk_notes(hourly, minute_15, liquidity),
                )
            )
        except Exception:  # one bad sub-timeframe can never create a guessed candidate
            continue

    candidates.sort(key=lambda item: (-item.quality_score, -item.scenario.rr, item.symbol))
    maximum = min(2, max(1, int(getattr(settings, "baby_stock_max_candidates", 2))))
    return BabyStockReport(
        scanned=hourly_report.scanned,
        failed=hourly_report.failed,
        candidates=tuple(candidates[:maximum]),
        created_at=datetime.now(timezone.utc),
        risk_profile=profile,
        shortlist_size=len(shortlist),
        requested_symbol=requested_symbol.upper().removesuffix(".IS") if requested_symbol else None,
    )


def _price(value: float) -> str:
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _money(value: float) -> str:
    return f"{value:,.0f}".replace(",", ".") + " TL"


def format_baby_stock_report(report: BabyStockReport, *, timezone_name: str = "Europe/Istanbul") -> str:
    from zoneinfo import ZoneInfo

    local = report.created_at.astimezone(ZoneInfo(timezone_name))
    profile = report.risk_profile
    title = "TEK HİSSE PROFESYONEL KONTROL" if report.requested_symbol else "BEBEK HİSSE • PRO GÜN İÇİ RADARI"
    lines = [
        f"🧸 {title}",
        f"{local:%d.%m.%Y %H:%M} TSİ • {report.scanned} hisse tarandı • saatlik kısa liste: {report.shortlist_size}",
        "",
        "🛡 RİSK ÇERÇEVESİ",
        f"• Sermaye: {_money(profile.capital)} • İşlem başı azami risk: %{profile.risk_per_trade_percent:.1f}",
        f"• Günlük zarar freni: %{profile.daily_loss_limit_percent:.1f} ({_money(profile.capital * profile.daily_loss_limit_percent / 100)})",
        f"• Eşzamanlı en fazla {profile.max_open_positions} pozisyon • işlem başı azami pozisyon: %{profile.max_position_percent:.0f}",
        f"• {'Gece pozisyonu taşınmaz.' if profile.no_overnight else 'Gece taşıma ayarı açık; haber/gap riski ayrıca değerlendirilmelidir.'}",
        "",
    ]
    if not report.candidates:
        lines.extend(
            [
                "⏸ BUGÜN ONAYLI ADAY YOK",
                "Saatlik evren filtresi sonrası günlük trend, 15dk retest, 5dk zamanlama, en az 1:2 R/R ve likidite kapılarının tamamını geçen aday bulunmadı.",
                "Zorla iki hisse yazılmadı. Yeni kapanışlar oluşunca /bebekhisse komutunu tekrar çalıştır.",
                "",
                "Not: Bu ekran yalnız BIST spot LONG senaryosudur; açığa satış/VİOP işlemi değildir.",
            ]
        )
        return "\n".join(lines)[:4096]

    lines.append("Yalnız bölge içinde 15dk teyit alan, spot LONG için uygun görünen adaylar:")
    for index, item in enumerate(report.candidates, start=1):
        scenario = item.scenario
        plan = item.position_plan
        pattern = f" • Formasyon: {scenario.confirmed_patterns[0].name}" if scenario.confirmed_patterns else ""
        lines.extend(
            [
                "",
                f"{index}. 🟢 {item.symbol} • KALİTE {item.quality_score}/100{pattern}",
                f"Son fiyat: {_price(scenario.price)} TL • Bu fiyat doğrudan giriş değildir.",
                f"🎯 Giriş bölgesi: {_price(scenario.entry_low)} – {_price(scenario.entry_high)} TL",
                f"🛑 Geçersizlik/stop: {_price(scenario.stop)} TL",
                f"🚀 TP1: {_price(scenario.tp1)} TL • TP2: {_price(scenario.tp2)} TL • R/R: 1:{scenario.rr:.2f}",
                (
                    f"🏢 Temel kontrol: {item.fundamental_status} • {item.fundamental_score}/100"
                    if item.fundamental_score is not None
                    else "🏢 Temel kontrol: doğrulanamadı; ‘sağlam firma’ etiketi kullanılmadı."
                ),
                *( [f"  ▸ Kaynak: {item.fundamental_source[:100]}"] if item.fundamental_source else [] ),
                "✅ Neden izleniyor:",
                *[f"  ▸ {reason}" for reason in item.reasons],
                "🧮 200 bin TL tarzı örnek plan:" if profile.capital == 200_000 else "🧮 Seçilen sermayeye göre plan:",
                f"  ▸ En fazla {plan.maximum_units} lot • yaklaşık {_money(plan.maximum_position_value)} pozisyon",
                f"  ▸ Planlanan stop riski: {_money(plan.planned_risk)} (üst sınır: {_money(plan.risk_budget)})",
                "⚠️ İşleme girmeden önce:",
                *[f"  ▸ {note}" for note in item.risk_notes],
            ]
        )
    lines.extend(
        [
            "",
            "Kural: İlk hedefe giderken kârın bir kısmı alınabilir; stop teknik geçersizlikten yukarı taşınmaz.",
            "Bu koşullu teknik taramadır, kişiye özel yatırım tavsiyesi değildir. Emirden önce fiyatı, KAP’ı ve güncel derinliği doğrula.",
        ]
    )
    return "\n".join(lines)[:4096]


def format_baby_stock_settings(profile: BabyStockRiskProfile) -> str:
    return "\n".join(
        [
            "🧸 BEBEK HİSSE • RİSK AYARLARI",
            f"• Varsayılan sermaye: {_money(profile.capital)}",
            f"• İşlem başı risk: %{profile.risk_per_trade_percent:.1f}",
            f"• Günlük toplam zarar freni: %{profile.daily_loss_limit_percent:.1f} ({_money(profile.capital * profile.daily_loss_limit_percent / 100)})",
            f"• En fazla açık pozisyon: {profile.max_open_positions}",
            f"• Bir pozisyonda azami sermaye: %{profile.max_position_percent:.0f}",
            f"• Gece taşıma: {'kapalı' if profile.no_overnight else 'açık'}",
            "",
            "Kullanım: /bebekhisse 200000 • Tek hisse denetimi: /bebekhisse_kontrol THYAO 200000",
            "Aday, günlük trend + 1s + 15dk + 5dk + likidite + en az 1:2 R/R kapılarının tümünü geçmeden listelenmez.",
        ]
    )
