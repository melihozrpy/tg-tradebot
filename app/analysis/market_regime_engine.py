from __future__ import annotations

from dataclasses import dataclass

from app.analysis.indicator_engine import InsufficientDataError, TechnicalSnapshot, compute_technical_snapshot
import pandas as pd

from app.data.base_provider import BaseMarketDataProvider, DataUnavailableError

REGIME_STRONG_UP = "guclu_yukselis"
REGIME_WEAK_UP = "zayif_yukselis"
REGIME_SIDEWAYS = "yatay"
REGIME_DISTRIBUTION = "dagitim"
REGIME_WEAK_DOWN = "zayif_dusus"
REGIME_STRONG_DOWN = "guclu_dusus"
REGIME_VOLATILE = "asiri_volatil"
REGIME_INSUFFICIENT = "veri_yetersiz"


@dataclass
class MarketRegimeResult:
    regime: str
    index_symbol: str
    snapshot: TechnicalSnapshot | None
    detail: str


def classify_market_regime_from_df(
    df: pd.DataFrame,
    index_symbol: str = "XU100",
    timeframe: str = "1d",
) -> MarketRegimeResult:
    """Önceden alınmış endeks verisini kullanır; aynı komutta ikinci fetch yapmaz."""

    try:
        snapshot = compute_technical_snapshot(df, index_symbol, timeframe)
    except (InsufficientDataError, ValueError, TypeError) as exc:
        return MarketRegimeResult(
            regime=REGIME_INSUFFICIENT,
            index_symbol=index_symbol,
            snapshot=None,
            detail=f"Endeks verisi yetersiz: {exc}",
        )

    atr_ratio = snapshot.atr / snapshot.close if snapshot.close else 0
    if atr_ratio > 0.045 or snapshot.bb_width > 0.12:
        return MarketRegimeResult(
            regime=REGIME_VOLATILE,
            index_symbol=index_symbol,
            snapshot=snapshot,
            detail=f"atr_ratio={atr_ratio:.4f} bb_width={snapshot.bb_width:.4f}",
        )

    strong_trend = snapshot.adx >= 25
    if snapshot.trend_direction == "up":
        regime = REGIME_STRONG_UP if strong_trend else REGIME_WEAK_UP
    elif snapshot.trend_direction == "down":
        regime = REGIME_STRONG_DOWN if strong_trend else REGIME_WEAK_DOWN
    else:
        regime = REGIME_DISTRIBUTION if not snapshot.obv_trend_up else REGIME_SIDEWAYS
    return MarketRegimeResult(
        regime=regime,
        index_symbol=index_symbol,
        snapshot=snapshot,
        detail=f"trend={snapshot.trend_direction} adx={snapshot.adx:.1f}",
    )


def classify_market_regime(
    provider: BaseMarketDataProvider, index_symbol: str = "XU100", timeframe: str = "1d"
) -> MarketRegimeResult:
    from datetime import datetime, timedelta, timezone

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)

    try:
        df = provider.get_ohlcv(index_symbol, timeframe, start, end)
    except (DataUnavailableError, InsufficientDataError) as exc:
        return MarketRegimeResult(
            regime=REGIME_INSUFFICIENT,
            index_symbol=index_symbol,
            snapshot=None,
            detail=f"Endeks verisi yetersiz/mevcut degil: {exc}",
        )

    return classify_market_regime_from_df(df, index_symbol, timeframe)
