from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.analysis.data_quality import DataQualityEngine, DataQualityStatus
from app.config.settings import Settings


def _frame(periods: int = 80, *, end=None, tz="UTC") -> pd.DataFrame:
    end = end or (datetime.now(timezone.utc) - timedelta(days=1))
    # Pandas 3 may count a weekend ``end`` as the first period and return one
    # row short. Roll it back explicitly so the fixture is calendar-independent.
    business_end = pd.offsets.BDay().rollback(pd.Timestamp(end))
    dates = pd.bdate_range(end=business_end, periods=periods, tz=tz)
    close = np.linspace(90.0, 100.0, periods)
    return pd.DataFrame(
        {
            "timestamp": dates,
            "open": close - 0.2,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(periods, 1_000_000.0),
        }
    )


def test_quality_empty_data_is_incomplete():
    result = DataQualityEngine().evaluate(pd.DataFrame(), min_bars=20)
    assert result.status == DataQualityStatus.INCOMPLETE
    assert result.score == 0
    assert not result.usable_for_analysis


def test_quality_missing_ohlc_is_invalid():
    result = DataQualityEngine().evaluate(_frame().drop(columns=["high"]), min_bars=20)
    assert result.status == DataQualityStatus.INVALID


def test_quality_duplicate_timestamp_is_invalid():
    df = _frame()
    df.loc[3, "timestamp"] = df.loc[2, "timestamp"]
    result = DataQualityEngine().evaluate(df, min_bars=20)
    assert result.duplicate_bar_count == 1
    assert result.status == DataQualityStatus.INVALID


def test_quality_stale_data_is_rejected():
    old_end = datetime.now(timezone.utc) - timedelta(days=30)
    result = DataQualityEngine().evaluate(
        _frame(end=old_end), min_bars=20, max_staleness_minutes=60
    )
    assert result.status == DataQualityStatus.STALE
    assert not result.usable_for_analysis


def test_quality_incomplete_intraday_candle_is_detected():
    df = _frame(end=datetime.now(timezone.utc), tz="UTC")
    df["timestamp"] = pd.date_range(
        end=datetime.now(timezone.utc), periods=len(df), freq="15min", tz="UTC"
    )
    result = DataQualityEngine().evaluate(
        df, min_bars=20, timeframe="15m", check_incomplete=True
    )
    assert result.status == DataQualityStatus.INCOMPLETE
    assert result.incomplete_bar_count == 1


def test_quality_invalid_high_low_is_rejected():
    df = _frame()
    df.loc[5, "high"] = df.loc[5, "low"] - 1
    result = DataQualityEngine().evaluate(df, min_bars=20)
    assert result.status == DataQualityStatus.INVALID


def test_quality_negative_volume_is_rejected():
    df = _frame()
    df.loc[4, "volume"] = -1
    result = DataQualityEngine().evaluate(df, min_bars=20)
    assert result.status == DataQualityStatus.INVALID


def test_quality_timezone_is_normalized_to_utc():
    df = _frame(tz=None)
    df["timestamp"] = df["timestamp"].dt.tz_localize(None)
    result = DataQualityEngine().evaluate(df, min_bars=20)
    assert result.status == DataQualityStatus.DEGRADED
    assert str(result.cleaned_df["timestamp"].dt.tz) == "UTC"


def test_quality_missing_business_day_is_reported():
    df = _frame()
    df = df.drop(index=20).reset_index(drop=True)
    result = DataQualityEngine().evaluate(df, min_bars=20, timeframe="1d")
    assert result.missing_bar_count >= 1
    assert result.status == DataQualityStatus.DEGRADED


def test_quality_unexplained_price_jump_is_outlier():
    df = _frame()
    df.loc[40, ["open", "high", "low", "close"]] = [190.0, 205.0, 185.0, 200.0]
    result = DataQualityEngine().evaluate(df, min_bars=20)
    assert result.outlier_count >= 1
    assert result.status == DataQualityStatus.DEGRADED


def test_quality_split_jump_is_not_anomaly():
    df = _frame()
    df.loc[40:, ["open", "high", "low", "close"]] = df.loc[40:, ["open", "high", "low", "close"]] * 2
    action_date = df.loc[40, "timestamp"]
    result = DataQualityEngine().evaluate(
        df,
        min_bars=20,
        corporate_actions=[{"type": "split", "date": action_date, "ratio": "2:1"}],
    )
    assert result.outlier_count == 0
    assert any("split/temettü" in warning for warning in result.warnings)


def test_quality_daily_intraday_mismatch_warns():
    intraday = _frame()
    intraday["timestamp"] = pd.date_range("2026-01-05", periods=len(intraday), freq="15min", tz="UTC")
    daily = _frame(end=datetime(2025, 1, 1, tzinfo=timezone.utc))
    result = DataQualityEngine().evaluate(
        intraday, min_bars=20, timeframe="15m", daily_reference=daily
    )
    assert any("ortak işlem günü" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [("SVGYO", "SVGYO.IS"), ("svgyo.is", "SVGYO.IS"), ("XU100", "^XU100")],
)
def test_quality_symbol_normalization(symbol, expected):
    result = DataQualityEngine().evaluate(_frame(), min_bars=20, symbol=symbol)
    assert result.normalized_symbol == expected


def test_quality_completed_candles_drops_open_intraday_bar():
    df = _frame()
    df["timestamp"] = pd.date_range(
        end=datetime.now(timezone.utc), periods=len(df), freq="15min", tz="UTC"
    )
    complete = DataQualityEngine().completed_candles(df, "15m")
    assert len(complete) == len(df) - 1


def test_quality_provider_down_result():
    from app.analysis.data_quality import DataQualityResult

    result = DataQualityResult.provider_down("test", "bağlantı yok")
    assert result.status == DataQualityStatus.PROVIDER_DOWN
    assert result.provider == "test"


def test_production_forbids_mock_provider():
    with pytest.raises(ValueError, match="mock"):
        Settings(app_env="production", market_data_provider="mock")


def test_development_allows_mock_provider():
    settings = Settings(app_env="development", market_data_provider="mock")
    assert settings.market_data_provider == "mock"
