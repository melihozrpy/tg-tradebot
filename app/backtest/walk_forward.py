from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Callable, Optional

import pandas as pd

from app.backtest.engine_v5g import BacktestConfig, BacktestEngine, BacktestResultV5G, SignalProvider


@dataclass(frozen=True)
class WalkForwardConfig:
    train_days: int = 504
    validation_days: int = 126
    test_days: int = 126
    step_days: int = 126
    mode: str = "rolling"
    seed: int = 42

    def __post_init__(self) -> None:
        normalized = self.mode.strip().lower()
        if normalized not in {"rolling", "expanding"}:
            raise ValueError("Walk-forward modu rolling veya expanding olmali.")
        object.__setattr__(self, "mode", normalized)
        if min(self.train_days, self.validation_days, self.test_days, self.step_days) < 1:
            raise ValueError("Walk-forward gun sayilari pozitif olmali.")


@dataclass
class WalkForwardWindow:
    index: int
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    test_start: datetime
    test_end: datetime
    train_data: pd.DataFrame = field(repr=False)
    validation_data: pd.DataFrame = field(repr=False)
    test_data: pd.DataFrame = field(repr=False)
    selected_parameters: dict = field(default_factory=dict)
    out_of_sample_result: Optional[BacktestResultV5G] = None


@dataclass
class WalkForwardResult:
    run_id: str
    mode: str
    seed: int
    windows: list[WalkForwardWindow]
    out_of_sample_results: list[BacktestResultV5G]
    config_snapshot: dict

    @property
    def out_of_sample_trade_count(self) -> int:
        return sum(len(item.trades) for item in self.out_of_sample_results)


ParameterSelector = Callable[[pd.DataFrame, pd.DataFrame], dict]


class WalkForwardEngine:
    """Egitim/dogrulama/test sinirlarini ayirir ve yalnizca test OOS raporlar."""

    def __init__(self, config: WalkForwardConfig | None = None):
        self.config = config or WalkForwardConfig()

    def split(self, bars: pd.DataFrame) -> list[WalkForwardWindow]:
        if bars is None or bars.empty:
            return []
        frame = bars.copy(deep=True)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
        frame = frame.sort_values("timestamp").reset_index(drop=True)
        first = pd.Timestamp(frame.iloc[0]["timestamp"])
        last = pd.Timestamp(frame.iloc[-1]["timestamp"])
        windows: list[WalkForwardWindow] = []
        anchor = first
        index = 0
        while True:
            train_start = first if self.config.mode == "expanding" else anchor
            train_end = anchor + pd.Timedelta(days=self.config.train_days)
            validation_start = train_end
            validation_end = validation_start + pd.Timedelta(days=self.config.validation_days)
            test_start = validation_end
            test_end = test_start + pd.Timedelta(days=self.config.test_days)
            if test_end > last:
                break

            train = frame.loc[(frame["timestamp"] >= train_start) & (frame["timestamp"] < train_end)].copy()
            validation = frame.loc[
                (frame["timestamp"] >= validation_start) & (frame["timestamp"] < validation_end)
            ].copy()
            test = frame.loc[(frame["timestamp"] >= test_start) & (frame["timestamp"] <= test_end)].copy()
            if not train.empty and not validation.empty and not test.empty:
                windows.append(
                    WalkForwardWindow(
                        index=index,
                        train_start=train_start.to_pydatetime(),
                        train_end=train_end.to_pydatetime(),
                        validation_start=validation_start.to_pydatetime(),
                        validation_end=validation_end.to_pydatetime(),
                        test_start=test_start.to_pydatetime(),
                        test_end=test_end.to_pydatetime(),
                        train_data=train,
                        validation_data=validation,
                        test_data=test,
                    )
                )
                index += 1
            anchor += pd.Timedelta(days=self.config.step_days)
        return windows

    def run(
        self,
        bars: pd.DataFrame,
        symbol: str,
        signal_provider: SignalProvider,
        *,
        backtest_config: BacktestConfig | None = None,
        parameter_selector: Optional[ParameterSelector] = None,
        benchmark_bars: Optional[pd.DataFrame] = None,
    ) -> WalkForwardResult:
        windows = self.split(bars)
        results: list[BacktestResultV5G] = []
        base_config = backtest_config or BacktestConfig(seed=self.config.seed)
        for window in windows:
            # Selector'a test verisi kesinlikle verilmez. Secim sadece train ve
            # validation dilimlerinde yapilir.
            selected = parameter_selector(window.train_data.copy(), window.validation_data.copy()) if parameter_selector else {}
            window.selected_parameters = dict(selected)
            allowed = {key: value for key, value in selected.items() if key in BacktestConfig.__dataclass_fields__}
            engine_config = replace(base_config, **allowed) if allowed else base_config
            benchmark_test = None
            if benchmark_bars is not None and not benchmark_bars.empty:
                benchmark = benchmark_bars.copy()
                benchmark["timestamp"] = pd.to_datetime(benchmark["timestamp"])
                benchmark_test = benchmark.loc[
                    (benchmark["timestamp"] >= pd.Timestamp(window.test_start))
                    & (benchmark["timestamp"] <= pd.Timestamp(window.test_end))
                ].copy()
            result = BacktestEngine(engine_config).run(
                window.test_data,
                symbol,
                signal_provider,
                benchmark_bars=benchmark_test,
                out_of_sample=True,
            )
            window.out_of_sample_result = result
            results.append(result)

        snapshot = {
            "walk_forward": self.config.__dict__,
            "backtest": base_config.snapshot(),
        }
        encoded = json.dumps(snapshot, sort_keys=True, default=str, separators=(",", ":"))
        run_id = "wf5g_" + hashlib.sha256((symbol.upper() + encoded).encode()).hexdigest()[:24]
        return WalkForwardResult(
            run_id=run_id,
            mode=self.config.mode,
            seed=self.config.seed,
            windows=windows,
            out_of_sample_results=results,
            config_snapshot=snapshot,
        )
