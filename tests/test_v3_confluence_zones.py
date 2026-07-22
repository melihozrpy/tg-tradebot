from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analysis.confluence_zone_engine import find_confluence_zones
from app.analysis.timeframe_levels_engine import compute_timeframe_levels


def _oscillating_df(n_days: int = 400, floor: float = 90.0, ceiling: float = 110.0, seed: int = 11) -> pd.DataFrame:
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
        {"timestamp": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes}
    )


@pytest.fixture(scope="module")
def sample_df() -> pd.DataFrame:
    return _oscillating_df()


def test_confluence_support_zone_found(sample_df):
    current_price = float(sample_df["close"].iloc[-1])
    levels = compute_timeframe_levels(sample_df, current_price)
    supports, _ = find_confluence_zones(levels, current_price)
    assert len(supports) >= 1
    zone = supports[0]
    assert zone.kind == "destek"
    assert len(set(zone.timeframes)) >= 2
    assert zone.low <= zone.mid <= zone.high


def test_confluence_resistance_zone_found(sample_df):
    current_price = float(sample_df["close"].iloc[-1])
    levels = compute_timeframe_levels(sample_df, current_price)
    _, resistances = find_confluence_zones(levels, current_price)
    assert len(resistances) >= 1
    zone = resistances[0]
    assert zone.kind == "direnc"
    assert len(set(zone.timeframes)) >= 2


def test_confluence_confidence_not_artificially_maxed(sample_df):
    """Cakisan bolge puani asla yapay olarak 100'e tamamlanmaz (ust sinir
    97), ancak birden fazla zaman diliminin dogrulamasi sayesinde tekil
    bir seviyeden daha yuksek olabilir."""
    current_price = float(sample_df["close"].iloc[-1])
    levels = compute_timeframe_levels(sample_df, current_price)
    supports, resistances = find_confluence_zones(levels, current_price)
    assert supports or resistances
    for zone_list in (supports, resistances):
        for zone in zone_list:
            assert zone.confidence <= 97.0


def test_confluence_requires_at_least_two_timeframes():
    """Tek bir zaman diliminden gelen (digerleriyle cakismayan) seviyeler
    'cakisan guclu bolge' olarak raporlanmamalidir."""
    from app.analysis.confluence_zone_engine import _find_confluences
    from app.analysis.timeframe_levels_engine import LevelDetail

    single_tf_levels = [
        LevelDetail(low=99.0, high=101.0, mid=100.0, confidence=60.0, touches=3, rejections=1,
                    last_test_date="2026-01-01", sources=["swing_low"], volume_confirmed=True, timeframe="gunluk"),
    ]
    zones = _find_confluences(single_tf_levels, "destek", current_price=105.0)
    assert zones == []


def test_no_confluence_when_no_reliable_levels():
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
    levels = compute_timeframe_levels(tiny_df, current_price=10.0)
    supports, resistances = find_confluence_zones(levels, 10.0)
    assert supports == []
    assert resistances == []
