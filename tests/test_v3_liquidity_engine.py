from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app.analysis.liquidity_engine import (
    LIQUIDITY_HIGH,
    LIQUIDITY_LOW,
    LIQUIDITY_VERY_HIGH,
    LIQUIDITY_VERY_LOW,
    compute_liquidity,
)


def _make_df(periods=80, base_price=20.0, base_volume=1_000_000, vol_noise=0.05, price_noise=0.01, seed=1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=periods, freq="1B", tz="UTC")
    returns = rng.normal(0.0003, price_noise, size=periods)
    closes = base_price * np.cumprod(1 + returns)
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    highs = closes * (1 + np.abs(rng.normal(0, 0.005, size=periods)))
    lows = closes * (1 - np.abs(rng.normal(0, 0.005, size=periods)))
    volumes = np.abs(rng.normal(base_volume, base_volume * vol_noise, size=periods))
    return pd.DataFrame(
        {"timestamp": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes}
    )


def test_insufficient_data_returns_unavailable():
    df = _make_df(periods=10)
    result = compute_liquidity(df)
    assert result.available is False
    assert result.liquidity_class == "veri_yetersiz"


def test_high_volume_high_turnover_scores_well():
    df = _make_df(periods=100, base_price=50.0, base_volume=5_000_000, vol_noise=0.05)
    result = compute_liquidity(df)
    assert result.available is True
    assert result.score >= 60
    assert result.liquidity_class in (LIQUIDITY_HIGH, LIQUIDITY_VERY_HIGH)
    assert result.allow_strong_signal is True


def test_low_volume_low_turnover_scores_poorly_and_blocks_strong_signal():
    df = _make_df(periods=100, base_price=5.0, base_volume=5_000, vol_noise=0.3, price_noise=0.02)
    result = compute_liquidity(df)
    assert result.available is True
    assert result.score < 45
    assert result.liquidity_class in (LIQUIDITY_LOW, LIQUIDITY_VERY_LOW, "orta")
    assert result.allow_strong_signal is False
    assert result.risk_note != "" or result.liquidity_class == "orta"


def test_declining_volume_is_detected():
    rng = np.random.default_rng(5)
    periods = 90
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=periods, freq="1B", tz="UTC")
    closes = 20 + np.cumsum(rng.normal(0, 0.1, size=periods))
    opens = np.roll(closes, 1)
    opens[0] = closes[0]
    highs = closes + 0.2
    lows = closes - 0.2
    # Ilk 60 gun yuksek hacim, son 20 gun cok dusuk hacim -> belirgin dusus.
    volumes = np.concatenate([
        np.abs(rng.normal(2_000_000, 100_000, size=70)),
        np.abs(rng.normal(300_000, 50_000, size=20)),
    ])
    df = pd.DataFrame({"timestamp": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes})
    result = compute_liquidity(df)
    assert result.available is True
    assert result.volume_declining is True


def test_abnormal_volume_flagged():
    df = _make_df(periods=80, base_price=20.0, base_volume=1_000_000, vol_noise=0.05)
    df.loc[df.index[-1], "volume"] = df["volume"].iloc[:-1].mean() * 5
    result = compute_liquidity(df)
    assert result.abnormal_volume is True


def test_custom_config_thresholds_applied():
    df = _make_df(periods=100, base_price=20.0, base_volume=200_000, vol_noise=0.05)
    default_result = compute_liquidity(df)
    strict_result = compute_liquidity(df, config={"minimum_average_volume": 10_000_000})
    # Ayni veri, cok daha yuksek bir minimum hacim esigiyle degerlendirilince skor dusmeli.
    assert strict_result.score <= default_result.score
