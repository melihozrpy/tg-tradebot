from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.analysis.explainable_signal_engine import ExplainableSignalEngine, ScoreContribution
from app.models.database import SignalFeatureSnapshot, SignalScoreContribution
from app.telegram.handlers_stage5g import _reason_text


def _c(key, value, available=True):
    return ScoreContribution(key, key.replace("_", " "), value, "TestEngine", key, available)


def test_46_positive_negative_contributions_reconcile_to_final_score():
    result = ExplainableSignalEngine().evaluate(50, [_c("trend", 12), _c("risk", -8), _c("volume", 6)])
    assert result.starting_score + result.positive_total + result.negative_total == result.raw_final_score
    assert result.raw_final_score == 60


def test_47_missing_data_factor_cannot_add_positive_score():
    result = ExplainableSignalEngine().evaluate(50, [_c("missing_sector", 10, available=False)])
    assert result.raw_final_score == 50
    assert result.positive_total == 0


def test_48_explainable_engine_has_no_groq_score_generation():
    source = Path("app/analysis/explainable_signal_engine.py").read_text(encoding="utf-8").lower()
    assert "groq" not in source
    assert "llm" not in source


def test_49_neden_output_uses_persisted_real_contributions(db_session):
    snapshot = SignalFeatureSnapshot(
        symbol="THYAO", signal_time=datetime.now(timezone.utc), signal_price=100,
        last_confirmed_close=100, signal_type="BUY", raw_signal_score=58,
        rule_based_confidence="orta", features_json="{}", strategy_version="5g",
        snapshot_hash="a" * 64,
    )
    db_session.add(snapshot); db_session.flush()
    db_session.add_all([
        SignalScoreContribution(
            signal_snapshot_id=snapshot.id, factor_key="trend", description="Haftalik trend",
            contribution=12, source_engine="TrendEngine", source_field="weekly", data_available=True,
        ),
        SignalScoreContribution(
            signal_snapshot_id=snapshot.id, factor_key="risk", description="Likidite riski",
            contribution=-4, source_engine="LiquidityEngine", source_field="risk", data_available=True,
        ),
    ])
    db_session.commit()
    text = _reason_text(db_session, "THYAO")
    assert "Haftalik trend: +12" in text
    assert "Likidite riski: -4" in text
    assert "58/100" in text


def test_50_most_important_reasons_are_sorted_by_absolute_value():
    result = ExplainableSignalEngine().evaluate(50, [
        _c("small_positive", 2), _c("large_positive", 10),
        _c("small_negative", -1), _c("large_negative", -9),
    ])
    positives, negatives = result.top_reasons(2)
    assert positives[0].factor_key == "large_positive"
    assert negatives[0].factor_key == "large_negative"
