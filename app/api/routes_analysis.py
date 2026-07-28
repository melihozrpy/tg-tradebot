from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.analysis.indicator_engine import InsufficientDataError
from app.config.settings import get_settings, get_strategy_config
from app.data.provider_factory import build_market_data_provider
from app.models.database import Signal, get_db_session
from app.services.analysis_service import AnalysisUnavailableError, run_symbol_analysis
from app.services.watchlist_service import InvalidSymbolError, normalize_symbol

router = APIRouter()


@router.get("/analysis/{symbol}")
def get_analysis(symbol: str, db: Session = Depends(get_db_session)):
    try:
        symbol = normalize_symbol(symbol)
    except InvalidSymbolError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    settings = get_settings()
    strategy_config = get_strategy_config()
    provider = build_market_data_provider(settings)
    timeframe = strategy_config["timeframes"]["primary"]

    try:
        outcome = run_symbol_analysis(db, provider, symbol, timeframe, strategy_config)
    except (AnalysisUnavailableError, InsufficientDataError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    signal = outcome.signal
    sr = signal.support_resistance
    return {
        "symbol": signal.symbol,
        "timeframe": signal.timeframe,
        "score": signal.score,
        "signal_type": signal.signal_type,
        "confidence": signal.confidence,
        "entry_zone": signal.entry_zone,
        "entry_trigger": signal.entry_trigger,
        "stop_price": signal.stop_price,
        "target_1": signal.target_1,
        "target_2": signal.target_2,
        "target_3": signal.target_3,
        "risk_reward": signal.risk_reward,
        "market_regime": signal.market_regime,
        "daily_change_percent": signal.daily_change_percent,
        "data_timestamp": signal.data_timestamp.isoformat(),
        "provider": signal.provider,
        "support_resistance": {
            "support_1": sr.support_1,
            "support_2": sr.support_2,
            "main_support": sr.main_support,
            "resistance_1": sr.resistance_1,
            "resistance_2": sr.resistance_2,
            "main_resistance": sr.main_resistance,
            "support_reliable": sr.support_reliable,
            "resistance_reliable": sr.resistance_reliable,
        } if sr else None,
        "contextual_notes": signal.contextual_notes,
        "reasons": [{"category": r.category, "description": r.description, "is_risk": r.is_risk} for r in signal.reasons],
        "is_new_signal": outcome.is_new_signal,
    }


@router.get("/signals")
def list_signals(db: Session = Depends(get_db_session), limit: int = 50):
    rows = db.query(Signal).order_by(Signal.created_at.desc()).limit(limit).all()
    return [
        {
            "id": s.id,
            "symbol": s.symbol,
            "signal_type": s.signal_type.value,
            "state": s.state.value,
            "score": s.score,
            "created_at": s.created_at.isoformat(),
        }
        for s in rows
    ]


@router.get("/signals/{signal_id}")
def get_signal(signal_id: int, db: Session = Depends(get_db_session)):
    s = db.query(Signal).filter(Signal.id == signal_id).first()
    if s is None:
        raise HTTPException(status_code=404, detail="Sinyal bulunamadi.")
    return {
        "id": s.id,
        "symbol": s.symbol,
        "timeframe": s.timeframe,
        "signal_type": s.signal_type.value,
        "state": s.state.value,
        "score": s.score,
        "confidence": s.confidence,
        "stop_price": s.stop_price,
        "target_1": s.target_1,
        "target_2": s.target_2,
        "risk_reward": s.risk_reward,
        "market_regime": s.market_regime,
        "strategy_version": s.strategy_version,
        "data_timestamp": s.data_timestamp.isoformat(),
        "provider": s.provider,
        "reasons": [
            {"category": r.category, "description": r.description, "is_risk": r.is_risk} for r in s.reasons
        ],
    }
