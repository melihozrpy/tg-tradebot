from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.analysis.confluence_zone_engine import find_confluence_zones
from app.analysis.price_scenario_engine import compute_price_scenarios
from app.analysis.timeframe_levels_engine import compute_timeframe_levels


def _oscillating_df(n_days: int = 400, floor: float = 90.0, ceiling: float = 110.0, seed: int = 21) -> pd.DataFrame:
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
def scenario_inputs():
    df = _oscillating_df()
    current_price = float(df["close"].iloc[-1])
    levels = compute_timeframe_levels(df, current_price)
    supports, resistances = find_confluence_zones(levels, current_price)
    return levels, supports, resistances, current_price


def test_near_decline_scenario_is_computed(scenario_inputs):
    levels, supports, resistances, price = scenario_inputs
    result = compute_price_scenarios(levels, supports, resistances, price)
    assert result.reliable is True
    assert result.decline_near is not None
    assert result.decline_near.low < price
    assert result.decline_near.direction == "dusus"
    assert result.decline_near.tier == "yakin"


def test_main_dip_zone_is_computed(scenario_inputs):
    levels, supports, resistances, price = scenario_inputs
    result = compute_price_scenarios(levels, supports, resistances, price)
    assert result.decline_main is not None
    assert result.decline_main.tier == "ana"
    # ana dip, yakin dusus senaryosundan daha uzakta olmalidir.
    assert result.decline_main.low <= result.decline_near.low


def test_extreme_scenario_not_created_without_enough_evidence():
    """Yalnizca tek bir destek/direnc adayi varsa 'asiri' senaryo
    UYDURULMAZ (None kalir)."""
    from app.analysis.price_scenario_engine import PriceScenarioResult, _MergedTier, compute_price_scenarios
    from app.analysis.timeframe_levels_engine import (
        LevelDetail,
        MultiTimeframeLevelsResult,
        TimeframeLevelResult,
    )

    only_one_support = LevelDetail(
        low=95.0, high=97.0, mid=96.0, confidence=60.0, touches=3, rejections=1,
        last_test_date="2026-01-01", sources=["swing_low"], volume_confirmed=True, timeframe="gunluk",
    )
    daily = TimeframeLevelResult(timeframe="gunluk", reliable=True, note="", support_1=only_one_support)
    weekly = TimeframeLevelResult(timeframe="haftalik", reliable=False, note="Guvenilir seviye hesaplanamadi.")
    monthly = TimeframeLevelResult(timeframe="aylik", reliable=False, note="Guvenilir seviye hesaplanamadi.")
    levels = MultiTimeframeLevelsResult(daily=daily, weekly=weekly, monthly=monthly)

    result = compute_price_scenarios(levels, [], [], current_price=100.0)
    assert result.decline_near is not None
    assert result.decline_extreme is None  # yeterli kanit yok


def test_near_target_and_strong_breakout_target_computed(scenario_inputs):
    levels, supports, resistances, price = scenario_inputs
    result = compute_price_scenarios(levels, supports, resistances, price)
    assert result.rise_near is not None
    assert result.rise_near.high > price


def test_strong_breakout_tier_computed_with_enough_resistance_tiers():
    """En az 3 ayirt edici direnc kademesi varsa 'guclu kirilim' senaryosu
    hesaplanmalidir."""
    from app.analysis.timeframe_levels_engine import (
        LevelDetail,
        MultiTimeframeLevelsResult,
        TimeframeLevelResult,
    )

    def lvl(mid: float) -> LevelDetail:
        return LevelDetail(
            low=mid - 1, high=mid + 1, mid=mid, confidence=70.0, touches=5, rejections=2,
            last_test_date="2026-01-01", sources=["swing_high"], volume_confirmed=True, timeframe="gunluk",
        )

    daily = TimeframeLevelResult(
        timeframe="gunluk", reliable=True, note="",
        resistance_1=lvl(110.0), resistance_2=lvl(120.0), main_resistance=lvl(130.0),
    )
    empty_tf = TimeframeLevelResult(timeframe="haftalik", reliable=False, note="Guvenilir seviye hesaplanamadi.")
    levels = MultiTimeframeLevelsResult(
        daily=daily,
        weekly=empty_tf,
        monthly=TimeframeLevelResult(timeframe="aylik", reliable=False, note="Guvenilir seviye hesaplanamadi."),
    )

    result = compute_price_scenarios(levels, [], [], current_price=100.0)
    assert result.rise_near is not None
    assert result.rise_main is not None
    assert result.rise_breakout is not None
    assert result.rise_breakout.tier == "guclu_kirilim"


def test_no_certainty_language_used(scenario_inputs):
    levels, supports, resistances, price = scenario_inputs
    result = compute_price_scenarios(levels, supports, resistances, price)
    forbidden_terms = ["kesin", "maksimum dip", "maksimum yukselis", "kesinlikle"]
    all_texts = []
    for name in ("decline_near", "decline_main", "decline_extreme", "rise_near", "rise_main", "rise_breakout", "rise_extreme"):
        zone = getattr(result, name)
        if zone is not None:
            all_texts.append(zone.activation_condition.lower())
    joined = " ".join(all_texts)
    for term in forbidden_terms:
        assert term not in joined


def test_no_scenarios_when_no_reliable_levels():
    from app.analysis.timeframe_levels_engine import MultiTimeframeLevelsResult, TimeframeLevelResult

    unreliable = TimeframeLevelResult(timeframe="gunluk", reliable=False, note="Guvenilir seviye hesaplanamadi.")
    levels = MultiTimeframeLevelsResult(
        daily=unreliable,
        weekly=TimeframeLevelResult(timeframe="haftalik", reliable=False, note="Guvenilir seviye hesaplanamadi."),
        monthly=TimeframeLevelResult(timeframe="aylik", reliable=False, note="Guvenilir seviye hesaplanamadi."),
    )
    result = compute_price_scenarios(levels, [], [], current_price=100.0)
    assert result.reliable is False
    assert "Guvenilir" in result.note
