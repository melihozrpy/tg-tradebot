from __future__ import annotations

from app.analysis.position_advice_engine import (
    DECISION_FULL_EXIT,
    DECISION_HOLD,
    DECISION_PARTIAL_PROFIT,
    DECISION_REDUCE,
    evaluate_position,
)


def test_position_stop_hit_triggers_full_exit():
    result = evaluate_position(
        lot=100, average_cost=50.0, current_price=44.0,
        technical_stop=45.0, target_1=55.0, target_2=60.0, target_3=65.0,
        main_resistance=70.0, decision_class="ALIM_ADAYI", trend_direction="up",
    )
    assert result.decision == DECISION_FULL_EXIT
    assert result.estimated_loss_if_stopped == (45.0 - 50.0) * 100


def test_position_target_1_reached_triggers_partial_profit():
    result = evaluate_position(
        lot=100, average_cost=50.0, current_price=56.0,
        technical_stop=45.0, target_1=55.0, target_2=60.0, target_3=65.0,
        main_resistance=70.0, decision_class="TUT", trend_direction="up",
    )
    assert result.decision == DECISION_PARTIAL_PROFIT


def test_position_target_3_reached_triggers_reduce():
    result = evaluate_position(
        lot=100, average_cost=50.0, current_price=66.0,
        technical_stop=45.0, target_1=55.0, target_2=60.0, target_3=65.0,
        main_resistance=70.0, decision_class="TUT", trend_direction="up",
    )
    assert result.decision == DECISION_REDUCE


def test_position_holds_when_structure_intact():
    result = evaluate_position(
        lot=100, average_cost=50.0, current_price=52.0,
        technical_stop=45.0, target_1=55.0, target_2=60.0, target_3=65.0,
        main_resistance=70.0, decision_class="TUT", trend_direction="up",
    )
    assert result.decision == DECISION_HOLD


def test_position_decision_is_independent_of_cost_basis():
    """Ayni teknik yapida, farkli maliyetli iki pozisyon icin karar AYNI olmalidir
    (kar/zarar goruntuye girer ama karara girmez)."""
    cheap = evaluate_position(
        lot=100, average_cost=10.0, current_price=44.0,  # buyuk kar
        technical_stop=45.0, target_1=55.0, target_2=60.0, target_3=65.0,
        main_resistance=70.0, decision_class="TUT", trend_direction="up",
    )
    expensive = evaluate_position(
        lot=100, average_cost=90.0, current_price=44.0,  # buyuk zarar
        technical_stop=45.0, target_1=55.0, target_2=60.0, target_3=65.0,
        main_resistance=70.0, decision_class="TUT", trend_direction="up",
    )
    assert cheap.decision == expensive.decision == DECISION_FULL_EXIT
    assert cheap.pnl_amount != expensive.pnl_amount
