from __future__ import annotations

from dataclasses import dataclass

from app.analysis.breakout_scenario_engine import compute_breakout_scenarios


@dataclass
class _FakeZone:
    low: float
    high: float
    mid: float
    confidence: float


def test_resistance_breakout_scenario_generated():
    resistance = _FakeZone(low=118.0, high=122.0, mid=120.0, confidence=80.0)
    result = compute_breakout_scenarios(
        resistance_zone=resistance, support_zone=None, current_price=115.0,
        atr_value=2.0, relative_volume=1.6, adx=28.0, liquidity_score=80.0,
    )
    assert result.reliable is True
    case = result.resistance_breakout
    assert case is not None
    assert case.kind == "direnc_kirilimi"
    assert case.confirmation_close_level == 122.0
    assert case.target_1 > case.confirmation_close_level
    assert case.target_2 > case.target_1
    assert case.failure_level == 118.0


def test_support_breakdown_scenario_generated():
    support = _FakeZone(low=95.0, high=99.0, mid=97.0, confidence=75.0)
    result = compute_breakout_scenarios(
        resistance_zone=None, support_zone=support, current_price=101.0,
        atr_value=2.0, relative_volume=1.5, adx=25.0, liquidity_score=80.0,
    )
    case = result.support_breakdown
    assert case is not None
    assert case.kind == "destek_kirilimi"
    assert case.confirmation_close_level == 95.0
    assert case.target_1 < case.confirmation_close_level
    assert case.target_2 < case.target_1
    assert case.failure_level == 99.0  # (yeniden pozitife donus seviyesi)


def test_breakout_without_volume_not_considered_strong():
    """Hacimsiz kirilim 'guclu' kabul edilmemeli: dusuk hacimde
    volume_currently_confirmed False olmali ve sahte kirilim riski
    yukselmelidir."""
    resistance = _FakeZone(low=118.0, high=122.0, mid=120.0, confidence=80.0)
    result_low_volume = compute_breakout_scenarios(
        resistance_zone=resistance, support_zone=None, current_price=115.0,
        atr_value=2.0, relative_volume=0.6, adx=15.0, liquidity_score=80.0,
    )
    result_high_volume = compute_breakout_scenarios(
        resistance_zone=resistance, support_zone=None, current_price=115.0,
        atr_value=2.0, relative_volume=2.0, adx=30.0, liquidity_score=80.0,
    )
    assert result_low_volume.resistance_breakout.volume_currently_confirmed is False
    assert result_high_volume.resistance_breakout.volume_currently_confirmed is True


def test_false_breakout_risk_detected_on_weak_conditions():
    resistance = _FakeZone(low=118.0, high=122.0, mid=120.0, confidence=80.0)
    weak = compute_breakout_scenarios(
        resistance_zone=resistance, support_zone=None, current_price=115.0,
        atr_value=2.0, relative_volume=0.5, adx=10.0, liquidity_score=20.0,
    )
    strong = compute_breakout_scenarios(
        resistance_zone=resistance, support_zone=None, current_price=115.0,
        atr_value=2.0, relative_volume=2.5, adx=32.0, liquidity_score=85.0,
    )
    assert weak.resistance_breakout.false_breakout_risk == "yuksek"
    assert strong.resistance_breakout.false_breakout_risk == "dusuk"


def test_no_breakout_scenario_without_reliable_levels():
    result = compute_breakout_scenarios(
        resistance_zone=None, support_zone=None, current_price=100.0, atr_value=1.5,
    )
    assert result.reliable is False
    assert result.resistance_breakout is None
    assert result.support_breakdown is None
