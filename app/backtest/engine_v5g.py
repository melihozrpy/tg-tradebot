from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Iterable, Optional, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from app.backtest.metrics import BacktestMetrics, compute_metrics

ISTANBUL = ZoneInfo("Europe/Istanbul")


class BacktestValidationError(ValueError):
    """Girdi veya zaman sirasi gercekci bir backteste uygun degil."""


class LookAheadBiasError(BacktestValidationError):
    """Strateji karar anindan sonraki veriye erismeye calisti."""


class PriceAdjustmentMismatchError(BacktestValidationError):
    """Raw ve adjusted seriler ayni kosuda karistirildi."""


class BacktestCancelled(RuntimeError):
    pass


class BacktestTimeout(RuntimeError):
    pass


class EntryModel(str, Enum):
    NEXT_OPEN = "next_open"
    NEXT_VWAP = "next_vwap"
    NEXT_CLOSE = "next_close"


class IntrabarPolicy(str, Enum):
    CONSERVATIVE = "conservative"
    OPTIMISTIC = "optimistic"
    NEAREST_TO_OPEN = "nearest_to_open"
    LOWER_TIMEFRAME = "lower_timeframe"


@dataclass(frozen=True)
class TransactionCostConfig:
    commission_bps: float = 15.0
    slippage_bps: float = 5.0
    spread_bps: float = 10.0
    bsmv_bps: float = 0.0
    minimum_cost: float = 0.0

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if value < 0:
                raise BacktestValidationError(f"{name} negatif olamaz.")

    def entry_fill(self, reference_price: float) -> float:
        impact_bps = self.slippage_bps + self.spread_bps / 2.0
        return reference_price * (1.0 + impact_bps / 10_000.0)

    def exit_fill(self, reference_price: float) -> float:
        impact_bps = self.slippage_bps + self.spread_bps / 2.0
        return max(reference_price * (1.0 - impact_bps / 10_000.0), 0.0001)

    def cash_cost(self, notional: float) -> float:
        variable = notional * (self.commission_bps + self.bsmv_bps) / 10_000.0
        return max(variable, self.minimum_cost) if notional > 0 else 0.0

    def impact_cost(self, reference_price: float, fill_price: float, quantity: float) -> float:
        return abs(fill_price - reference_price) * quantity


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 100_000.0
    max_position_pct: float = 20.0
    entry_model: EntryModel | str = EntryModel.NEXT_OPEN
    intrabar_policy: IntrabarPolicy | str = IntrabarPolicy.CONSERVATIVE
    transaction_costs: TransactionCostConfig = field(default_factory=TransactionCostConfig)
    target_allocations: tuple[float, float, float] = (0.40, 0.30, 0.30)
    trailing_stop_percent: Optional[float] = None
    max_holding_bars: int = 60
    reverse_signal_exit: bool = True
    price_adjustment_mode: str = "adjusted"
    minimum_history_bars: int = 2
    minimum_sample_size: int = 30
    seed: int = 42

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_model", EntryModel(self.entry_model))
        object.__setattr__(self, "intrabar_policy", IntrabarPolicy(self.intrabar_policy))
        mode = self.price_adjustment_mode.strip().lower()
        if mode not in {"adjusted", "raw"}:
            raise BacktestValidationError("price_adjustment_mode adjusted veya raw olmali.")
        object.__setattr__(self, "price_adjustment_mode", mode)
        if self.initial_capital <= 0 or not (0 < self.max_position_pct <= 100):
            raise BacktestValidationError("Sermaye ve maksimum pozisyon yuzdesi gecersiz.")
        if len(self.target_allocations) != 3 or any(x < 0 for x in self.target_allocations):
            raise BacktestValidationError("Uc adet negatif olmayan hedef cikis orani gerekli.")
        if sum(self.target_allocations) > 1.000001:
            raise BacktestValidationError("Hedef cikis oranlari toplami %100'u asamaz.")
        if self.max_holding_bars < 1 or self.minimum_history_bars < 1:
            raise BacktestValidationError("Bar sinirlari pozitif olmali.")

    def snapshot(self) -> dict:
        payload = asdict(self)
        payload["entry_model"] = self.entry_model.value
        payload["intrabar_policy"] = self.intrabar_policy.value
        return payload


@dataclass(frozen=True)
class SignalInstruction:
    action: str = "NONE"  # BUY | EXIT | NONE
    stop_price: Optional[float] = None
    targets: tuple[Optional[float], Optional[float], Optional[float]] = (None, None, None)
    invalidation_price: Optional[float] = None
    raw_signal_score: Optional[float] = None
    signal_type: str = "UNKNOWN"
    market_regime: str = "unknown"
    volatility_regime: str = "unknown"
    liquidity_regime: str = "unknown"
    sector: Optional[str] = None
    signal_time: Optional[datetime] = None
    levels_as_of: Optional[datetime] = None


class PointInTimeContext:
    """Stratejiye yalnizca karar aninda bilinen tamamlanmis mumlari verir."""

    def __init__(self, visible_bars: pd.DataFrame):
        self._bars = visible_bars.copy(deep=True)
        self.as_of = self._bars.iloc[-1]["timestamp"]

    @property
    def bars(self) -> pd.DataFrame:
        return self._bars.copy(deep=True)

    @property
    def current(self) -> pd.Series:
        return self._bars.iloc[-1].copy(deep=True)

    def bar(self, offset: int = 0) -> pd.Series:
        if offset > 0:
            raise LookAheadBiasError("Gelecek mum verisine erisim engellendi.")
        index = len(self._bars) - 1 + offset
        if index < 0:
            raise IndexError("Gorunur gecmiste bu mum yok.")
        return self._bars.iloc[index].copy(deep=True)

    def future_bar(self, _offset: int = 1) -> pd.Series:
        raise LookAheadBiasError("Future candle access engellendi.")

    def records_available_at(self, records: Iterable[dict], time_field: str = "published_at") -> list[dict]:
        """Finansal/haber kaydini yayin zamanindan once gorunur yapmaz."""
        result: list[dict] = []
        cutoff = pd.Timestamp(self.as_of)
        for item in records:
            published = pd.Timestamp(item[time_field])
            if published.tzinfo is None:
                published = published.tz_localize(ISTANBUL)
            else:
                published = published.tz_convert(ISTANBUL)
            if published <= cutoff:
                result.append(dict(item))
        return result


class SignalProvider(Protocol):
    def __call__(self, context: PointInTimeContext) -> Optional[SignalInstruction]: ...


@dataclass
class BacktestTradeV5G:
    symbol: str
    entry_time: datetime
    entry_price: float
    quantity: float
    stop_price: Optional[float]
    target_1: Optional[float]
    target_2: Optional[float]
    target_3: Optional[float]
    signal_type: str
    raw_signal_score: Optional[float]
    market_regime: str
    volatility_regime: str
    liquidity_regime: str
    sector: Optional[str]
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    net_pnl: float = 0.0
    gross_pnl: float = 0.0
    total_cost: float = 0.0
    mae_percent: float = 0.0
    mfe_percent: float = 0.0
    holding_bars: int = 0
    target_1_hit: bool = False
    target_2_hit: bool = False
    target_3_hit: bool = False
    partial_exits: list[dict] = field(default_factory=list)

    @property
    def pnl(self) -> float:
        return self.net_pnl


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    equity: float
    benchmark_equity: Optional[float]
    exposure_percent: float


@dataclass
class BacktestResultV5G:
    run_id: str
    data_version: str
    symbol: str
    config_snapshot: dict
    initial_capital: float
    final_equity: float
    trades: list[BacktestTradeV5G]
    equity_points: list[EquityPoint]
    metrics: BacktestMetrics
    segmented_results: dict[str, dict[str, dict[str, float]]]
    warnings: list[str]
    excluded_periods: list[dict]
    out_of_sample: bool = False


@dataclass
class _OpenPosition:
    trade: BacktestTradeV5G
    remaining_quantity: float
    entry_outlay: float
    proceeds: float = 0.0
    highest_price: float = 0.0
    lowest_price: float = float("inf")
    bars_held: int = 0
    hit_targets: list[bool] = field(default_factory=lambda: [False, False, False])
    invalidation_price: Optional[float] = None


LowerTimeframeResolver = Callable[[_OpenPosition, pd.Series], list[str]]


class BacktestEngine:
    """Nokta-zaman verisi, sonraki mum girisi ve masraflarla deterministik motor."""

    REQUIRED_COLUMNS = {"timestamp", "open", "high", "low", "close", "volume"}

    def __init__(self, config: BacktestConfig | None = None):
        self.config = config or BacktestConfig()

    def run(
        self,
        bars: pd.DataFrame,
        symbol: str,
        signal_provider: SignalProvider,
        *,
        benchmark_bars: Optional[pd.DataFrame] = None,
        lower_timeframe_resolver: Optional[LowerTimeframeResolver] = None,
        out_of_sample: bool = False,
        cancel_check: Optional[Callable[[], bool]] = None,
        progress_callback: Optional[Callable[[float], None]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> BacktestResultV5G:
        frame, excluded, warnings = self._prepare_bars(bars)
        if len(frame) < self.config.minimum_history_bars + 1:
            raise BacktestValidationError("Backtest icin yeterli tamamlanmis ve gecerli mum yok.")

        data_version = self._data_version(frame)
        run_id = self._run_id(symbol, data_version)
        cash = float(self.config.initial_capital)
        position: Optional[_OpenPosition] = None
        completed: list[BacktestTradeV5G] = []
        equity_points: list[EquityPoint] = []
        exposure_periods = 0
        started_monotonic = time.monotonic()

        benchmark = self._prepare_benchmark(benchmark_bars)
        benchmark_initial = None
        if benchmark is not None and not benchmark.empty:
            benchmark_initial = float(benchmark.iloc[0]["close"])

        for execution_index in range(self.config.minimum_history_bars, len(frame)):
            if cancel_check is not None and cancel_check():
                raise BacktestCancelled("Backtest kullanici tarafindan iptal edildi.")
            if timeout_seconds is not None and time.monotonic() - started_monotonic > timeout_seconds:
                raise BacktestTimeout("Backtest kaynak suresi sinirini asti.")
            if progress_callback is not None:
                progress_callback(execution_index / max(len(frame) - 1, 1) * 100.0)
            decision_bars = frame.iloc[:execution_index]
            execution_bar = frame.iloc[execution_index]
            context = PointInTimeContext(decision_bars)
            instruction = signal_provider(context) or SignalInstruction()
            self._validate_instruction_time(instruction, context)

            if position is not None and self.config.price_adjustment_mode == "raw":
                self._apply_split(position, execution_bar)
                dividend = float(execution_bar.get("cash_dividend", 0.0) or 0.0)
                if dividend > 0:
                    dividend_cash = dividend * position.remaining_quantity
                    cash += dividend_cash
                    position.proceeds += dividend_cash
                    for field_name in ("stop_price", "target_1", "target_2", "target_3"):
                        value = getattr(position.trade, field_name)
                        if value is not None:
                            setattr(position.trade, field_name, max(0.0001, value - dividend))

            action = instruction.action.strip().upper()
            if position is not None and self.config.reverse_signal_exit and action in {"EXIT", "SELL"}:
                reference = self._entry_reference(execution_bar)
                cash += self._close_position(position, reference, execution_bar["timestamp"], "REVERSE_SIGNAL")
                completed.append(position.trade)
                position = None

            if position is None and action == "BUY":
                position, debit = self._open_position(symbol, instruction, execution_bar, cash)
                if position is not None:
                    cash -= debit

            if position is not None:
                exposure_periods += 1
                position.bars_held += 1
                position.trade.holding_bars = position.bars_held
                self._update_excursions(position, execution_bar)
                cash_delta, closed = self._process_intrabar(
                    position, execution_bar, lower_timeframe_resolver, warnings
                )
                cash += cash_delta
                if closed:
                    completed.append(position.trade)
                    position = None
                elif (
                    position.invalidation_price is not None
                    and float(execution_bar["close"]) <= position.invalidation_price
                ):
                    cash += self._close_position(
                        position, float(execution_bar["close"]), execution_bar["timestamp"], "INVALIDATED"
                    )
                    completed.append(position.trade)
                    position = None
                elif position.bars_held >= self.config.max_holding_bars:
                    cash += self._close_position(
                        position, float(execution_bar["close"]), execution_bar["timestamp"], "TIME_EXIT"
                    )
                    completed.append(position.trade)
                    position = None

            mark_value = 0.0 if position is None else position.remaining_quantity * float(execution_bar["close"])
            equity = cash + mark_value
            benchmark_equity = self._benchmark_equity(
                benchmark, benchmark_initial, execution_bar["timestamp"]
            )
            equity_points.append(
                EquityPoint(
                    timestamp=pd.Timestamp(execution_bar["timestamp"]).to_pydatetime(),
                    equity=round(equity, 4),
                    benchmark_equity=benchmark_equity,
                    exposure_percent=(100.0 if position is not None else 0.0),
                )
            )

        if position is not None:
            final_bar = frame.iloc[-1]
            cash += self._close_position(position, float(final_bar["close"]), final_bar["timestamp"], "BACKTEST_END")
            completed.append(position.trade)
            position = None
            if equity_points:
                last = equity_points[-1]
                equity_points[-1] = EquityPoint(last.timestamp, round(cash, 4), last.benchmark_equity, 0.0)

        equity_curve = [self.config.initial_capital] + [point.equity for point in equity_points]
        benchmark_return = None
        if equity_points and equity_points[-1].benchmark_equity is not None:
            benchmark_return = (equity_points[-1].benchmark_equity / self.config.initial_capital - 1.0) * 100.0
        metrics = compute_metrics(
            equity_curve,
            [trade.net_pnl for trade in completed],
            [float(trade.holding_bars) for trade in completed],
            trade_details=completed,
            exposure_periods=exposure_periods,
            minimum_sample_size=self.config.minimum_sample_size,
            benchmark_return_percent=benchmark_return,
        )
        if metrics.sample_warning:
            warnings.append(metrics.sample_warning)
        if excluded:
            warnings.append(f"Veri kalitesi nedeniyle {len(excluded)} donem backtest disinda birakildi.")
        if progress_callback is not None:
            progress_callback(100.0)

        return BacktestResultV5G(
            run_id=run_id,
            data_version=data_version,
            symbol=symbol.upper(),
            config_snapshot=self.config.snapshot(),
            initial_capital=self.config.initial_capital,
            final_equity=round(cash, 2),
            trades=completed,
            equity_points=equity_points,
            metrics=metrics,
            segmented_results=self._segment_results(completed),
            warnings=list(dict.fromkeys(warnings)),
            excluded_periods=excluded,
            out_of_sample=out_of_sample,
        )

    def _prepare_bars(self, bars: pd.DataFrame) -> tuple[pd.DataFrame, list[dict], list[str]]:
        if bars is None or bars.empty:
            raise BacktestValidationError("Fiyat serisi bos.")
        missing = self.REQUIRED_COLUMNS - set(bars.columns)
        if missing:
            raise BacktestValidationError(f"Eksik OHLCV alanlari: {sorted(missing)}")
        frame = bars.copy(deep=True)
        timestamps = pd.to_datetime(frame["timestamp"], errors="raise")
        if timestamps.dt.tz is None:
            timestamps = timestamps.dt.tz_localize(ISTANBUL)
        else:
            timestamps = timestamps.dt.tz_convert(ISTANBUL)
        frame["timestamp"] = timestamps
        frame = frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)

        excluded: list[dict] = []
        keep = pd.Series(True, index=frame.index)
        if "is_complete" in frame.columns:
            incomplete = ~frame["is_complete"].fillna(False).astype(bool)
            for _, row in frame.loc[incomplete].iterrows():
                excluded.append({"timestamp": str(row["timestamp"]), "reason": "INCOMPLETE_CANDLE"})
            keep &= ~incomplete
        if "data_quality" in frame.columns:
            invalid = frame["data_quality"].fillna("VALID").astype(str).str.upper().eq("INVALID")
            for _, row in frame.loc[invalid].iterrows():
                excluded.append({"timestamp": str(row["timestamp"]), "reason": "INVALID_DATA"})
            keep &= ~invalid
        frame = frame.loc[keep].reset_index(drop=True)

        numeric = ["open", "high", "low", "close", "volume"]
        if frame[numeric].isna().any().any() or (frame[["open", "high", "low", "close"]] <= 0).any().any():
            raise BacktestValidationError("OHLCV serisinde gecersiz fiyat var.")
        if ((frame["high"] < frame[["open", "close", "low"]].max(axis=1)) |
                (frame["low"] > frame[["open", "close", "high"]].min(axis=1))).any():
            raise BacktestValidationError("High/low tutarliligi bozuk.")

        if "price_mode" in frame.columns:
            modes = {str(v).strip().lower() for v in frame["price_mode"].dropna().unique()}
            if len(modes) > 1 or (modes and self.config.price_adjustment_mode not in modes):
                raise PriceAdjustmentMismatchError("Raw ve adjusted fiyat serileri karistirilamaz.")
        return frame, excluded, []

    def _prepare_benchmark(self, bars: Optional[pd.DataFrame]) -> Optional[pd.DataFrame]:
        if bars is None or bars.empty:
            return None
        frame = bars.copy(deep=True)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
        if frame["timestamp"].dt.tz is None:
            frame["timestamp"] = frame["timestamp"].dt.tz_localize(ISTANBUL)
        else:
            frame["timestamp"] = frame["timestamp"].dt.tz_convert(ISTANBUL)
        return frame.sort_values("timestamp").reset_index(drop=True)

    def _entry_reference(self, bar: pd.Series) -> float:
        if self.config.entry_model == EntryModel.NEXT_OPEN:
            return float(bar["open"])
        if self.config.entry_model == EntryModel.NEXT_CLOSE:
            return float(bar["close"])
        if "vwap" in bar.index and pd.notna(bar["vwap"]):
            return float(bar["vwap"])
        # Gunluk OHLCV'de gercek VWAP yoksa tipik fiyat kullanilir ve config
        # snapshot'inda next_vwap oldugu acik kalir; uydurma provider verisi yoktur.
        return float((bar["high"] + bar["low"] + bar["close"]) / 3.0)

    def _open_position(
        self, symbol: str, signal: SignalInstruction, bar: pd.Series, cash: float
    ) -> tuple[Optional[_OpenPosition], float]:
        reference = self._entry_reference(bar)
        fill = self.config.transaction_costs.entry_fill(reference)
        max_notional = min(cash, self.config.initial_capital * self.config.max_position_pct / 100.0)
        quantity = math.floor(max_notional / fill)
        while quantity > 0:
            notional = fill * quantity
            explicit_cost = self.config.transaction_costs.cash_cost(notional)
            debit = notional + explicit_cost
            if debit <= cash + 1e-9:
                break
            quantity -= 1
        if quantity <= 0:
            return None, 0.0
        stop = signal.stop_price
        targets = signal.targets
        if stop is not None and stop >= fill:
            return None, 0.0
        normalized_targets = tuple(t if t is not None and t > fill else None for t in targets)
        explicit = self.config.transaction_costs.cash_cost(fill * quantity)
        impact = self.config.transaction_costs.impact_cost(reference, fill, quantity)
        trade = BacktestTradeV5G(
            symbol=symbol.upper(),
            entry_time=pd.Timestamp(bar["timestamp"]).to_pydatetime(),
            entry_price=fill,
            quantity=float(quantity),
            stop_price=stop,
            target_1=normalized_targets[0],
            target_2=normalized_targets[1],
            target_3=normalized_targets[2],
            signal_type=signal.signal_type,
            raw_signal_score=signal.raw_signal_score,
            market_regime=signal.market_regime,
            volatility_regime=signal.volatility_regime,
            liquidity_regime=signal.liquidity_regime,
            sector=signal.sector,
            total_cost=explicit + impact,
        )
        return _OpenPosition(
            trade=trade,
            remaining_quantity=float(quantity),
            entry_outlay=fill * quantity + explicit,
            highest_price=fill,
            lowest_price=fill,
            invalidation_price=signal.invalidation_price,
        ), fill * quantity + explicit

    def _process_intrabar(
        self,
        position: _OpenPosition,
        bar: pd.Series,
        lower_timeframe_resolver: Optional[LowerTimeframeResolver],
        warnings: list[str],
    ) -> tuple[float, bool]:
        stop = position.trade.stop_price
        targets = [position.trade.target_1, position.trade.target_2, position.trade.target_3]
        stop_hit = stop is not None and float(bar["low"]) <= stop
        target_hits = [
            (target is not None and not position.hit_targets[i] and float(bar["high"]) >= target)
            for i, target in enumerate(targets)
        ]
        events: list[str] = []
        if stop_hit and any(target_hits):
            policy = self.config.intrabar_policy
            if policy == IntrabarPolicy.CONSERVATIVE:
                events = ["STOP"]
            elif policy == IntrabarPolicy.OPTIMISTIC:
                events = [f"TARGET_{i + 1}" for i, hit in enumerate(target_hits) if hit]
                events.append("STOP")
            elif policy == IntrabarPolicy.NEAREST_TO_OPEN:
                first_target = next(targets[i] for i, hit in enumerate(target_hits) if hit)
                if abs(float(bar["open"]) - float(stop)) <= abs(float(bar["open"]) - float(first_target)):
                    events = ["STOP"]
                else:
                    events = [f"TARGET_{i + 1}" for i, hit in enumerate(target_hits) if hit]
                    events.append("STOP")
            else:
                if lower_timeframe_resolver is None:
                    warnings.append("Lower-timeframe sirasi yoktu; conservative politika kullanildi.")
                    events = ["STOP"]
                else:
                    events = list(lower_timeframe_resolver(position, bar))
        elif stop_hit:
            events = ["STOP"]
        else:
            events = [f"TARGET_{i + 1}" for i, hit in enumerate(target_hits) if hit]

        cash_delta = 0.0
        for event in events:
            if position.remaining_quantity <= 1e-9:
                break
            if event == "STOP":
                cash_delta += self._close_position(position, float(stop), bar["timestamp"], "STOP")
                return cash_delta, True
            if event.startswith("TARGET_"):
                target_index = int(event.rsplit("_", 1)[1]) - 1
                if not 0 <= target_index <= 2 or position.hit_targets[target_index]:
                    continue
                target = targets[target_index]
                if target is None:
                    continue
                original = position.trade.quantity
                allocation = self.config.target_allocations[target_index]
                quantity = min(position.remaining_quantity, original * allocation)
                if target_index == 2:
                    quantity = position.remaining_quantity
                if quantity > 0:
                    cash_delta += self._partial_exit(position, float(target), quantity, bar["timestamp"], event)
                    position.hit_targets[target_index] = True
                    setattr(position.trade, f"target_{target_index + 1}_hit", True)
                if position.remaining_quantity <= 1e-9:
                    self._finalize_trade(position, bar["timestamp"], float(target), event)
                    return cash_delta, True

        if self.config.trailing_stop_percent is not None and position.remaining_quantity > 0:
            trailing = position.highest_price * (1.0 - self.config.trailing_stop_percent / 100.0)
            if position.trade.stop_price is None or trailing > position.trade.stop_price:
                position.trade.stop_price = trailing
        return cash_delta, False

    def _partial_exit(
        self, position: _OpenPosition, reference: float, quantity: float, timestamp, reason: str
    ) -> float:
        fill = self.config.transaction_costs.exit_fill(reference)
        notional = fill * quantity
        explicit = self.config.transaction_costs.cash_cost(notional)
        impact = self.config.transaction_costs.impact_cost(reference, fill, quantity)
        proceeds = notional - explicit
        position.proceeds += proceeds
        position.remaining_quantity -= quantity
        position.trade.gross_pnl += (reference - position.trade.entry_price) * quantity
        position.trade.total_cost += explicit + impact
        position.trade.partial_exits.append(
            {
                "time": pd.Timestamp(timestamp).isoformat(),
                "reason": reason,
                "quantity": round(quantity, 6),
                "fill_price": round(fill, 6),
            }
        )
        return proceeds

    def _close_position(self, position: _OpenPosition, reference: float, timestamp, reason: str) -> float:
        quantity = position.remaining_quantity
        proceeds = self._partial_exit(position, reference, quantity, timestamp, reason) if quantity > 0 else 0.0
        self._finalize_trade(position, timestamp, reference, reason)
        return proceeds

    def _finalize_trade(self, position: _OpenPosition, timestamp, reference: float, reason: str) -> None:
        trade = position.trade
        trade.exit_time = pd.Timestamp(timestamp).to_pydatetime()
        trade.exit_price = self.config.transaction_costs.exit_fill(reference)
        trade.exit_reason = reason
        trade.net_pnl = round(position.proceeds - position.entry_outlay, 6)
        trade.gross_pnl = round(trade.gross_pnl, 6)
        trade.total_cost = round(trade.total_cost, 6)
        trade.holding_bars = position.bars_held

    @staticmethod
    def _update_excursions(position: _OpenPosition, bar: pd.Series) -> None:
        position.highest_price = max(position.highest_price, float(bar["high"]))
        position.lowest_price = min(position.lowest_price, float(bar["low"]))
        entry = position.trade.entry_price
        position.trade.mfe_percent = max(position.trade.mfe_percent, (position.highest_price / entry - 1.0) * 100.0)
        position.trade.mae_percent = min(position.trade.mae_percent, (position.lowest_price / entry - 1.0) * 100.0)

    @staticmethod
    def _validate_instruction_time(signal: SignalInstruction, context: PointInTimeContext) -> None:
        cutoff = pd.Timestamp(context.as_of)
        for label, value in (("signal_time", signal.signal_time), ("levels_as_of", signal.levels_as_of)):
            if value is None:
                continue
            candidate = pd.Timestamp(value)
            if candidate.tzinfo is None:
                candidate = candidate.tz_localize(ISTANBUL)
            else:
                candidate = candidate.tz_convert(ISTANBUL)
            if candidate > cutoff:
                raise LookAheadBiasError(f"{label} karar anindan sonraya ait.")

    @staticmethod
    def _apply_split(position: _OpenPosition, bar: pd.Series) -> None:
        if "split_factor" not in bar.index or pd.isna(bar["split_factor"]):
            return
        factor = float(bar["split_factor"])
        if factor <= 0 or abs(factor - 1.0) < 1e-12:
            return
        position.remaining_quantity *= factor
        position.trade.quantity *= factor
        position.trade.entry_price /= factor
        position.entry_outlay = position.trade.entry_price * position.trade.quantity + (
            position.entry_outlay - (position.trade.entry_price * factor) * (position.trade.quantity / factor)
        )
        for field_name in ("stop_price", "target_1", "target_2", "target_3"):
            value = getattr(position.trade, field_name)
            if value is not None:
                setattr(position.trade, field_name, value / factor)
        position.highest_price /= factor
        position.lowest_price /= factor

    def _data_version(self, frame: pd.DataFrame) -> str:
        stable = frame[[c for c in frame.columns if c != "fetched_at"]].copy()
        encoded = pd.util.hash_pandas_object(stable, index=True).values.tobytes()
        return hashlib.sha256(encoded).hexdigest()[:24]

    def _run_id(self, symbol: str, data_version: str) -> str:
        payload = json.dumps(
            {"symbol": symbol.upper(), "data_version": data_version, "config": self.config.snapshot()},
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        return "bt5g_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def _benchmark_equity(
        self, benchmark: Optional[pd.DataFrame], initial_price: Optional[float], timestamp
    ) -> Optional[float]:
        if benchmark is None or benchmark.empty or not initial_price:
            return None
        eligible = benchmark.loc[benchmark["timestamp"] <= pd.Timestamp(timestamp)]
        if eligible.empty:
            return None
        close = float(eligible.iloc[-1]["close"])
        return round(self.config.initial_capital * close / initial_price, 4)

    @staticmethod
    def _segment_results(trades: list[BacktestTradeV5G]) -> dict[str, dict[str, dict[str, float]]]:
        dimensions = {
            "market_regime": lambda t: t.market_regime,
            "volatility": lambda t: t.volatility_regime,
            "liquidity": lambda t: t.liquidity_regime,
            "symbol": lambda t: t.symbol,
            "sector": lambda t: t.sector or "unknown",
            "signal_type": lambda t: t.signal_type,
            "score_bin": lambda t: BacktestEngine._score_bin(t.raw_signal_score),
        }
        output: dict[str, dict[str, dict[str, float]]] = {}
        for dimension, key_fn in dimensions.items():
            groups: dict[str, list[BacktestTradeV5G]] = {}
            for trade in trades:
                groups.setdefault(str(key_fn(trade)), []).append(trade)
            output[dimension] = {
                key: {
                    "trade_count": float(len(items)),
                    "net_pnl": round(sum(t.net_pnl for t in items), 4),
                    "win_rate_percent": round(sum(t.net_pnl > 0 for t in items) / len(items) * 100, 2),
                }
                for key, items in groups.items()
            }
        return output

    @staticmethod
    def _score_bin(score: Optional[float]) -> str:
        if score is None:
            return "unknown"
        for low, high in ((0, 39), (40, 49), (50, 59), (60, 69), (70, 79), (80, 89), (90, 100)):
            if low <= score <= high:
                return f"{low}-{high}"
        return "out_of_range"
