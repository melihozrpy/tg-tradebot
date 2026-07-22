from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from app.models.database import SignalOutcome
from app.services.signal_outcome_tracker import SignalOutcomeTracker, SignalSnapshotInput


def _input(signal_time, stop=95, targets=(105, 110, 115)):
    return SignalSnapshotInput(
        symbol="THYAO", signal_time=signal_time, signal_price=100,
        last_confirmed_close=100, signal_type="BUY_CANDIDATE",
        raw_signal_score=72, rule_based_confidence="orta", displayed_confidence="orta",
        market_regime="boga", benchmark_strength=60, sector_strength=55,
        liquidity_score=70, data_quality_score=90, trends={"daily": "up"},
        support_resistance={"support": 95}, stop_price=stop, targets=targets,
        news_impact=0, positive_contributions=[{"value": 5}],
        negative_contributions=[{"value": -3}], strategy_version="5g",
        provider="fake", price_adjustment_mode="adjusted",
    )


def _future_bars(signal_time, count=65, *, daily_change=0.2):
    rows = []
    for index in range(1, count + 1):
        close = 100 + index * daily_change
        rows.append({
            "timestamp": signal_time + timedelta(days=index),
            "open": close - 0.1, "high": close + 0.5, "low": close - 0.5,
            "close": close, "volume": 1000, "is_complete": True, "data_quality": "VALID",
        })
    return pd.DataFrame(rows)


def test_33_signal_snapshot_does_not_change_after_outcome_evaluation(db_session):
    signal_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    tracker = SignalOutcomeTracker(db_session)
    snapshot = tracker.capture(_input(signal_time))
    before = (snapshot.snapshot_hash, snapshot.features_json, snapshot.raw_signal_score)
    tracker.evaluate(snapshot, _future_bars(signal_time))
    db_session.refresh(snapshot)
    assert (snapshot.snapshot_hash, snapshot.features_json, snapshot.raw_signal_score) == before


def test_34_one_five_twenty_sixty_day_outcomes_are_calculated(db_session):
    signal_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    tracker = SignalOutcomeTracker(db_session); snapshot = tracker.capture(_input(signal_time, targets=(150, 160, 170)))
    outcomes = tracker.evaluate(snapshot, _future_bars(signal_time), horizons=(1, 5, 20, 60))
    assert [item.horizon_days for item in outcomes] == [1, 5, 20, 60]
    assert [item.return_percent for item in outcomes] == [0.2, 1.0, 4.0, 12.0]


def test_35_target_reach_is_recorded(db_session):
    signal_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    tracker = SignalOutcomeTracker(db_session); snapshot = tracker.capture(_input(signal_time))
    bars = _future_bars(signal_time, 5, daily_change=1.1)
    outcome = tracker.evaluate(snapshot, bars, horizons=(5,))[0]
    assert outcome.target_hits[0]
    assert outcome.outcome_class in {"BASARILI", "KISMEN_BASARILI"}


def test_36_stop_is_recorded_conservatively(db_session):
    signal_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    tracker = SignalOutcomeTracker(db_session); snapshot = tracker.capture(_input(signal_time))
    bars = _future_bars(signal_time, 1)
    bars.loc[0, ["high", "low", "close"]] = [106, 94, 96]
    outcome = tracker.evaluate(snapshot, bars, horizons=(1,))[0]
    assert outcome.stop_hit
    assert not outcome.target_hits[0]
    assert outcome.outcome_class == "STOP"


def test_37_missing_future_data_does_not_fabricate_result(db_session):
    signal_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    tracker = SignalOutcomeTracker(db_session); snapshot = tracker.capture(_input(signal_time))
    outcome = tracker.evaluate(snapshot, _future_bars(signal_time, 2), horizons=(5,))[0]
    assert outcome.return_percent is None
    assert outcome.outcome_class == "VERI_YETERSIZ"
    assert outcome.data_sufficiency == "YETERSIZ"


def test_38_duplicate_signal_outcome_is_not_created(db_session):
    signal_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
    tracker = SignalOutcomeTracker(db_session); snapshot = tracker.capture(_input(signal_time))
    tracker.evaluate(snapshot, _future_bars(signal_time, 5), horizons=(5,))
    tracker.evaluate(snapshot, _future_bars(signal_time, 5), horizons=(5,))
    assert db_session.query(SignalOutcome).filter_by(signal_snapshot_id=snapshot.id, horizon_days=5).count() == 1
