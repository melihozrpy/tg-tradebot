from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.analysis.decision_engine import (
    DECISION_BUY,
    DECISION_STRONG_BUY,
    DECISION_WAIT_TRIGGER,
    decide,
)
from app.analysis.liquidity_engine import LIQUIDITY_LOW, LIQUIDITY_VERY_HIGH, LiquidityResult
from app.analysis.multi_timeframe_engine import (
    TREND_STRONG_DOWN,
    TREND_STRONG_UP,
    TREND_UP,
    MultiTimeframeResult,
)
from app.analysis.signal_engine import SignalResult


def _make_signal(signal_type="STRONG_BUY_CANDIDATE", score=88.0, is_actionable=True, confidence="yuksek") -> SignalResult:
    return SignalResult(
        symbol="SVGYO",
        timeframe="1d",
        score=score,
        signal_type=signal_type,
        confidence=confidence,
        reasons=[],
        entry_zone=(10.0, 10.2),
        stop_price=9.5,
        target_1=11.0,
        target_2=11.5,
        risk_reward=2.5,
        market_regime="guclu_yukselis",
        data_timestamp=datetime.now(timezone.utc),
        provider="mock",
        strategy_version="1.0.0",
        idempotency_key="abc123",
        is_actionable_buy=is_actionable,
        invalidation_note="",
    )


def _make_liquidity(available=True, score=80.0, allow_strong=True, liquidity_class=LIQUIDITY_VERY_HIGH, manipulation_risk=False) -> LiquidityResult:
    return LiquidityResult(
        available=available, score=score, liquidity_class=liquidity_class,
        allow_strong_signal=allow_strong, manipulation_risk=manipulation_risk,
    )


def _make_mtf(primary_direction=TREND_UP, short_term_direction=TREND_UP, conflict=False, counter_trend_warning=False) -> MultiTimeframeResult:
    return MultiTimeframeResult(
        symbol="SVGYO", snapshots={}, confluence_score=70.0,
        primary_direction=primary_direction, short_term_direction=short_term_direction,
        conflict=conflict, counter_trend_warning=counter_trend_warning, scenario_note="test senaryo",
    )


def test_strong_buy_passes_through_when_all_conditions_good():
    signal = _make_signal()
    result = decide(signal, liquidity=_make_liquidity(), multi_timeframe=_make_mtf())
    assert result.decision_class == DECISION_STRONG_BUY
    assert result.is_actionable_buy is True


def test_low_liquidity_blocks_strong_buy():
    signal = _make_signal()
    liquidity = _make_liquidity(score=20.0, allow_strong=False, liquidity_class=LIQUIDITY_LOW)
    result = decide(signal, liquidity=liquidity)
    assert result.decision_class == DECISION_BUY
    assert result.is_actionable_buy is False
    assert any("Likidite" in r or "likidite" in r for r in result.gating_reasons)


def test_counter_trend_warning_downgrades_to_wait_trigger():
    signal = _make_signal()
    mtf = _make_mtf(primary_direction=TREND_STRONG_DOWN, short_term_direction=TREND_STRONG_UP, counter_trend_warning=True)
    result = decide(signal, liquidity=_make_liquidity(), multi_timeframe=mtf)
    assert result.decision_class == DECISION_WAIT_TRIGGER
    assert result.is_actionable_buy is False


def test_conflict_downgrades_confidence_and_strong_buy():
    signal = _make_signal()
    mtf = _make_mtf(conflict=True)
    result = decide(signal, liquidity=_make_liquidity(), multi_timeframe=mtf)
    assert result.decision_class == DECISION_BUY
    assert result.confidence == "orta"


def test_manipulation_risk_prevents_actionable_buy():
    signal = _make_signal()
    liquidity = _make_liquidity(manipulation_risk=True)
    result = decide(signal, liquidity=liquidity)
    assert result.is_actionable_buy is False


def test_missing_liquidity_and_mtf_still_produces_decision():
    """Likidite/coklu zaman dilimi verilmezse (None) sistem cokmemeli, sadece
    temel sinyal siniflandirmasini Turkce karsiligina cevirmeli."""
    signal = _make_signal(signal_type="WATCH", is_actionable=False, score=68.0)
    result = decide(signal)
    assert result.decision_class == DECISION_WAIT_TRIGGER
    assert result.decision_label_tr == "TETİK BEKLENİYOR"


def test_missing_news_and_anomaly_scores_do_not_penalize():
    signal = _make_signal()
    result = decide(signal, liquidity=_make_liquidity(), news_score=None, anomaly_score=None)
    assert result.decision_class == DECISION_STRONG_BUY


def test_strong_negative_news_blocks_actionable_buy():
    signal = _make_signal()
    result = decide(signal, liquidity=_make_liquidity(), news_score=-75)
    assert result.is_actionable_buy is False


def test_critical_anomaly_downgrades_strong_buy():
    signal = _make_signal()
    result = decide(signal, liquidity=_make_liquidity(), anomaly_score=90)
    assert result.decision_class == DECISION_BUY
