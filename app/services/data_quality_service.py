from __future__ import annotations

import json
import logging
from typing import Optional

import pandas as pd
from sqlalchemy.orm import Session

from app.analysis.data_quality import DataQualityEngine, DataQualityResult
from app.models.database import DataQualitySnapshot

logger = logging.getLogger("mergen_quant.data_quality")


def provider_fetch_metadata(provider, symbol: str, timeframe: str) -> dict:
    if hasattr(provider, "metadata_for"):
        try:
            return provider.metadata_for(symbol, timeframe) or {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def assess_and_persist_quality(
    db: Optional[Session],
    df: pd.DataFrame,
    *,
    provider,
    symbol: str,
    timeframe: str,
    min_bars: int,
    check_incomplete: bool = False,
    max_staleness_minutes: Optional[float] = None,
) -> DataQualityResult:
    metadata = provider_fetch_metadata(provider, symbol, timeframe)
    source = metadata.get("provider") or getattr(provider, "name", "unknown")
    result = DataQualityEngine().evaluate(
        df,
        symbol=symbol,
        timeframe=timeframe,
        min_bars=min_bars,
        check_incomplete=check_incomplete,
        max_staleness_minutes=max_staleness_minutes,
        provider=source,
        fallback_used=bool(metadata.get("fallback_used")),
        cache_used=bool(metadata.get("cache_used")),
        cache_age_minutes=metadata.get("cache_age_minutes"),
        price_mode=getattr(provider, "price_mode", "unadjusted"),
    )
    if db is not None:
        try:
            db.add(
                DataQualitySnapshot(
                    symbol=symbol.upper(),
                    timeframe=timeframe,
                    status=result.status.value,
                    quality_score=result.score,
                    data_age_minutes=result.data_age_minutes,
                    last_bar_time=result.last_bar_time,
                    missing_bar_count=result.missing_bar_count,
                    duplicate_bar_count=result.duplicate_bar_count,
                    outlier_count=result.outlier_count,
                    incomplete_bar_count=result.incomplete_bar_count,
                    provider=result.provider,
                    fallback_used=result.fallback_used,
                    cache_used=result.cache_used,
                    cache_age_minutes=result.cache_age_minutes,
                    warnings_json=json.dumps(result.warnings, ensure_ascii=False),
                    issues_json=json.dumps(result.issues, ensure_ascii=False),
                    usable_for_analysis=result.usable_for_analysis,
                )
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001 - kalite logu ana analizi bozmamalı
            logger.warning("Veri kalite snapshot'ı kaydedilemedi symbol=%s: %s", symbol, exc)
            db.rollback()
    return result


def format_data_quality_status(symbol: str, result: DataQualityResult) -> str:
    fallback = "Hayır"
    if result.cache_used:
        fallback = f"Yerel cache ({result.cache_age_minutes:.0f} dk)" if result.cache_age_minutes is not None else "Yerel cache"
    elif result.fallback_used:
        fallback = "Evet"
    last_bar = result.last_bar_time.isoformat() if result.last_bar_time else "-"
    age = f"{result.data_age_minutes:.0f} dk" if result.data_age_minutes is not None else "-"
    cache = "Kullanıldı" if result.cache_used else "Kullanılmadı"
    return (
        "🏹 MERGEN QUANT — VERİ DURUMU\n\n"
        f"Sembol: {symbol.upper()}\n"
        f"Durum: {result.status.value}\n"
        f"Kalite: {result.score}/100\n"
        f"Son mum: {last_bar}\n"
        f"Veri yaşı: {age}\n"
        f"Ana provider: {result.provider}\n"
        f"Fallback: {fallback}\n"
        f"Eksik mum: {result.missing_bar_count}\n"
        f"Aykırı değer: {result.outlier_count}\n"
        f"Cache durumu: {cache}\n"
        f"Analize uygunluk: {'Uygun' if result.usable_for_analysis else 'Uygun değil'}"
    )
