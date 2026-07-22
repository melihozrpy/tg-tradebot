from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analysis.support_resistance_engine import LevelZone, _cluster_levels
from app.analysis.timeframe_levels_engine import (
    TIMEFRAME_DAILY,
    _build_zone_detail,
    compute_timeframe_levels,
)


def _oscillating(n=420, floor=90.0, ceiling=110.0, seed=55):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n, tz="UTC")
    close = 100 + 8.5 * np.sin(np.linspace(0, 16 * np.pi, n)) + rng.normal(0, 0.2, n)
    close = np.clip(close, floor + 0.4, ceiling - 0.4)
    return pd.DataFrame(
        {
            "timestamp": dates,
            "open": close + rng.normal(0, 0.2, n),
            "high": close + rng.uniform(0.3, 0.9, n),
            "low": close - rng.uniform(0.3, 0.9, n),
            "close": close,
            "volume": rng.uniform(900_000, 1_500_000, n),
        }
    )


def _flat_frame(n=100, volume=1_000_000.0):
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n, tz="UTC")
    return pd.DataFrame(
        {"timestamp": dates, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": volume}
    )


@pytest.mark.parametrize("timeframe_attr", ["daily", "weekly", "monthly"])
def test_level_outputs_have_professional_fields(timeframe_attr):
    df = _oscillating()
    result = compute_timeframe_levels(df, float(df.iloc[-1]["close"]))
    tf = getattr(result, timeframe_attr)
    level = tf.support_1 or tf.resistance_1
    assert level is not None
    assert 0 <= level.confidence <= 97
    assert level.strength_class in {"Çok güçlü", "Güçlü", "Orta", "Zayıf", "Veri yetersiz"}
    assert level.invalidation_condition


def test_near_levels_are_clustered_by_atr():
    zones = _cluster_levels([(100.0, "a"), (100.2, "b"), (105.0, "c")], atr=1.0, current_price=100)
    assert len(zones) == 2
    assert zones[0].votes == 2
    assert set(zones[0].sources) == {"a", "b"}


def test_same_level_not_repeated_in_all_zones():
    df = _oscillating()
    result = compute_timeframe_levels(df, float(df.iloc[-1]["close"]))
    keys = [(level.timeframe, round(level.mid, 2)) for level in result.all_zones()]
    assert len(keys) == len(set(keys))


def test_more_touches_increase_detailed_confidence():
    df_many = _flat_frame()
    df_many.loc[::10, "low"] = 90.0
    df_many.loc[::10, "close"] = 92.0
    df_one = _flat_frame()
    df_one.loc[0, "low"] = 90.0
    df_one.loc[0, "close"] = 92.0
    zone = LevelZone(price=90.0, votes=1, sources=["swing_low"])
    many = _build_zone_detail(zone, 1.0, 100.0, df_many, TIMEFRAME_DAILY, True)
    one = _build_zone_detail(zone, 1.0, 100.0, df_one, TIMEFRAME_DAILY, True)
    assert many.touches > one.touches
    assert many.confidence > one.confidence


def test_old_level_recency_score_is_lower():
    old_df = _flat_frame()
    old_df.loc[0, ["low", "close"]] = [90.0, 92.0]
    recent_df = _flat_frame()
    recent_df.loc[len(recent_df) - 2, ["low", "close"]] = [90.0, 92.0]
    zone = LevelZone(price=90.0, votes=1, sources=["swing_low"])
    old = _build_zone_detail(zone, 1.0, 100.0, old_df, TIMEFRAME_DAILY, True)
    recent = _build_zone_detail(zone, 1.0, 100.0, recent_df, TIMEFRAME_DAILY, True)
    assert old.age_bars > recent.age_bars
    assert old.confidence < recent.confidence


def test_strongly_broken_level_becomes_inactive():
    df = _flat_frame()
    df.loc[len(df) - 3 :, ["open", "high", "low", "close", "volume"]] = [96.0, 97.0, 94.0, 95.0, 10_000_000.0]
    zone = LevelZone(price=100.0, votes=3, sources=["swing_low", "ema20"])
    detail = _build_zone_detail(zone, 1.0, 105.0, df, TIMEFRAME_DAILY, True)
    assert detail.active is False
    assert detail.break_count >= 2


def test_retest_role_reversal_is_marked():
    df = _flat_frame()
    zone = LevelZone(price=99.0, votes=3, sources=["kirilan_direnc_simdi_destek_retest"])
    detail = _build_zone_detail(zone, 1.0, 105.0, df, TIMEFRAME_DAILY, True)
    assert detail.role_reversal is True


def test_atr_controls_zone_width():
    df = _flat_frame(volume=100_000_000.0)
    zone = LevelZone(price=95.0, votes=2, sources=["swing_low"])
    narrow = _build_zone_detail(zone, 1.0, 100.0, df, TIMEFRAME_DAILY, True)
    wide = _build_zone_detail(zone, 3.0, 100.0, df, TIMEFRAME_DAILY, True)
    assert wide.high - wide.low > narrow.high - narrow.low


def test_insufficient_data_does_not_fabricate_level():
    df = _flat_frame(n=5)
    result = compute_timeframe_levels(df, 100.0)
    assert not result.daily.reliable
    assert result.daily.support_1 is None
    assert result.daily.resistance_1 is None


def test_identical_data_produces_stable_levels():
    df = _oscillating()
    current = float(df.iloc[-1]["close"])
    first = compute_timeframe_levels(df.copy(), current)
    second = compute_timeframe_levels(df.copy(), current)
    assert first.daily.support_1 == second.daily.support_1
    assert first.weekly.main_resistance == second.weekly.main_resistance


def test_small_price_perturbation_does_not_create_extreme_shift():
    df = _oscillating()
    current = float(df.iloc[-1]["close"])
    base = compute_timeframe_levels(df, current)
    perturbed = df.copy()
    for col in ("open", "high", "low", "close"):
        perturbed[col] *= 1.0002
    shifted = compute_timeframe_levels(perturbed, current * 1.0002)
    assert base.daily.support_1 is not None and shifted.daily.support_1 is not None
    assert abs(base.daily.support_1.mid - shifted.daily.support_1.mid) / current < 0.01


def test_multiple_method_families_are_used_when_data_supports_them():
    df = _oscillating()
    result = compute_timeframe_levels(df, float(df.iloc[-1]["close"]))
    sources = {source for level in result.all_zones() for source in level.sources}
    assert any(source.startswith("fibonacci") for source in sources)
    assert any("vwap" in source or "volume_profile" in source for source in sources)


def test_supports_and_resistances_stay_on_correct_price_side():
    df = _oscillating()
    current = float(df.iloc[-1]["close"])
    result = compute_timeframe_levels(df, current)
    for tf in (result.daily, result.weekly, result.monthly):
        for level in (tf.support_1, tf.support_2, tf.main_support):
            if level:
                assert level.mid <= current
        for level in (tf.resistance_1, tf.resistance_2, tf.main_resistance):
            if level:
                assert level.mid > current


def test_low_liquidity_produces_wider_zone():
    zone = LevelZone(price=95.0, votes=2, sources=["swing_low"])
    low_liquidity = _build_zone_detail(zone, 1.0, 100.0, _flat_frame(volume=100.0), TIMEFRAME_DAILY, True)
    liquid = _build_zone_detail(zone, 1.0, 100.0, _flat_frame(volume=100_000_000.0), TIMEFRAME_DAILY, True)
    assert low_liquidity.high - low_liquidity.low > liquid.high - liquid.low


def test_next_zone_is_populated_when_secondary_exists():
    df = _oscillating()
    result = compute_timeframe_levels(df, float(df.iloc[-1]["close"]))
    if result.daily.support_2 is not None:
        assert result.daily.support_1.next_zone_low == result.daily.support_2.low
    if result.daily.resistance_2 is not None:
        assert result.daily.resistance_1.next_zone_low == result.daily.resistance_2.low
