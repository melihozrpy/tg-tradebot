from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass(frozen=True)
class ScoreContribution:
    factor_key: str
    description: str
    value: float
    source_engine: str
    source_field: str
    data_available: bool = True


@dataclass(frozen=True)
class ExplainableSignalResult:
    starting_score: float
    positive_contributions: tuple[ScoreContribution, ...]
    negative_contributions: tuple[ScoreContribution, ...]
    positive_total: float
    negative_total: float
    raw_final_score: float
    calibration_information: str | None = None

    @property
    def all_contributions(self) -> tuple[ScoreContribution, ...]:
        return self.positive_contributions + self.negative_contributions

    def top_reasons(self, count: int = 2) -> tuple[list[ScoreContribution], list[ScoreContribution]]:
        positives = sorted(self.positive_contributions, key=lambda item: abs(item.value), reverse=True)[:count]
        negatives = sorted(self.negative_contributions, key=lambda item: abs(item.value), reverse=True)[:count]
        return positives, negatives


class ExplainableSignalEngine:
    """Hesapta kullanilan deterministik faktorleri son skorla uzlastirir."""

    def __init__(self, max_factor_impact: float = 20.0):
        self.max_factor_impact = max(0.1, float(max_factor_impact))

    def evaluate(
        self,
        starting_score: float,
        contributions: Iterable[ScoreContribution],
        *,
        calibration_information: str | None = None,
    ) -> ExplainableSignalResult:
        accepted: list[ScoreContribution] = []
        for item in contributions:
            value = min(self.max_factor_impact, max(-self.max_factor_impact, float(item.value)))
            # Eksik veri olumlu puan uretemez. Eksikligin gercek bir ceza olarak
            # hesaplandigi negatif katkilar ise korunur.
            if not item.data_available and value > 0:
                value = 0.0
            accepted.append(ScoreContribution(
                factor_key=item.factor_key,
                description=item.description,
                value=round(value, 6),
                source_engine=item.source_engine,
                source_field=item.source_field,
                data_available=item.data_available,
            ))

        unconstrained = float(starting_score) + sum(item.value for item in accepted)
        final_score = min(100.0, max(0.0, unconstrained))
        clamp_adjustment = final_score - unconstrained
        if abs(clamp_adjustment) > 1e-9:
            accepted.append(ScoreContribution(
                factor_key="score_boundary",
                description="Skor 0-100 sinirina uyarlandi",
                value=round(clamp_adjustment, 6),
                source_engine="ExplainableSignalEngine",
                source_field="score_bounds",
                data_available=True,
            ))

        positives = tuple(item for item in accepted if item.value > 0)
        negatives = tuple(item for item in accepted if item.value < 0)
        positive_total = sum(item.value for item in positives)
        negative_total = sum(item.value for item in negatives)
        reconciled = float(starting_score) + positive_total + negative_total
        return ExplainableSignalResult(
            starting_score=round(float(starting_score), 6),
            positive_contributions=positives,
            negative_contributions=negatives,
            positive_total=round(positive_total, 6),
            negative_total=round(negative_total, 6),
            raw_final_score=round(reconciled, 6),
            calibration_information=calibration_information,
        )


def format_signal_reasons(symbol: str, result: ExplainableSignalResult, *, compact: bool = False) -> str:
    positives, negatives = result.top_reasons(2 if compact else 10)
    lines = [f"NEDEN - {symbol.upper()}", "", "PUANI ARTIRANLAR"]
    lines.extend(f"+ {item.description}: +{item.value:g}" for item in positives)
    if not positives:
        lines.append("Olumlu katkı yok.")
    lines.extend(["", "PUANI DUSURENLER"])
    lines.extend(f"- {item.description}: {item.value:g}" for item in negatives)
    if not negatives:
        lines.append("Olumsuz katkı yok.")
    lines.extend([
        "",
        f"Baslangic puani: {result.starting_score:g}",
        f"Pozitif toplam: +{result.positive_total:g}",
        f"Negatif toplam: {result.negative_total:g}",
        f"Ham nihai skor: {result.raw_final_score:g}/100",
    ])
    if result.calibration_information:
        lines.append(f"Kalibrasyon: {result.calibration_information}")
    return "\n".join(lines)
