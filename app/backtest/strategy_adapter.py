from __future__ import annotations

from typing import Callable, Optional

from app.analysis.indicator_engine import InsufficientDataError, compute_technical_snapshot
from app.analysis.market_regime_engine import MarketRegimeResult
from app.analysis.signal_engine import evaluate_signal
from app.backtest.engine_v5g import PointInTimeContext, SignalInstruction


RegimeProvider = Callable[[PointInTimeContext], MarketRegimeResult]


def _neutral_regime(_context: PointInTimeContext) -> MarketRegimeResult:
    return MarketRegimeResult(
        regime="yatay",
        index_symbol="XU100",
        snapshot=None,
        detail="Nokta-zaman benchmark rejimi saglanmadi; notr rejim.",
    )


class ExistingSignalStrategyAdapter:
    """Mevcut teknik/sinyal motorunu degistirmeden BacktestEngine'e baglar."""

    def __init__(
        self,
        *,
        symbol: str,
        timeframe: str,
        strategy_config: dict,
        provider_name: str,
        regime_provider: Optional[RegimeProvider] = None,
    ):
        self.symbol = symbol
        self.timeframe = timeframe
        self.strategy_config = strategy_config
        self.provider_name = provider_name
        self.regime_provider = regime_provider or _neutral_regime

    def __call__(self, context: PointInTimeContext) -> SignalInstruction:
        try:
            snapshot = compute_technical_snapshot(context.bars, self.symbol, self.timeframe)
        except InsufficientDataError:
            return SignalInstruction()
        result = evaluate_signal(
            snapshot=snapshot,
            regime_result=self.regime_provider(context),
            provider_name=self.provider_name,
            strategy_config=self.strategy_config,
            df=context.bars,
        )
        action = "BUY" if result.is_actionable_buy else (
            "EXIT" if result.signal_type in {"REDUCE_POSITION", "STRONG_RISK"} else "NONE"
        )
        return SignalInstruction(
            action=action,
            stop_price=result.stop_price,
            targets=(result.target_1, result.target_2, result.target_3),
            raw_signal_score=result.score,
            signal_type=result.signal_type,
            market_regime=result.market_regime,
            signal_time=result.data_timestamp,
            levels_as_of=result.data_timestamp,
        )
