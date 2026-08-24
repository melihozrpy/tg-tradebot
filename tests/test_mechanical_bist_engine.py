from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from app.analysis.mechanical_bist_engine import analyze_mechanical_bist_setup, format_mechanical_bist_report


def _frame(periods: int, frequency: str, *, base: float, slope: float) -> pd.DataFrame:
    close = base + np.arange(periods, dtype=float) * slope
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-02", periods=periods, freq=frequency, tz="UTC"),
            "open": close - 0.35,
            "high": close + 0.7,
            "low": close - 0.7,
            "close": close,
            "volume": np.full(periods, 5_000_000.0),
        }
    )


class _Provider:
    def __init__(self) -> None:
        self.daily = _frame(180, "D", base=100.0, slope=0.4)
        self.hourly = _frame(360, "h", base=100.0, slope=0.05)
        self.minute = _frame(360, "15min", base=100.0, slope=0.04)
        # Last 15m candle closes beyond and retests the previous 1H high.
        previous_hour_high = float(self.hourly.iloc[-2].high)
        self.minute.loc[self.minute.index[-1], ["open", "low", "high", "close"]] = [
            previous_hour_high - 0.15,
            previous_hour_high - 0.04,
            previous_hour_high + 0.5,
            previous_hour_high + 0.2,
        ]

    def get_ohlcv(self, _symbol, timeframe, _start, _end):
        return {"1d": self.daily, "1h": self.hourly, "15m": self.minute}[timeframe].copy()


def test_mechanical_engine_returns_coordinate_rich_json_payload() -> None:
    result = analyze_mechanical_bist_setup(
        "THYAO",
        provider=_Provider(),
        settings=SimpleNamespace(mechanical_setup_minimum_liquidity_score=65.0, mechanical_setup_risk_per_trade_percent=0.25),
    )

    assert result["schema_version"] == "bist-mechanical-setup/v1"
    assert result["timeframe_hierarchy"]["1D"]["bias"] == "UP"
    assert result["structural_levels"]
    assert all({"label", "x", "y"}.issubset(level) for level in result["structural_levels"])
    assert result["liquidity"]["gate"] == "PASS"
    assert result["status"] in {"ACTIVE", "WAIT"}
    if result["signal"] is not None:
        assert result["signal"]["entry_type"] in {"SWEEP", "BREAK_RETEST"}
        assert "coordinates" in result["signal"]
        assert result["signal"]["position_risk_percent"] == 0.25


def test_mechanical_engine_blocks_when_liquidity_threshold_is_unmet() -> None:
    result = analyze_mechanical_bist_setup(
        "THYAO",
        provider=_Provider(),
        settings=SimpleNamespace(mechanical_setup_minimum_liquidity_score=101.0, mechanical_setup_risk_per_trade_percent=0.25),
    )

    assert result["status"] == "WAIT"
    assert result["signal"] is None
    assert result["liquidity"]["gate"] == "BLOCKED"


def test_mechanical_telegram_card_hides_internal_json_and_explains_wait() -> None:
    result = analyze_mechanical_bist_setup(
        "THYAO",
        provider=_Provider(),
        settings=SimpleNamespace(mechanical_setup_minimum_liquidity_score=101.0, mechanical_setup_risk_per_trade_percent=0.25),
    )

    card = format_mechanical_bist_report(result)

    assert "MEKANİK İŞLEM PLANI" in card
    assert "ŞU AN İŞLEM YOK" in card
    assert "<pre>" not in card
    assert '"schema_version"' not in card
    assert "BEKLENECEK TEYİT" in card
