from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np


@dataclass(frozen=True)
class BootstrapInterval:
    lower_percent: float
    median_percent: float
    upper_percent: float
    confidence_level: float
    samples: int
    seed: int


@dataclass(frozen=True)
class RobustnessReport:
    bootstrap: BootstrapInterval
    original_net_pnl: float
    without_best_5_net_pnl: float
    without_worst_5_net_pnl: float
    top_5_contribution_percent: float
    slippage_stress: dict[str, float]
    commission_stress: dict[str, float]
    parameter_sensitivity: dict[str, float]
    regime_stability: dict[str, float]
    evidence_class: str
    warnings: tuple[str, ...]


def bootstrap_mean_return_interval(
    trade_returns_percent: Iterable[float],
    *,
    samples: int = 2_000,
    confidence_level: float = 0.95,
    seed: int = 42,
) -> BootstrapInterval:
    values = np.asarray(list(trade_returns_percent), dtype=float)
    if len(values) == 0:
        return BootstrapInterval(0.0, 0.0, 0.0, confidence_level, samples, seed)
    rng = np.random.default_rng(seed)
    means = np.empty(samples, dtype=float)
    for index in range(samples):
        means[index] = float(np.mean(rng.choice(values, size=len(values), replace=True)))
    alpha = (1.0 - confidence_level) / 2.0
    return BootstrapInterval(
        lower_percent=round(float(np.quantile(means, alpha)), 4),
        median_percent=round(float(np.quantile(means, 0.5)), 4),
        upper_percent=round(float(np.quantile(means, 1.0 - alpha)), 4),
        confidence_level=confidence_level,
        samples=samples,
        seed=seed,
    )


def cost_stress_test(
    trade_returns_percent: Iterable[float],
    *,
    extra_cost_bps_scenarios: Iterable[float],
) -> dict[str, float]:
    values = list(float(item) for item in trade_returns_percent)
    return {
        f"{float(bps):g}_bps": round(sum(value - (float(bps) / 100.0) for value in values), 4)
        for bps in extra_cost_bps_scenarios
    }


def classify_evidence(
    trade_count: int,
    bootstrap: BootstrapInterval,
    *,
    top_5_contribution_percent: float,
    parameter_instability_percent: float = 0.0,
) -> str:
    if trade_count < 30:
        return "YETERSIZ_ORNEK"
    if top_5_contribution_percent > 60 or parameter_instability_percent > 50:
        return "ASIRI_UYUM_RISKI"
    if bootstrap.lower_percent > 0 and trade_count >= 100:
        return "GUCLU_KANIT"
    if bootstrap.lower_percent >= 0 or bootstrap.median_percent > 0:
        return "ORTA_KANIT"
    return "ZAYIF_KANIT"


def build_robustness_report(
    trade_pnls: Iterable[float],
    trade_returns_percent: Iterable[float],
    *,
    parameter_results: dict[str, float] | None = None,
    regime_results: dict[str, float] | None = None,
    seed: int = 42,
) -> RobustnessReport:
    pnls = sorted(float(item) for item in trade_pnls)
    returns = list(float(item) for item in trade_returns_percent)
    bootstrap = bootstrap_mean_return_interval(returns, seed=seed)
    original = sum(pnls)
    without_best = sum(pnls[:-5]) if len(pnls) > 5 else 0.0
    without_worst = sum(pnls[5:]) if len(pnls) > 5 else 0.0
    best_five = sum(pnls[-5:]) if pnls else 0.0
    top_contribution = abs(best_five / original * 100.0) if original else 100.0

    parameter_results = parameter_results or {}
    parameter_values = list(parameter_results.values())
    instability = 0.0
    if parameter_values:
        scale = max(abs(float(np.mean(parameter_values))), 1e-9)
        instability = float(np.std(parameter_values) / scale * 100.0)
    evidence = classify_evidence(
        len(pnls), bootstrap,
        top_5_contribution_percent=top_contribution,
        parameter_instability_percent=instability,
    )
    warnings: list[str] = []
    if len(pnls) < 30:
        warnings.append("Istatistiksel degerlendirme icin islem sayisi yetersiz.")
    if top_contribution > 60:
        warnings.append("Performans az sayida buyuk isleme asiri bagimli.")
    if instability > 50:
        warnings.append("Parametre hassasiyeti yuksek; asiri uyum riski var.")
    return RobustnessReport(
        bootstrap=bootstrap,
        original_net_pnl=round(original, 4),
        without_best_5_net_pnl=round(without_best, 4),
        without_worst_5_net_pnl=round(without_worst, 4),
        top_5_contribution_percent=round(top_contribution, 2),
        slippage_stress=cost_stress_test(returns, extra_cost_bps_scenarios=(0, 5, 10, 20)),
        commission_stress=cost_stress_test(returns, extra_cost_bps_scenarios=(0, 10, 20, 30)),
        parameter_sensitivity={key: round(float(value), 4) for key, value in parameter_results.items()},
        regime_stability={key: round(float(value), 4) for key, value in (regime_results or {}).items()},
        evidence_class=evidence,
        warnings=tuple(warnings),
    )
