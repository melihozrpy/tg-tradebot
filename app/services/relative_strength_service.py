from __future__ import annotations

"""MERGEN QUANT - Asama 5c: relative_strength_periods servis katmani.

`/guc SEMBOL` komutu ve /analiz akisindaki gelismis goreceli guc gorunumu
bu servisi kullanir. Sonuclar `relative_strength_periods` tablosuna
kaydedilir (her cagrida guncel bir kayit seti eklenir; gecmis kayitlar
SILINMEZ, boylece zaman icindeki degisim izlenebilir).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.analysis.relative_strength_periods_engine import (
    BENCHMARK_SECTOR,
    BENCHMARK_XU100,
    RelativeStrengthPeriodsResult,
    compute_relative_strength_periods,
)
from app.analysis.data_quality import DataQualityEngine, DataQualityResult
from app.config.settings import Settings, get_strategy_config
from app.data.base_provider import BaseMarketDataProvider, DataUnavailableError
from app.models.database import RelativeStrengthPeriod
from app.services.sector_service import get_sector_info

logger = logging.getLogger("mergen_quant.relative_strength")


@dataclass
class SymbolRelativeStrength:
    symbol: str
    xu100: RelativeStrengthPeriodsResult
    sector: Optional[RelativeStrengthPeriodsResult]
    sector_name: Optional[str]
    data_quality: Optional[DataQualityResult] = None


def compute_symbol_relative_strength(
    provider: BaseMarketDataProvider, symbol: str, settings: Settings
) -> SymbolRelativeStrength:
    """XU100 ve (varsa) sektor endeksine gore donemsel goreceli gucu hesaplar.
    Hesaplama sirasinda veri alinamayan taraf icin sahte deger URETILMEZ;
    ilgili benchmark 'veri yetersiz' olarak isaretlenir.
    """
    strategy_config = get_strategy_config()
    timeframe = strategy_config["timeframes"]["primary"]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)  # ~120 islem gunu + tampon

    try:
        stock_df = provider.get_ohlcv(symbol, timeframe, start, end)
    except DataUnavailableError as exc:
        raise DataUnavailableError(f"'{symbol}' icin veri alinamadi: {exc}") from exc

    quality_engine = DataQualityEngine()
    stock_df = quality_engine.completed_candles(stock_df, timeframe, now=end)
    metadata = provider.metadata_for(symbol, timeframe) if hasattr(provider, "metadata_for") else {}
    metadata = metadata or {}
    data_quality = quality_engine.evaluate(
        stock_df,
        symbol=symbol,
        timeframe=timeframe,
        min_bars=65,
        provider=metadata.get("provider", getattr(provider, "name", "unknown")),
        fallback_used=bool(metadata.get("fallback_used")),
        cache_used=bool(metadata.get("cache_used")),
        cache_age_minutes=metadata.get("cache_age_minutes"),
        now=end,
    )
    if not data_quality.usable_for_analysis:
        raise DataUnavailableError(
            f"'{symbol}' veri kalitesi göreceli güç analizi için uygun değil: {data_quality.status.value}."
        )

    try:
        xu100_df = provider.get_ohlcv(settings.xu100_symbol, timeframe, start, end)
    except DataUnavailableError as exc:
        logger.warning("XU100 verisi alinamadi: %s", exc)
        xu100_df = None
    if xu100_df is not None:
        xu100_df = quality_engine.completed_candles(xu100_df, timeframe, now=end)
        xu_quality = quality_engine.evaluate(
            xu100_df, symbol=settings.xu100_symbol, timeframe=timeframe,
            min_bars=65, provider=getattr(provider, "name", "unknown"), now=end,
        )
        if not xu_quality.usable_for_analysis:
            logger.warning("XU100 veri kalitesi yetersiz: %s", xu_quality.status.value)
            xu100_df = None

    xu100_result = compute_relative_strength_periods(
        stock_df, xu100_df, symbol, BENCHMARK_XU100, settings.xu100_symbol
    )

    sector_info = get_sector_info(symbol)
    sector_result: Optional[RelativeStrengthPeriodsResult] = None
    sector_name: Optional[str] = None
    if sector_info is not None and sector_info.sector_index:
        sector_name = sector_info.sector_name
        try:
            sector_df = provider.get_ohlcv(sector_info.sector_index, timeframe, start, end)
        except DataUnavailableError as exc:
            logger.warning("Sektor endeksi verisi alinamadi symbol=%s: %s", symbol, exc)
            sector_df = None
        if sector_df is not None:
            sector_df = quality_engine.completed_candles(sector_df, timeframe, now=end)
            sector_quality = quality_engine.evaluate(
                sector_df, symbol=sector_info.sector_index, timeframe=timeframe,
                min_bars=65, provider=getattr(provider, "name", "unknown"), now=end,
            )
            if not sector_quality.usable_for_analysis:
                logger.warning("Sektor veri kalitesi yetersiz symbol=%s", symbol)
                sector_df = None
        sector_result = compute_relative_strength_periods(
            stock_df, sector_df, symbol, BENCHMARK_SECTOR, sector_info.sector_index
        )

    return SymbolRelativeStrength(
        symbol=symbol, xu100=xu100_result, sector=sector_result,
        sector_name=sector_name, data_quality=data_quality,
    )


def persist_relative_strength(db: Session, result: SymbolRelativeStrength) -> int:
    """Hesaplanan donemsel sonuclari relative_strength_periods tablosuna kaydeder.
    Doner: eklenen satir sayisi.
    """
    added = 0
    for periods_result in filter(None, [result.xu100, result.sector]):
        for period_strength in periods_result.periods.values():
            db.add(
                RelativeStrengthPeriod(
                    symbol=result.symbol,
                    benchmark=periods_result.benchmark,
                    period=period_strength.period,
                    stock_return_pct=period_strength.stock_return_pct,
                    benchmark_return_pct=period_strength.benchmark_return_pct,
                    diff_pct=period_strength.diff_pct,
                    classification=period_strength.classification,
                    strength_score=period_strength.strength_score,
                )
            )
            added += 1
    db.commit()
    return added
