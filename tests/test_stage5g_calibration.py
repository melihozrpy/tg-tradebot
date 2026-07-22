from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.score_calibration_engine import (
    DISCLAIMER, CalibrationObservation, ScoreCalibrationEngine, persist_calibration_model,
)
from app.models.database import ScoreCalibrationBin, ScoreCalibrationModel


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _obs(score, success, days=-1, symbol="THYAO", sector="ULASTIRMA"):
    return CalibrationObservation(score, success, NOW + timedelta(days=days), symbol, sector)


def test_39_calibration_bin_observed_success_rate_is_correct():
    model = ScoreCalibrationEngine(minimum_sample_size=2).fit(
        [_obs(65, True), _obs(68, False), _obs(62, True)], training_end=NOW,
    )
    bin_60 = next(item for item in model.bins if item.score_min == 60)
    assert bin_60.observed_success_rate == pytest.approx(66.67, abs=0.01)
    assert bin_60.sample_count == 3


def test_40_low_sample_is_shrunk_and_warning_is_shown():
    model = ScoreCalibrationEngine(minimum_sample_size=30, prior_strength=20).fit([_obs(85, True)], training_end=NOW)
    result = model.calibrate(85)
    assert 50 < result.calibrated_success_rate < 100
    assert result.calibration_warning is not None


def test_41_brier_score_is_correct():
    model = ScoreCalibrationEngine().fit([_obs(80, True), _obs(20, False)], training_end=NOW)
    assert model.brier_score == pytest.approx(0.04)


def test_42_test_period_observations_do_not_enter_calibration():
    model = ScoreCalibrationEngine().fit(
        [_obs(70, True, -1), _obs(70, False, 1)], training_end=NOW,
    )
    assert model.sample_count == 1
    bin_70 = next(item for item in model.bins if item.score_min == 70)
    assert bin_70.observed_success_rate == 100


def test_43_calibration_version_is_deterministic_and_persisted(db_session):
    engine = ScoreCalibrationEngine()
    observations = [_obs(60, True), _obs(70, False)]
    one = engine.fit(observations, training_end=NOW)
    two = engine.fit(observations, training_end=NOW)
    assert one.version == two.version
    record = persist_calibration_model(db_session, one)
    assert record.version == one.version
    assert db_session.query(ScoreCalibrationBin).filter_by(calibration_model_id=record.id).count() == 7


def test_44_scope_falls_back_from_symbol_to_sector_then_market():
    engine = ScoreCalibrationEngine(minimum_sample_size=3)
    observations = [
        _obs(60, True, symbol="AAA", sector="BANKA"),
        _obs(65, True, symbol="BBB", sector="BANKA"),
        _obs(70, False, symbol="CCC", sector="BANKA"),
        _obs(50, False, symbol="DDD", sector="SANAYI"),
    ]
    sector_model = engine.select_scope(observations, symbol="AAA", sector="BANKA", training_end=NOW)
    market_model = engine.select_scope(observations, symbol="AAA", sector="YOK", training_end=NOW)
    assert sector_model.scope_type == "sector"
    assert market_model.scope_type == "market"


def test_45_historical_rate_is_not_presented_as_future_guarantee():
    result = ScoreCalibrationEngine(minimum_sample_size=1).fit([_obs(70, True)], training_end=NOW).calibrate(70)
    assert result.disclaimer == DISCLAIMER
    assert "garanti etmez" in result.disclaimer.lower()
