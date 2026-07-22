from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app.analysis.long_term_scenario_engine import compute_long_term_scenarios
from app.analysis.target_realism_engine import evaluate_target_realism
from app.analysis.target_roadmap_engine import build_target_roadmap, update_roadmap_status
from app.analysis.user_target_engine import evaluate_user_target


def _daily(periods=300, start=10.0, end=20.0, volume=1_000_000):
    close = np.linspace(start, end, periods)
    dates = pd.date_range(end=datetime(2026, 7, 17, tzinfo=timezone.utc), periods=periods, freq="1D")
    return pd.DataFrame(
        {
            "timestamp": dates,
            "open": close * 0.995,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": [volume] * periods,
        }
    )


def test_long_term_scenario_separates_short_medium_and_long_targets():
    result = compute_long_term_scenarios(_daily(), 20.0, liquidity_score=70)
    assert result.reliable
    zones = [result.short_term_target, result.medium_term_target, result.long_term_main_target]
    assert any(zone is not None for zone in zones)
    mids = [zone.mid for zone in zones if zone is not None]
    assert mids == sorted(mids)


def test_extreme_bull_is_not_generated_without_three_evidence_sources():
    result = compute_long_term_scenarios(_daily(100, 18, 20), 20.0)
    assert result.extreme_bull is None


def test_extreme_negative_is_bounded_to_reasonable_floor():
    result = compute_long_term_scenarios(_daily(350, 2, 20), 20.0)
    if result.extreme_negative:
        assert result.extreme_negative.mid >= 6.0


def test_long_term_scenarios_include_activation_and_invalidation():
    result = compute_long_term_scenarios(_daily(), 20.0, liquidity_score=60)
    for zone in result.all_scenarios():
        assert zone.activation_conditions
        assert zone.invalidation_conditions


def test_long_term_scenario_does_not_use_certainty_language():
    result = compute_long_term_scenarios(_daily(), 20.0)
    text = " ".join(
        condition
        for zone in result.all_scenarios()
        for condition in zone.activation_conditions + zone.invalidation_conditions
    ).lower()
    assert "kesin olacak" not in text
    assert "maksimum fiyat" not in text
    assert "kesin döner" not in text


def test_user_target_percentage_and_multiple_are_correct():
    result = evaluate_user_target("SVGYO", 12.72, 70.0, liquidity_score=35)
    assert result.realism.required_change_percent == 450.31
    assert result.realism.required_price_multiple == 5.5


def test_user_target_does_not_mutate_external_signal_score():
    signal = {"score": 61.0, "signal_type": "WATCH"}
    before = signal.copy()
    evaluate_user_target("SVGYO", 12.72, 70.0)
    assert signal == before


def test_far_target_passes_through_realism_filter():
    result = evaluate_target_realism(
        12.72, 70.0, shares_outstanding=100_000_000,
        liquidity_score=30, fundamental_available=False,
    )
    assert result.technical_probability_class == "Aşırı spekülatif"
    assert result.realism_score <= 55
    assert result.target_market_cap == 7_000_000_000


def test_missing_fundamentals_never_fabricate_positive_support():
    result = evaluate_target_realism(10, 15, liquidity_score=80, fundamental_available=False)
    assert result.fundamental_support_class == "Veri yetersiz"
    assert result.realism_score <= 55


def test_low_liquidity_increases_target_risk():
    low = evaluate_target_realism(10, 15, liquidity_score=10, fundamental_available=True)
    high = evaluate_target_realism(10, 15, liquidity_score=80, fundamental_available=True)
    assert low.realism_score < high.realism_score
    assert low.liquidity_risk == "Çok yüksek"


def test_manipulation_wording_is_not_a_definitive_accusation():
    result = evaluate_target_realism(10, 30, abnormal_price_volume=True)
    assert "iddiası değildir" in result.manipulation_indicator


def test_roadmap_sorts_and_deduplicates_steps():
    result = build_target_roadmap(10, 30, intermediate_levels=[20, 15, 15.05, 25])
    mids = [step.mid for step in result.steps]
    assert mids == sorted(mids)
    assert len(mids) == len(set(mids))
    assert mids[-1] == 30


def test_roadmap_status_and_invalidation_work():
    step = build_target_roadmap(10, 20, intermediate_levels=[15]).steps[0]
    assert update_roadmap_status(step, current_price=step.mid) == "Test ediliyor"
    assert update_roadmap_status(step, current_price=9, close_price=8) == "Geçersiz"


def test_target_market_cap_increase_is_correct():
    result = evaluate_target_realism(10, 20, shares_outstanding=1_000_000)
    assert result.current_market_cap == 10_000_000
    assert result.target_market_cap == 20_000_000
    assert result.market_cap_increase == 10_000_000
