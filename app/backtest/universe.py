from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from app.backtest.engine_v5g import (
    BacktestConfig, BacktestEngine, BacktestResultV5G, SignalInstruction, SignalProvider,
)


@dataclass(frozen=True)
class UniverseBacktestRequest:
    scope: str  # symbol | watchlist | sector | all_bist
    start_date: datetime
    end_date: datetime
    symbols: tuple[str, ...] = ()
    sector: Optional[str] = None
    signal_type: Optional[str] = None
    market_regime: Optional[str] = None
    strategy_name: str = "existing_signal_engine"

    def __post_init__(self) -> None:
        normalized = self.scope.strip().lower()
        if normalized not in {"symbol", "watchlist", "sector", "all_bist"}:
            raise ValueError("Backtest kapsami symbol/watchlist/sector/all_bist olmali.")
        if self.start_date >= self.end_date:
            raise ValueError("Backtest tarih araligi gecersiz.")
        object.__setattr__(self, "scope", normalized)


@dataclass
class UniverseBacktestResult:
    request: UniverseBacktestRequest
    results: dict[str, BacktestResultV5G] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)


DataLoader = Callable[[str, datetime, datetime], pd.DataFrame]
SignalProviderFactory = Callable[[str], SignalProvider]


class UniverseBacktestEngine:
    """Tek motoru sembol, watchlist, sektor veya desteklenen BIST evrenine uygular."""

    def __init__(
        self,
        config: BacktestConfig,
        *,
        symbol_to_sector: Optional[dict[str, str]] = None,
        bist_symbols_path: str | Path = "data/symbols/bist_symbols.csv",
    ):
        self.config = config
        self.symbol_to_sector = {key.upper(): value for key, value in (symbol_to_sector or {}).items()}
        self.bist_symbols_path = Path(bist_symbols_path)

    def resolve_symbols(self, request: UniverseBacktestRequest) -> list[str]:
        provided = list(dict.fromkeys(symbol.upper() for symbol in request.symbols))
        if request.scope == "symbol":
            if len(provided) != 1:
                raise ValueError("Tek sembol kapsami tam bir sembol gerektirir.")
            return provided
        if request.scope == "watchlist":
            if not provided:
                raise ValueError("Izleme listesi bos.")
            return provided
        if request.scope == "sector":
            if not request.sector:
                raise ValueError("Sektor kapsami sektor adi gerektirir.")
            universe = provided or list(self.symbol_to_sector)
            return [symbol for symbol in universe if self.symbol_to_sector.get(symbol) == request.sector]
        if provided:
            return provided
        if not self.bist_symbols_path.exists():
            raise ValueError("Desteklenen BIST sembol evreni dosyasi bulunamadi.")
        frame = pd.read_csv(self.bist_symbols_path)
        column = "symbol" if "symbol" in frame.columns else frame.columns[0]
        return list(dict.fromkeys(str(value).strip().upper() for value in frame[column] if str(value).strip()))

    def run(
        self,
        request: UniverseBacktestRequest,
        *,
        data_loader: DataLoader,
        signal_provider_factory: SignalProviderFactory,
        benchmark_loader: Optional[DataLoader] = None,
    ) -> UniverseBacktestResult:
        output = UniverseBacktestResult(request=request)
        benchmark = benchmark_loader("XU100.IS", request.start_date, request.end_date) if benchmark_loader else None
        for symbol in self.resolve_symbols(request):
            try:
                bars = data_loader(symbol, request.start_date, request.end_date)
                base_provider = signal_provider_factory(symbol)

                def filtered_provider(context, provider=base_provider):
                    instruction = provider(context) or SignalInstruction()
                    if request.signal_type and instruction.signal_type != request.signal_type:
                        return SignalInstruction()
                    if request.market_regime and instruction.market_regime != request.market_regime:
                        return SignalInstruction()
                    return instruction

                output.results[symbol] = BacktestEngine(self.config).run(
                    bars, symbol, filtered_provider, benchmark_bars=benchmark
                )
            except Exception as exc:
                output.failures[symbol] = f"{type(exc).__name__}: {exc}"
        return output
