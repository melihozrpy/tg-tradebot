from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from app.analysis.indicator_engine import (
    MIN_BARS_FOR_FULL_ANALYSIS,
    InsufficientDataError,
    compute_technical_snapshot,
)
from app.analysis.market_regime_engine import MarketRegimeResult
from app.analysis.signal_engine import evaluate_signal
from app.backtest.metrics import BacktestMetrics, compute_metrics
from app.risk.position_sizing import InvalidStopError, calculate_position_size


@dataclass
class BacktestTradeRecord:
    symbol: str
    side: str
    entry_time: datetime
    entry_price: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    quantity: float = 0.0
    pnl: Optional[float] = None
    exit_reason: Optional[str] = None
    stop_price: Optional[float] = None
    target_1: Optional[float] = None


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    initial_capital: float
    final_equity: float
    equity_curve: list[float]
    trades: list[BacktestTradeRecord]
    metrics: BacktestMetrics
    warnings: list[str] = field(default_factory=list)
    buy_and_hold_return_percent: Optional[float] = None
    benchmark_symbol: Optional[str] = None
    benchmark_return_percent: Optional[float] = None
    alpha_vs_benchmark_percent: Optional[float] = None


# Backtestte piyasa rejimini her bar icin tekrar hesaplamak yerine notr/sabit
# bir rejim kullanilir; FAZ 1'de endeks verisi olmadan gercekci rejim
# hesaplanamayacagi icin bu acikca "veri_yetersiz olmayan notr" bir varsayimla
# sinirlanir ve raporda "warnings" alaninda belirtilir.
_NEUTRAL_REGIME = MarketRegimeResult(
    regime="yatay", index_symbol="N/A", snapshot=None, detail="backtest: endeks rejimi kullanilmadi"
)


def run_backtest(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    strategy_config: dict,
    initial_capital: float = 100_000.0,
    commission_percent: float = 0.15,
    slippage_percent: float = 0.05,
    provider_name: str = "backtest",
    benchmark_df: Optional[pd.DataFrame] = None,
    benchmark_symbol: Optional[str] = None,
) -> BacktestResult:
    """Basit, tek pozisyonlu, uzun-only walk-forward backtest.

    Look-ahead bias engellenmesi: her adimda yalnizca o ana kadar KAPANMIS
    mumlar kullanilir (df.iloc[:i+1]); islem, sinyalin uretildigi mumdan
    SONRAKI mumun acilisinda gerceklesir.

    benchmark_df verilirse (orn. XU100), ayni tarih araliginda basit
    buy&hold getirisi hesaplanip stratejinin getirisiyle karsilastirilir
    (alpha = strateji getirisi - benchmark getirisi). Benchmark verisi
    yoksa bu alanlar None kalir; hicbir zaman uydurulmaz.
    """
    warnings: list[str] = []
    if len(df) < MIN_BARS_FOR_FULL_ANALYSIS + 2:
        raise InsufficientDataError(
            f"Backtest icin yeterli veri yok: {len(df)} bar < {MIN_BARS_FOR_FULL_ANALYSIS + 2}"
        )

    df = df.sort_values("timestamp").reset_index(drop=True)

    cash = initial_capital
    equity_curve: list[float] = [initial_capital]
    trades: list[BacktestTradeRecord] = []
    position: Optional[BacktestTradeRecord] = None
    position_qty = 0.0

    risk_cfg = strategy_config["risk"]

    for i in range(MIN_BARS_FOR_FULL_ANALYSIS, len(df) - 1):
        window = df.iloc[: i + 1]  # sadece kapanmis mumlar (look-ahead yok)
        next_bar = df.iloc[i + 1]  # islem SONRAKI mumun acilisinda gerceklesir

        try:
            snapshot = compute_technical_snapshot(window, symbol, timeframe)
        except InsufficientDataError:
            equity_curve.append(cash if position is None else equity_curve[-1])
            continue

        current_price = snapshot.close

        # Acik pozisyon varsa stop/hedef kontrolu (sonraki barin high/low ile)
        if position is not None:
            exit_price = None
            exit_reason = None
            if position.stop_price is not None and next_bar["low"] <= position.stop_price:
                exit_price = position.stop_price
                exit_reason = "STOP_HIT"
            elif position.target_1 is not None and next_bar["high"] >= position.target_1:
                exit_price = position.target_1
                exit_reason = "TARGET_1_HIT"

            if exit_price is not None:
                slip = exit_price * (slippage_percent / 100)
                fill_price = max(exit_price - slip, 0.01)
                commission = fill_price * position_qty * (commission_percent / 100)
                proceeds = fill_price * position_qty - commission
                cost_basis = position.entry_price * position_qty
                pnl = proceeds - cost_basis

                position.exit_time = next_bar["timestamp"].to_pydatetime()
                position.exit_price = fill_price
                position.pnl = round(pnl, 2)
                position.exit_reason = exit_reason
                trades.append(position)

                cash += proceeds
                position = None
                position_qty = 0.0

        # Yeni pozisyon araniyor (sadece pozisyon yoksa)
        if position is None:
            signal = evaluate_signal(
                snapshot=snapshot,
                regime_result=_NEUTRAL_REGIME,
                provider_name=provider_name,
                strategy_config=strategy_config,
                df=window,
            )
            if signal.is_actionable_buy and signal.stop_price is not None:
                try:
                    sizing = calculate_position_size(
                        total_capital=cash,
                        risk_percent=risk_cfg["risk_per_trade_percent"],
                        entry_price=current_price,
                        stop_price=signal.stop_price,
                    )
                except InvalidStopError:
                    sizing = None

                if sizing is not None and sizing.lot > 0:
                    entry_slip = next_bar["open"] * (slippage_percent / 100)
                    fill_price = next_bar["open"] + entry_slip
                    commission = fill_price * sizing.lot * (commission_percent / 100)
                    total_cost = fill_price * sizing.lot + commission

                    if total_cost <= cash:
                        cash -= total_cost
                        position_qty = sizing.lot
                        position = BacktestTradeRecord(
                            symbol=symbol,
                            side="BUY",
                            entry_time=next_bar["timestamp"].to_pydatetime(),
                            entry_price=fill_price,
                            quantity=sizing.lot,
                            stop_price=signal.stop_price,
                            target_1=signal.target_1,
                        )

        mark_price = current_price if position is None else next_bar["close"]
        unrealized = (mark_price - position.entry_price) * position_qty if position else 0.0
        equity_curve.append(cash + unrealized)

    # Backtest sonunda acik pozisyon varsa son fiyattan kapat (mark-to-market)
    if position is not None:
        last_close = float(df.iloc[-1]["close"])
        proceeds = last_close * position_qty
        pnl = proceeds - (position.entry_price * position_qty)
        position.exit_time = df.iloc[-1]["timestamp"].to_pydatetime()
        position.exit_price = last_close
        position.pnl = round(pnl, 2)
        position.exit_reason = "BACKTEST_END"
        trades.append(position)
        cash += proceeds
        equity_curve.append(cash)

    trade_pnls = [t.pnl for t in trades if t.pnl is not None]
    holding_periods = [
        (t.exit_time - t.entry_time).total_seconds() / 86400
        for t in trades
        if t.exit_time is not None
    ]
    metrics = compute_metrics(equity_curve, trade_pnls, holding_periods)

    warnings.append(
        "Backtest, endeks/piyasa rejimi hesaplamadan notr rejim varsayimiyla calisir (FAZ 1 sinirlamasi)."
    )
    warnings.append("Mock/CSV veri uzerinde calisiliyorsa sonuclar gercek piyasa performansini yansitmaz.")
    if len(trades) < 20:
        warnings.append(f"Islem sayisi dusuk ({len(trades)}); istatistiksel anlamlilik sinirlidir (overfitting riski).")

    total_return_percent = round((equity_curve[-1] / initial_capital - 1) * 100, 2)

    buy_and_hold_return_percent = None
    first_close = float(df.iloc[MIN_BARS_FOR_FULL_ANALYSIS]["close"])
    last_close = float(df.iloc[-1]["close"])
    if first_close > 0:
        buy_and_hold_return_percent = round((last_close / first_close - 1) * 100, 2)

    benchmark_return_percent = None
    alpha_vs_benchmark_percent = None
    if benchmark_df is not None and not benchmark_df.empty:
        bench = benchmark_df.sort_values("timestamp").reset_index(drop=True)
        # Ayni tarih araligina hizala (backtest'in kullandigi ilk/son tarih)
        start_ts = df.iloc[MIN_BARS_FOR_FULL_ANALYSIS]["timestamp"]
        end_ts = df.iloc[-1]["timestamp"]
        mask = (bench["timestamp"] >= start_ts) & (bench["timestamp"] <= end_ts)
        aligned = bench.loc[mask]
        if len(aligned) >= 2:
            bench_first = float(aligned.iloc[0]["close"])
            bench_last = float(aligned.iloc[-1]["close"])
            if bench_first > 0:
                benchmark_return_percent = round((bench_last / bench_first - 1) * 100, 2)
                alpha_vs_benchmark_percent = round(total_return_percent - benchmark_return_percent, 2)
        else:
            warnings.append("Benchmark verisi backtest tarih araligiyla hizalanamadi; karsilastirma atlandi.")

    return BacktestResult(
        symbol=symbol,
        timeframe=timeframe,
        initial_capital=initial_capital,
        final_equity=round(equity_curve[-1], 2),
        equity_curve=equity_curve,
        trades=trades,
        metrics=metrics,
        buy_and_hold_return_percent=buy_and_hold_return_percent,
        benchmark_symbol=benchmark_symbol,
        benchmark_return_percent=benchmark_return_percent,
        alpha_vs_benchmark_percent=alpha_vs_benchmark_percent,
        warnings=warnings,
    )
