from __future__ import annotations

"""Aşama 5e komutları için tek-fetch ortak analiz bağlamı."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from app.analysis.confluence_zone_engine import find_confluence_zones
from app.analysis.corporate_actions_engine import CorporateActionEvent, normalize_corporate_actions
from app.analysis.data_quality import DataQualityEngine, DataQualityResult
from app.analysis.gyo_valuation_engine import GYOValuationResult, collect_fundamental_payload, evaluate_gyo_valuation
from app.analysis.liquidity_engine import LiquidityResult, compute_liquidity
from app.analysis.long_term_scenario_engine import LongTermScenarioResult, compute_long_term_scenarios
from app.analysis.market_regime_engine import MarketRegimeResult, classify_market_regime_from_df
from app.analysis.relative_strength_engine import RelativeStrengthResult, compute_relative_strength
from app.analysis.timeframe_levels_engine import MultiTimeframeLevelsResult, compute_timeframe_levels
from app.config.settings import Settings
from app.data.base_provider import BaseMarketDataProvider, DataUnavailableError
from app.data.provider_factory import build_fundamental_provider
from app.services.current_price_service import CurrentPriceResult, resolve_current_price
from app.services.sector_service import get_sector_info


@dataclass
class Stage5EAnalysisContext:
    symbol: str
    daily_df: pd.DataFrame
    completed_daily_df: pd.DataFrame
    current_price: CurrentPriceResult
    data_quality: DataQualityResult
    levels: MultiTimeframeLevelsResult
    confluence_supports: list
    confluence_resistances: list
    liquidity: LiquidityResult
    long_term_scenarios: LongTermScenarioResult
    valuation: GYOValuationResult
    xu100_relative_strength: RelativeStrengthResult
    sector_relative_strength: Optional[RelativeStrengthResult]
    market_regime: MarketRegimeResult
    corporate_actions: list[CorporateActionEvent]
    data_timestamp: datetime

    def intermediate_levels(self) -> list:
        candidates = list(self.levels.all_zones())
        candidates.extend(self.long_term_scenarios.all_scenarios())
        return candidates

    def support_levels(self) -> list:
        current = self.current_price.current_price or 0.0
        return [item for item in self.levels.all_zones() if item.mid <= current]


class Stage5EContextUnavailableError(Exception):
    pass


def build_stage5e_analysis_context(
    provider: BaseMarketDataProvider,
    symbol: str,
    settings: Settings,
    *,
    now: Optional[datetime] = None,
) -> Stage5EAnalysisContext:
    symbol = symbol.strip().upper()
    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    try:
        daily = provider.get_ohlcv(symbol, "1d", end - timedelta(days=365 * 8), end)
    except DataUnavailableError as exc:
        raise Stage5EContextUnavailableError(f"{symbol} için günlük veri alınamadı: {exc}") from exc
    quality_engine = DataQualityEngine()
    completed = quality_engine.completed_candles(daily, "1d", now=end)
    if completed is None or len(completed) < 60:
        raise Stage5EContextUnavailableError("Uzun vadeli analiz için en az 60 tamamlanmış günlük mum gerekli.")
    metadata = provider.metadata_for(symbol, "1d") if hasattr(provider, "metadata_for") else {}
    metadata = metadata or {}
    quality = quality_engine.evaluate(
        completed, symbol=symbol, timeframe="1d", min_bars=60,
        provider=metadata.get("provider", getattr(provider, "name", "unknown")),
        fallback_used=bool(metadata.get("fallback_used")), cache_used=bool(metadata.get("cache_used")),
        cache_age_minutes=metadata.get("cache_age_minutes"), price_mode=settings.price_adjustment_mode,
        now=end,
    )
    if not quality.usable_for_analysis:
        raise Stage5EContextUnavailableError(
            f"Veri kalitesi uzun vadeli analiz için uygun değil: {quality.status.value} ({quality.score}/100)."
        )
    price = resolve_current_price(
        provider, symbol, now=end, daily_df=daily, timezone_name=settings.timezone_name
    )
    if price.current_price is None or price.analysis_close is None:
        raise Stage5EContextUnavailableError("Güncel fiyat veya kesinleşmiş kapanış bulunamadı.")
    # Seviyeler yalnızca kesinleşmiş kapanış serisiyle; uzaklık ve hedef
    # hesapları güncel işlem fiyatıyla yapılır.
    levels = compute_timeframe_levels(completed, price.analysis_close)
    supports, resistances = find_confluence_zones(levels, price.current_price)
    liquidity = compute_liquidity(completed)
    try:
        actions_raw = provider.get_corporate_actions(symbol)
    except Exception:  # noqa: BLE001
        actions_raw = []
    actions = normalize_corporate_actions(symbol, actions_raw)

    # Temel veri yoksa nötr kalır; pozitif destek uydurulmaz.
    fundamental_provider = build_fundamental_provider(settings)
    fundamental_payload = collect_fundamental_payload(fundamental_provider, symbol)
    sector_info = get_sector_info(symbol)
    valuation = evaluate_gyo_valuation(
        symbol,
        price.current_price,
        fundamental_payload,
        sector_name=sector_info.sector_name if sector_info else None,
    )

    unavailable_rs = RelativeStrengthResult(False, "Karşılaştırma verisi bulunamadı.")
    xu100_rs = unavailable_rs
    sector_rs: Optional[RelativeStrengthResult] = None
    market_regime = MarketRegimeResult("veri_yetersiz", settings.xu100_symbol, None, "Endeks verisi bulunamadı.")
    benchmark_start = end - timedelta(days=365 * 3)
    try:
        xu100_df = provider.get_ohlcv(settings.xu100_symbol, "1d", benchmark_start, end)
        xu100_rs = compute_relative_strength(completed, xu100_df)
        market_regime = classify_market_regime_from_df(xu100_df, settings.xu100_symbol, "1d")
    except (DataUnavailableError, ValueError, TypeError):
        pass
    if sector_info and sector_info.sector_index and sector_info.sector_index != settings.xu100_symbol:
        try:
            sector_df = provider.get_ohlcv(sector_info.sector_index, "1d", benchmark_start, end)
            sector_rs = compute_relative_strength(completed, sector_df)
        except (DataUnavailableError, ValueError, TypeError):
            sector_rs = RelativeStrengthResult(False, "Sektör karşılaştırma verisi bulunamadı.")

    long_term = compute_long_term_scenarios(
        completed,
        price.current_price,
        levels_result=levels,
        liquidity_score=liquidity.score if liquidity.available else None,
        market_context={
            "xu100_trend": market_regime.regime,
            "sector_trend": getattr(sector_rs, "classification", None),
        },
        valuation_result=valuation,
        relative_strength=xu100_rs,
        sector_relative_strength=sector_rs,
        market_regime=market_regime,
        data_quality_score=quality.score,
        shares_outstanding=valuation.shares_outstanding,
        current_market_cap=valuation.current_market_cap,
        manipulation_risk=liquidity.manipulation_risk if liquidity.available else False,
    )
    last_timestamp = pd.Timestamp(completed.iloc[-1]["timestamp"])
    if last_timestamp.tzinfo is None:
        last_timestamp = last_timestamp.tz_localize("UTC")
    return Stage5EAnalysisContext(
        symbol, daily, completed, price, quality, levels, supports, resistances,
        liquidity, long_term, valuation, xu100_rs, sector_rs, market_regime,
        actions, last_timestamp.to_pydatetime(),
    )
