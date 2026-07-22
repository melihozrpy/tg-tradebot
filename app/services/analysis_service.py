from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.analysis.indicator_engine import InsufficientDataError, compute_technical_snapshot
from app.analysis.market_regime_engine import classify_market_regime
from app.analysis.signal_engine import SignalResult, evaluate_signal
from app.data.base_provider import BaseMarketDataProvider, DataUnavailableError
from app.models.database import Signal, SignalReason, SignalStateEnum, SignalTypeEnum

logger = logging.getLogger("mergen_quant.analysis")


class AnalysisUnavailableError(Exception):
    """Analiz yapilamadiginda (veri eksik/eski/tutarsiz) firlatilir; bot bu durumda sinyal vermez."""


@dataclass
class AnalysisOutcome:
    signal: SignalResult
    is_new_signal: bool
    is_cooldown_blocked: bool


def run_symbol_analysis(
    db: Session,
    provider: BaseMarketDataProvider,
    symbol: str,
    timeframe: str,
    strategy_config: dict,
) -> AnalysisOutcome:
    """Bir sembol icin uctan uca analiz calistirir: veri -> indikator -> rejim -> skor.

    Fail-closed davranis: veri eksik, eski veya tutarsizsa AnalysisUnavailableError
    firlatilir ve HICBIR sinyal olusturulmaz/gonderilmez.
    """
    filters = strategy_config["filters"]

    try:
        freshness = provider.get_data_freshness(symbol, timeframe)
    except DataUnavailableError:
        raise AnalysisUnavailableError("Bu sembol için güncel veri alınamadı.")

    if freshness.last_timestamp is None:
        raise AnalysisUnavailableError("Bu sembol için güncel veri alınamadı.")
    if not freshness.is_fresh and not filters.get("allow_signals_on_stale_data", False):
        raise AnalysisUnavailableError(
            f"{symbol}/{timeframe} verisi guncel degil (son veri: {freshness.last_timestamp}). "
            "Fail-closed: sinyal uretilmeyecek."
        )

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    try:
        df = provider.get_ohlcv(symbol, timeframe, start, end)
    except DataUnavailableError as exc:
        raise AnalysisUnavailableError(f"Bu sembol için güncel veri alınamadı. (Detay: {exc})") from exc

    try:
        snapshot = compute_technical_snapshot(df, symbol, timeframe)
    except InsufficientDataError as exc:
        raise AnalysisUnavailableError(str(exc)) from exc

    regime_result = classify_market_regime(provider, index_symbol="XU100", timeframe=timeframe)

    signal = evaluate_signal(
        snapshot=snapshot,
        regime_result=regime_result,
        provider_name=provider.name,
        strategy_config=strategy_config,
        df=df,
    )
    signal.extras["close"] = snapshot.close
    signal.extras["trend_direction"] = snapshot.trend_direction

    existing = db.query(Signal).filter(Signal.idempotency_key == signal.idempotency_key).first()
    if existing is not None:
        return AnalysisOutcome(signal=signal, is_new_signal=False, is_cooldown_blocked=True)

    cooldown_minutes = strategy_config["thresholds"]["signal_cooldown_minutes"]
    cooldown_cutoff = datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)
    recent_same_type = (
        db.query(Signal)
        .filter(
            Signal.symbol == symbol,
            Signal.timeframe == timeframe,
            Signal.signal_type == SignalTypeEnum(signal.signal_type),
            Signal.created_at >= cooldown_cutoff,
        )
        .first()
    )
    if recent_same_type is not None:
        return AnalysisOutcome(signal=signal, is_new_signal=False, is_cooldown_blocked=True)

    db_signal = Signal(
        symbol=signal.symbol,
        timeframe=signal.timeframe,
        signal_type=SignalTypeEnum(signal.signal_type),
        state=SignalStateEnum.CREATED,
        score=signal.score,
        confidence=signal.confidence,
        entry_zone_low=signal.entry_zone[0],
        entry_zone_high=signal.entry_zone[1],
        stop_price=signal.stop_price,
        target_1=signal.target_1,
        target_2=signal.target_2,
        risk_reward=signal.risk_reward,
        market_regime=signal.market_regime,
        strategy_version=signal.strategy_version,
        data_timestamp=signal.data_timestamp,
        provider=signal.provider,
        idempotency_key=signal.idempotency_key,
    )
    db.add(db_signal)
    db.flush()

    for reason in signal.reasons:
        db.add(
            SignalReason(
                signal_id=db_signal.id,
                category=reason.category,
                description=reason.description,
                is_risk=reason.is_risk,
            )
        )
    db.commit()

    return AnalysisOutcome(signal=signal, is_new_signal=True, is_cooldown_blocked=False)
