from __future__ import annotations

from app.backtest.statistics import (
    bootstrap_mean_return_interval, build_robustness_report, classify_evidence, cost_stress_test,
)


def test_51_bootstrap_confidence_interval_is_generated_deterministically():
    one = bootstrap_mean_return_interval([1, 2, -1, 3, 0.5], samples=500, seed=9)
    two = bootstrap_mean_return_interval([1, 2, -1, 3, 0.5], samples=500, seed=9)
    assert one == two
    assert one.lower_percent <= one.median_percent <= one.upper_percent


def test_52_result_is_recalculated_without_best_five_trades():
    report = build_robustness_report(range(-5, 15), [1] * 20)
    assert report.without_best_5_net_pnl == sum(range(-5, 10))
    assert report.without_best_5_net_pnl != report.original_net_pnl


def test_53_slippage_stress_test_reduces_result():
    stressed = cost_stress_test([1, 1, 1], extra_cost_bps_scenarios=(0, 20))
    assert stressed["20_bps"] < stressed["0_bps"]


def test_54_overfit_risk_is_classified():
    bootstrap = bootstrap_mean_return_interval([1] * 40, samples=100)
    evidence = classify_evidence(40, bootstrap, top_5_contribution_percent=80)
    assert evidence == "ASIRI_UYUM_RISKI"
