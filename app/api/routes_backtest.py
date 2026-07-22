from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.analysis.indicator_engine import InsufficientDataError
from app.backtest.engine import run_backtest
from app.config.settings import get_settings, get_strategy_config
from app.data.provider_factory import build_market_data_provider
from app.services.watchlist_service import InvalidSymbolError, normalize_symbol

router = APIRouter()


class BacktestRequest(BaseModel):
    symbol: str
    days: int = 500
    initial_capital: float = 100_000.0
    commission_percent: float = 0.15
    slippage_percent: float = 0.05


@router.post("/backtests")
def create_backtest(payload: BacktestRequest):
    try:
        symbol = normalize_symbol(payload.symbol)
    except InvalidSymbolError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    settings = get_settings()
    strategy_config = get_strategy_config()
    provider = build_market_data_provider(settings)
    timeframe = strategy_config["timeframes"]["primary"]

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=payload.days)

    try:
        df = provider.get_ohlcv(symbol, timeframe, start, end)
        result = run_backtest(
            df,
            symbol,
            timeframe,
            strategy_config,
            initial_capital=payload.initial_capital,
            commission_percent=payload.commission_percent,
            slippage_percent=payload.slippage_percent,
        )
    except InsufficientDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "symbol": result.symbol,
        "timeframe": result.timeframe,
        "initial_capital": result.initial_capital,
        "final_equity": result.final_equity,
        "trade_count": len(result.trades),
        "metrics": result.metrics.__dict__,
        "warnings": result.warnings,
    }
