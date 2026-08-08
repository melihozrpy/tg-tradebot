from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis.screener_engine import (
    analyze_symbol_frame,
    format_trade_scenario_report,
    run_intraday_trade_scenario_scan,
    run_technical_screener,
)
from app.models.database import Base
from app.telegram.bot import _build_evening_scan_scheduler


def _last_bar_cross(*, upward: bool) -> pd.DataFrame:
    count = 140
    close = np.full(count, 100.0)
    close[-1] = 115.0 if upward else 85.0
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=count, freq="D", tz="UTC"),
            "open": close - (0.2 if upward else -0.2),
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.linspace(1_000_000, 2_000_000, count),
        }
    )


class _Provider:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame

    def get_ohlcv(self, symbol, timeframe, start, end):
        return self.frame.copy()


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        rsi_overbought=75.0,
        rsi_oversold=25.0,
        technical_screener_min_confluence=3,
        technical_screener_workers=2,
        technical_screener_max_symbols_per_run=1000,
        trade_scenario_max_results=6,
    )


def test_last_bar_ema50_100_cross_is_detected() -> None:
    golden = analyze_symbol_frame("THYAO", _last_bar_cross(upward=True))
    death = analyze_symbol_frame("THYAO", _last_bar_cross(upward=False))
    assert golden.crossover == "golden"
    assert death.crossover == "death"


def test_same_cross_is_persisted_and_not_alerted_twice() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    provider = _Provider(_last_bar_cross(upward=True))
    try:
        first = run_technical_screener(
            db,
            symbols=["THYAO"],
            provider_factory=lambda: provider,
            settings=_settings(),
        )
        second = run_technical_screener(
            db,
            symbols=["THYAO"],
            provider_factory=lambda: provider,
            settings=_settings(),
        )
        assert len(first.alerts) == 1
        assert first.alerts[0].kind == "golden_cross"
        assert second.alerts == ()
    finally:
        db.close()


def test_intraday_scenario_requires_confluence_and_uses_retest_entry() -> None:
    # The upward cross makes Supertrend, EMA direction, MACD and OBV line up.
    # The scenario's entry must be an EMA/VWAP zone, never blindly the close.
    frame = _last_bar_cross(upward=True)
    frame["timestamp"] = pd.date_range("2025-01-01", periods=len(frame), freq="15min", tz="UTC")
    result = run_intraday_trade_scenario_scan(
        symbols=["THYAO"],
        provider_factory=lambda: _Provider(frame),
        settings=_settings(),
    )
    assert result.scenarios
    scenario = result.scenarios[0]
    assert scenario.confirmation_count >= 3
    assert scenario.entry_low <= scenario.entry_high
    assert scenario.stop < scenario.entry_low
    assert "15 DK FIRSAT RADARI" in format_trade_scenario_report(result)


def test_new_scanner_jobs_use_istanbul_market_hours() -> None:
    settings = SimpleNamespace(
        close_scan_enabled=False,
        timezone_name="Europe/Istanbul",
        technical_screener_enabled=True,
        technical_screener_interval_minutes=30,
        intraday_vwap_scan_enabled=True,
        intraday_vwap_scan_minute_step=30,
        trade_scenario_scan_enabled=True,
        trade_scenario_scan_minutes=15,
        trade_scenario_max_results=6,
        user_price_alerts_enabled=False,
        enhanced_alarm_scan_enabled=False,
        signal_monitor_enabled=False,
    )
    scheduler = _build_evening_scan_scheduler(settings)
    scenario_job = scheduler.get_job("full_universe_trade_scenario_scan")
    assert scenario_job is not None
    assert scheduler.get_job("full_universe_ema_rsi_scan") is None
    assert scheduler.get_job("full_universe_vwap_volume_profile_scan") is None
    assert str(scenario_job.trigger.timezone) == "Europe/Istanbul"
