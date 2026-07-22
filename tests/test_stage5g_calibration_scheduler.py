from __future__ import annotations

from datetime import datetime, timezone

from app.analysis.score_calibration_engine import lookup_calibrated_success, run_calibration_training
from app.models.database import ScoreCalibrationModel, SignalFeatureSnapshot, SignalOutcome


def test_65_completed_outcomes_train_and_expose_market_calibration(db_session):
    now = datetime.now(timezone.utc)
    for index in range(4):
        snapshot = SignalFeatureSnapshot(
            symbol="THYAO", signal_time=now, signal_price=100, last_confirmed_close=100,
            signal_type="BUY", raw_signal_score=70 + index, rule_based_confidence="orta",
            features_json="{}", strategy_version="5g", snapshot_hash=f"{index:064d}",
        )
        db_session.add(snapshot); db_session.flush()
        db_session.add(SignalOutcome(
            signal_snapshot_id=snapshot.id, horizon_days=20, evaluated_at=now,
            outcome_class="BASARILI" if index < 3 else "BASARISIZ",
            data_sufficiency="YETERLI", target_1_hit=False, target_2_hit=False,
            target_3_hit=False, stop_hit=False,
        ))
    db_session.commit()
    assert run_calibration_training(db_session, minimum_sample_size=3) >= 1
    assert db_session.query(ScoreCalibrationModel).filter_by(scope_type="market").count() == 1
    calibrated = lookup_calibrated_success(
        db_session, score=72, symbol="THYAO", sector=None, minimum_sample_size=3,
    )
    assert calibrated is not None
    assert calibrated.calibration_scope in {"symbol:THYAO", "market:BIST"}
