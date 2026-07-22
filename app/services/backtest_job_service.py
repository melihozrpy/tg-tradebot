from __future__ import annotations

import asyncio
import json
import math
import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import pandas as pd
from sqlalchemy.orm import Session, sessionmaker

from app.backtest.engine_v5g import (
    BacktestCancelled,
    BacktestConfig,
    BacktestEngine,
    BacktestResultV5G,
    BacktestTimeout,
    SignalProvider,
)
from app.models.database import BacktestDailyEquity, BacktestMetric, BacktestRun, BacktestTrade


class BacktestJobError(ValueError):
    pass


BarsLoader = Callable[[], pd.DataFrame]
CompletionCallback = Callable[[str, str], Awaitable[None]]


class BacktestJobService:
    """Agir kosulari thread'de calistirir; durum, iptal ve restart kaydi tutar."""

    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        max_concurrent_per_user: int = 1,
        timeout_seconds: int = 600,
    ):
        self.session_factory = session_factory
        self.max_concurrent_per_user = max(1, int(max_concurrent_per_user))
        self.timeout_seconds = max(1, int(timeout_seconds))
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, threading.Event] = {}

    async def start(
        self,
        *,
        user_id: int,
        symbol: str,
        timeframe: str,
        start_date: datetime,
        end_date: datetime,
        bars_loader: BarsLoader,
        signal_provider: SignalProvider,
        config: BacktestConfig,
        benchmark_loader: Optional[BarsLoader] = None,
        strategy_version: str = "5g",
        strategy_name: str = "existing_signal_engine",
        provider: str = "unknown",
        on_complete: Optional[CompletionCallback] = None,
    ) -> str:
        db: Session = self.session_factory()
        try:
            active = db.query(BacktestRun).filter(
                BacktestRun.user_id == user_id,
                BacktestRun.run_status.in_(["PENDING", "RUNNING"]),
            ).count()
            if active >= self.max_concurrent_per_user:
                raise BacktestJobError("Ayni anda izin verilen agir backtest sinirina ulasildi.")
            run_id = "btjob_" + uuid.uuid4().hex[:24]
            costs = config.transaction_costs
            record = BacktestRun(
                run_id=run_id, user_id=user_id, symbol=symbol.upper(), timeframe=timeframe,
                start_date=start_date, end_date=end_date, initial_capital=config.initial_capital,
                commission_percent=costs.commission_bps / 100.0,
                slippage_percent=costs.slippage_bps / 100.0,
                strategy_version=strategy_version, strategy_name=strategy_name,
                config_snapshot=json.dumps(config.snapshot(), sort_keys=True, default=str),
                transaction_cost_config=json.dumps(asdict(costs), sort_keys=True),
                price_adjustment_mode=config.price_adjustment_mode,
                provider=provider, seed=config.seed,
                status="PENDING", run_status="PENDING", progress_percent=0.0,
            )
            db.add(record)
            db.commit()
        finally:
            db.close()

        cancel_event = threading.Event()
        self._cancel_events[run_id] = cancel_event
        task = asyncio.create_task(self._execute(
            run_id=run_id, bars_loader=bars_loader, benchmark_loader=benchmark_loader,
            signal_provider=signal_provider, config=config, cancel_event=cancel_event,
            on_complete=on_complete,
        ))
        self._tasks[run_id] = task
        return run_id

    async def _execute(
        self,
        *,
        run_id: str,
        bars_loader: BarsLoader,
        benchmark_loader: Optional[BarsLoader],
        signal_provider: SignalProvider,
        config: BacktestConfig,
        cancel_event: threading.Event,
        on_complete: Optional[CompletionCallback],
    ) -> None:
        status = "FAILED"
        try:
            await asyncio.to_thread(
                self._run_sync, run_id, bars_loader, benchmark_loader,
                signal_provider, config, cancel_event,
            )
            status = "COMPLETED"
        except BacktestCancelled:
            status = "CANCELLED"
            self._set_failure(run_id, status, "Kullanici tarafindan iptal edildi.")
        except BacktestTimeout:
            status = "FAILED"
            self._set_failure(run_id, status, "Backtest zaman sinirini asti.")
        except Exception as exc:  # teknik ayrinti DB'de, Telegram'da steril metin
            self._set_failure(run_id, "FAILED", f"{type(exc).__name__}: {exc}")
        finally:
            self._tasks.pop(run_id, None)
            self._cancel_events.pop(run_id, None)
            if on_complete is not None:
                await on_complete(run_id, status)

    def _run_sync(
        self,
        run_id: str,
        bars_loader: BarsLoader,
        benchmark_loader: Optional[BarsLoader],
        signal_provider: SignalProvider,
        config: BacktestConfig,
        cancel_event: threading.Event,
    ) -> None:
        db: Session = self.session_factory()
        last_progress = -5
        try:
            record = db.query(BacktestRun).filter_by(run_id=run_id).one()
            record.status = record.run_status = "RUNNING"
            record.started_at = datetime.now(timezone.utc)
            db.commit()
            bars = bars_loader()
            benchmark = benchmark_loader() if benchmark_loader else None

            def update_progress(value: float) -> None:
                nonlocal last_progress
                bucket = int(value // 5) * 5
                if bucket <= last_progress:
                    return
                last_progress = bucket
                record.progress_percent = min(100.0, value)
                record.updated_at = datetime.now(timezone.utc)
                db.commit()

            result = BacktestEngine(config).run(
                bars, record.symbol, signal_provider,
                benchmark_bars=benchmark,
                cancel_check=cancel_event.is_set,
                progress_callback=update_progress,
                timeout_seconds=self.timeout_seconds,
            )
            self._persist_result(db, record, result)
        finally:
            db.close()

    @staticmethod
    def _persist_result(db: Session, record: BacktestRun, result: BacktestResultV5G) -> None:
        record.data_version = result.data_version
        record.metrics_json = json.dumps(_json_safe(asdict(result.metrics)), sort_keys=True, allow_nan=False)
        record.status = record.run_status = "COMPLETED"
        record.progress_percent = 100.0
        record.finished_at = datetime.now(timezone.utc)
        record.updated_at = record.finished_at
        db.flush()
        for trade in result.trades:
            db.add(BacktestTrade(
                backtest_run_id=record.id, symbol=trade.symbol, side="BUY",
                entry_time=trade.entry_time, exit_time=trade.exit_time,
                entry_price=trade.entry_price, exit_price=trade.exit_price,
                quantity=trade.quantity, pnl=trade.net_pnl, gross_pnl=trade.gross_pnl,
                total_cost=trade.total_cost, exit_reason=trade.exit_reason,
                stop_price=trade.stop_price, target_1=trade.target_1,
                target_2=trade.target_2, target_3=trade.target_3,
                target_1_hit=trade.target_1_hit, target_2_hit=trade.target_2_hit,
                target_3_hit=trade.target_3_hit, mae_percent=trade.mae_percent,
                mfe_percent=trade.mfe_percent, holding_bars=trade.holding_bars,
                market_regime=trade.market_regime, sector=trade.sector,
                signal_type=trade.signal_type, raw_signal_score=trade.raw_signal_score,
            ))
        for point in result.equity_points:
            db.add(BacktestDailyEquity(
                backtest_run_id=record.id, trading_date=point.timestamp,
                strategy_equity=point.equity, benchmark_equity=point.benchmark_equity,
                exposure_percent=point.exposure_percent,
            ))
        db.add(BacktestMetric(
            backtest_run_id=record.id, scope_type="overall", scope_value="out_of_sample" if result.out_of_sample else "full",
            metrics_json=record.metrics_json, sample_count=len(result.trades),
            evidence_class=("YETERSIZ_ORNEK" if not result.metrics.sample_sufficient else "DEGERLENDIRILEBILIR"),
        ))
        db.commit()

    def _set_failure(self, run_id: str, status: str, detail: str) -> None:
        db: Session = self.session_factory()
        try:
            record = db.query(BacktestRun).filter_by(run_id=run_id).one_or_none()
            if record is not None:
                record.status = record.run_status = status
                record.error_detail = detail[:2000]
                record.finished_at = datetime.now(timezone.utc)
                db.commit()
        finally:
            db.close()

    def cancel(self, *, user_id: int, run_id: str) -> bool:
        db: Session = self.session_factory()
        try:
            record = db.query(BacktestRun).filter_by(run_id=run_id, user_id=user_id).one_or_none()
            if record is None or record.run_status not in {"PENDING", "RUNNING"}:
                return False
            event = self._cancel_events.get(run_id)
            if event is None:
                return False
            event.set()
            return True
        finally:
            db.close()

    async def wait(self, run_id: str) -> None:
        task = self._tasks.get(run_id)
        if task is not None:
            await task

    @staticmethod
    def mark_interrupted_runs(session_factory: sessionmaker) -> int:
        db: Session = session_factory()
        try:
            try:
                records = db.query(BacktestRun).filter(BacktestRun.run_status.in_(["PENDING", "RUNNING"])).all()
            except Exception:
                # Migration oncesi veya tamamen bos smoke DB'sinde tablo/sutun
                # olmayabilir; startup bu nedenle cokmemelidir.
                db.rollback()
                return 0
            for record in records:
                record.status = record.run_status = "INTERRUPTED"
                record.error_detail = "Uygulama yeniden baslatildigi icin yarim kalan kosu durduruldu."
                record.finished_at = datetime.now(timezone.utc)
            db.commit()
            return len(records)
        finally:
            db.close()


def _json_safe(value):
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
