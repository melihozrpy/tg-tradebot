from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analysis.timeframe_levels_engine import (
    TIMEFRAME_DAILY,
    TIMEFRAME_MONTHLY,
    TIMEFRAME_WEEKLY,
    compute_timeframe_levels,
)


def _oscillating_df(n_days: int = 400, floor: float = 90.0, ceiling: float = 110.0, seed: int = 7) -> pd.DataFrame:
    """Floor/ceiling arasinda tekrar tekrar sekerek belirgin destek/direnc
    yapisi olusturan sentetik gunluk OHLCV serisi uretir (haftasonlari
    haric, is gunleri)."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n_days, tz="UTC")
    mid = (floor + ceiling) / 2
    amplitude = (ceiling - floor) / 2
    closes = mid + amplitude * 0.85 * np.sin(np.linspace(0, 14 * np.pi, n_days)) + rng.normal(0, 0.3, n_days)
    closes = np.clip(closes, floor + 0.5, ceiling - 0.5)
    highs = closes + rng.uniform(0.2, 1.0, n_days)
    lows = closes - rng.uniform(0.2, 1.0, n_days)
    opens = closes + rng.normal(0, 0.3, n_days)
    volumes = rng.uniform(800_000, 1_200_000, n_days)

    return pd.DataFrame(
        {
            "timestamp": dates,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


@pytest.fixture(scope="module")
def sample_df() -> pd.DataFrame:
    return _oscillating_df()


def test_daily_support_resistance_reliable(sample_df):
    current_price = float(sample_df["close"].iloc[-1])
    result = compute_timeframe_levels(sample_df, current_price)
    assert result.daily.timeframe == TIMEFRAME_DAILY
    assert result.daily.reliable is True
    assert result.daily.note == ""
    assert result.daily.support_1 is not None or result.daily.resistance_1 is not None


def test_weekly_support_resistance_reliable(sample_df):
    current_price = float(sample_df["close"].iloc[-1])
    result = compute_timeframe_levels(sample_df, current_price)
    assert result.weekly.timeframe == TIMEFRAME_WEEKLY
    assert result.weekly.reliable is True


def test_monthly_support_resistance_reliable(sample_df):
    current_price = float(sample_df["close"].iloc[-1])
    result = compute_timeframe_levels(sample_df, current_price)
    assert result.monthly.timeframe == TIMEFRAME_MONTHLY
    assert result.monthly.reliable is True
    # Spesifikasyon geregi aylikta ikincil (Destek 2 / Direnc 2) seviye yok.
    assert result.monthly.support_2 is None
    assert result.monthly.resistance_2 is None


def test_timeframes_are_not_mixed(sample_df):
    """Her zaman diliminin seviyeleri kendi timeframe etiketini tasir ve
    farkli zaman dilimlerinin sonuclari birbirine karismaz."""
    current_price = float(sample_df["close"].iloc[-1])
    result = compute_timeframe_levels(sample_df, current_price)
    for tf_result, expected_label in (
        (result.daily, TIMEFRAME_DAILY),
        (result.weekly, TIMEFRAME_WEEKLY),
        (result.monthly, TIMEFRAME_MONTHLY),
    ):
        for lvl in (
            tf_result.support_1,
            tf_result.support_2,
            tf_result.main_support,
            tf_result.resistance_1,
            tf_result.resistance_2,
            tf_result.main_resistance,
        ):
            if lvl is not None:
                assert lvl.timeframe == expected_label

    # Ayrica gunluk ve haftalik seviye kumeleri, ayni objeler olmamalidir.
    assert result.daily is not result.weekly
    assert result.weekly is not result.monthly


def test_insufficient_data_marks_unreliable():
    tiny_df = pd.DataFrame(
        {
            "timestamp": pd.bdate_range(end=pd.Timestamp.today(), periods=5, tz="UTC"),
            "open": [10.0] * 5,
            "high": [10.5] * 5,
            "low": [9.5] * 5,
            "close": [10.0] * 5,
            "volume": [1000.0] * 5,
        }
    )
    result = compute_timeframe_levels(tiny_df, current_price=10.0)
    assert result.daily.reliable is False
    assert "Guvenilir seviye hesaplanamadi" in result.daily.note
    assert result.weekly.reliable is False
    assert result.monthly.reliable is False


def test_no_unclosed_period_is_used(sample_df):
    """Icinde bulunulan hafta/ay tamamlanmadan kesinlesmis seviyeye
    girmemeli: son haftalik/aylik son_test_tarihi, elimizdeki son gunluk
    bardan ILERI bir tarih olamaz."""
    current_price = float(sample_df["close"].iloc[-1])
    last_daily_date = sample_df["timestamp"].max()
    result = compute_timeframe_levels(sample_df, current_price)
    for tf_result in (result.weekly, result.monthly):
        for lvl in (tf_result.support_1, tf_result.resistance_1):
            if lvl is not None and lvl.last_test_date:
                assert pd.Timestamp(lvl.last_test_date, tz="UTC") <= last_daily_date
