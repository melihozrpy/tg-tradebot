from __future__ import annotations

from dataclasses import asdict, is_dataclass

from app.analysis.explainable_signal_engine import ExplainableSignalEngine, ScoreContribution


def build_analysis_explanation(advanced_score, *, quality_status: str = "VALID"):
    """Mevcut AdvancedScoreBreakdown'u 50 puanlik notr bazdan aciklar."""
    components = [
        ("trend", "Trend yapisi", advanced_score.trend, 10.0, True),
        ("momentum", "Momentum", advanced_score.momentum, 5.0, True),
        ("volume", "Hacim teyidi", advanced_score.volume, 7.5, True),
        ("support_resistance", "Destek/direnc yapisi", advanced_score.support_resistance, 7.5, True),
        ("xu100_strength", "XU100 goreceli guc", advanced_score.xu100_strength, 7.5, advanced_score.xu100_strength_available),
        ("sector_strength", "Sektor goreceli guc", advanced_score.sector_strength, 5.0, advanced_score.sector_strength_available),
        ("regime", "Piyasa rejimi", advanced_score.regime, 5.0, True),
        ("risk_reward", "Likidite/risk yapisi", advanced_score.risk_reward, 2.5, True),
    ]
    contributions = [
        ScoreContribution(
            factor_key=key,
            description=description,
            value=(float(value) - neutral) if available else 0.0,
            source_engine="AdvancedScoringEngine",
            source_field=key,
            data_available=available,
        )
        for key, description, value, neutral, available in components
    ]
    if quality_status.upper() == "DEGRADED":
        contributions.append(ScoreContribution(
            factor_key="data_quality_penalty", description="Veri kalitesi dusuk",
            value=-8.0, source_engine="DataQualityEngine", source_field="status",
            data_available=True,
        ))
    if abs(float(getattr(advanced_score, "news_adjustment", 0.0))) > 1e-12:
        contributions.append(ScoreContribution(
            factor_key="news_adjustment", description="Dogrulanmis haber etkisi",
            value=float(advanced_score.news_adjustment), source_engine="NewsImpactEngine",
            source_field="score_contribution", data_available=True,
        ))
    return ExplainableSignalEngine().evaluate(50.0, contributions)


def serialize_contribution(item) -> dict:
    return asdict(item) if is_dataclass(item) else dict(item)
