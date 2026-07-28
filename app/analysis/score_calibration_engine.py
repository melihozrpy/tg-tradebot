from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable, Optional

import numpy as np
from sqlalchemy.orm import Session

from app.models.database import ScoreCalibrationBin, ScoreCalibrationModel

SCORE_RANGES = ((0, 39), (40, 49), (50, 59), (60, 69), (70, 79), (80, 89), (90, 100))
DISCLAIMER = "Gecmis benzer sinyallerin gerceklesme oranidir; gelecek sonucu garanti etmez."


@dataclass(frozen=True)
class CalibrationObservation:
    raw_signal_score: float
    success: bool
    completed_at: datetime
    symbol: Optional[str] = None
    sector: Optional[str] = None


@dataclass(frozen=True)
class ReliabilityBin:
    score_min: int
    score_max: int
    expected_success_rate: float
    observed_success_rate: float
    calibrated_success_rate: float
    sample_count: int


@dataclass(frozen=True)
class CalibrationModelResult:
    version: str
    method: str
    scope_type: str
    scope_value: str
    training_end: datetime
    sample_count: int
    brier_score: float
    calibration_error: float
    bins: tuple[ReliabilityBin, ...]
    minimum_sample_size: int

    def calibrate(self, score: float) -> "CalibratedSuccess":
        bounded = min(100.0, max(0.0, float(score)))
        selected = next((item for item in self.bins if item.score_min <= bounded <= item.score_max), self.bins[-1])
        warning = None
        if selected.sample_count < self.minimum_sample_size:
            warning = "Ornek sayisi az; oran notr degere dogru daraltilmistir."
        return CalibratedSuccess(
            raw_signal_score=bounded,
            calibrated_success_rate=selected.calibrated_success_rate,
            calibration_sample_count=selected.sample_count,
            calibration_version=self.version,
            calibration_scope=f"{self.scope_type}:{self.scope_value}",
            calibration_warning=warning,
            disclaimer=DISCLAIMER,
        )


@dataclass(frozen=True)
class CalibratedSuccess:
    raw_signal_score: float
    calibrated_success_rate: float
    calibration_sample_count: int
    calibration_version: str
    calibration_scope: str
    calibration_warning: Optional[str]
    disclaimer: str


class ScoreCalibrationEngine:
    """Sabit bin + beta shrinkage + PAVA isotonic kalibrasyon."""

    def __init__(self, minimum_sample_size: int = 30, prior_strength: int = 20):
        self.minimum_sample_size = max(1, int(minimum_sample_size))
        self.prior_strength = max(1, int(prior_strength))

    def fit(
        self,
        observations: Iterable[CalibrationObservation],
        *,
        training_end: datetime,
        scope_type: str = "market",
        scope_value: str = "BIST",
    ) -> CalibrationModelResult:
        eligible = [item for item in observations if item.completed_at <= training_end]
        raw_bins: list[tuple[int, int, float, float, int, float]] = []
        for low, high in SCORE_RANGES:
            group = [item for item in eligible if low <= item.raw_signal_score <= high]
            count = len(group)
            observed = sum(item.success for item in group) / count if count else 0.5
            expected = np.mean([item.raw_signal_score / 100.0 for item in group]) if group else (low + high) / 200.0
            shrunk = (sum(item.success for item in group) + self.prior_strength * 0.5) / (count + self.prior_strength)
            raw_bins.append((low, high, float(expected), float(observed), count, float(shrunk)))

        calibrated = self._pava([row[5] for row in raw_bins], [max(row[4], 1) for row in raw_bins])
        bins = tuple(
            ReliabilityBin(
                score_min=row[0], score_max=row[1],
                expected_success_rate=round(row[2] * 100.0, 2),
                observed_success_rate=round(row[3] * 100.0, 2),
                calibrated_success_rate=round(calibrated[index] * 100.0, 2),
                sample_count=row[4],
            )
            for index, row in enumerate(raw_bins)
        )
        if eligible:
            actual = np.array([float(item.success) for item in eligible])
            expected_prob = np.array([item.raw_signal_score / 100.0 for item in eligible])
            brier = float(np.mean((expected_prob - actual) ** 2))
            ece = sum(abs(row[3] - row[2]) * row[4] for row in raw_bins) / len(eligible)
        else:
            brier = 0.25
            ece = 0.0
        version_payload = {
            "scope": [scope_type, scope_value],
            "training_end": training_end.isoformat(),
            "bins": [asdict(item) for item in bins],
            "method": "isotonic_pava_beta_shrinkage",
        }
        version = "cal5g_" + hashlib.sha256(
            json.dumps(version_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
        return CalibrationModelResult(
            version=version,
            method="isotonic_pava_beta_shrinkage",
            scope_type=scope_type,
            scope_value=scope_value,
            training_end=training_end,
            sample_count=len(eligible),
            brier_score=round(brier, 6),
            calibration_error=round(float(ece), 6),
            bins=bins,
            minimum_sample_size=self.minimum_sample_size,
        )

    def select_scope(
        self,
        observations: Iterable[CalibrationObservation],
        *,
        symbol: Optional[str],
        sector: Optional[str],
        training_end: datetime,
    ) -> CalibrationModelResult:
        eligible = [item for item in observations if item.completed_at <= training_end]
        symbol_items = [item for item in eligible if symbol and item.symbol == symbol]
        if len(symbol_items) >= self.minimum_sample_size:
            return self.fit(symbol_items, training_end=training_end, scope_type="symbol", scope_value=symbol or "")
        sector_items = [item for item in eligible if sector and item.sector == sector]
        if len(sector_items) >= self.minimum_sample_size:
            return self.fit(sector_items, training_end=training_end, scope_type="sector", scope_value=sector or "")
        return self.fit(eligible, training_end=training_end, scope_type="market", scope_value="BIST")

    @staticmethod
    def _pava(values: list[float], weights: list[int]) -> list[float]:
        blocks = [{"value": float(v), "weight": float(w), "indices": [i]} for i, (v, w) in enumerate(zip(values, weights))]
        cursor = 0
        while cursor < len(blocks) - 1:
            if blocks[cursor]["value"] <= blocks[cursor + 1]["value"] + 1e-15:
                cursor += 1
                continue
            left, right = blocks[cursor], blocks[cursor + 1]
            total_weight = left["weight"] + right["weight"]
            merged = {
                "value": (left["value"] * left["weight"] + right["value"] * right["weight"]) / total_weight,
                "weight": total_weight,
                "indices": left["indices"] + right["indices"],
            }
            blocks[cursor:cursor + 2] = [merged]
            cursor = max(0, cursor - 1)
        output = [0.5] * len(values)
        for block in blocks:
            for index in block["indices"]:
                output[index] = min(1.0, max(0.0, block["value"]))
        return output


def persist_calibration_model(db: Session, result: CalibrationModelResult) -> ScoreCalibrationModel:
    existing = db.query(ScoreCalibrationModel).filter_by(version=result.version).one_or_none()
    if existing is not None:
        return existing
    record = ScoreCalibrationModel(
        version=result.version,
        method=result.method,
        scope_type=result.scope_type,
        scope_value=result.scope_value,
        training_end=result.training_end,
        sample_count=result.sample_count,
        brier_score=result.brier_score,
        calibration_error=result.calibration_error,
        model_json=json.dumps({"minimum_sample_size": result.minimum_sample_size}, sort_keys=True),
    )
    db.add(record)
    db.flush()
    for item in result.bins:
        db.add(ScoreCalibrationBin(
            calibration_model_id=record.id,
            score_min=item.score_min,
            score_max=item.score_max,
            expected_success_rate=item.expected_success_rate,
            observed_success_rate=item.observed_success_rate,
            calibrated_success_rate=item.calibrated_success_rate,
            sample_count=item.sample_count,
        ))
    db.commit()
    db.refresh(record)
    return record


def run_calibration_training(db: Session, *, minimum_sample_size: int = 30) -> int:
    """Tamamlanmis 20 islem gunu sonuclarindan market/sektor/sembol modelleri kurar."""
    from app.models.database import SignalFeatureSnapshot, SignalOutcome
    from app.services.sector_service import get_sector_info

    rows = (
        db.query(SignalOutcome, SignalFeatureSnapshot)
        .join(SignalFeatureSnapshot, SignalOutcome.signal_snapshot_id == SignalFeatureSnapshot.id)
        .filter(
            SignalOutcome.horizon_days == 20,
            SignalOutcome.data_sufficiency == "YETERLI",
        )
        .all()
    )
    if not rows:
        return 0
    observations = []
    for outcome, snapshot in rows:
        sector_info = get_sector_info(snapshot.symbol)
        observations.append(CalibrationObservation(
            raw_signal_score=snapshot.raw_signal_score,
            success=outcome.outcome_class in {"BASARILI", "KISMEN_BASARILI"},
            completed_at=outcome.evaluated_at,
            symbol=snapshot.symbol,
            sector=sector_info.sector_name if sector_info else None,
        ))
    training_end = max(item.completed_at for item in observations)
    engine = ScoreCalibrationEngine(minimum_sample_size=minimum_sample_size)
    results = [engine.fit(observations, training_end=training_end, scope_type="market", scope_value="BIST")]
    for symbol in sorted({item.symbol for item in observations if item.symbol}):
        group = [item for item in observations if item.symbol == symbol]
        if len(group) >= minimum_sample_size:
            results.append(engine.fit(group, training_end=training_end, scope_type="symbol", scope_value=symbol or ""))
    for sector in sorted({item.sector for item in observations if item.sector}):
        group = [item for item in observations if item.sector == sector]
        if len(group) >= minimum_sample_size:
            results.append(engine.fit(group, training_end=training_end, scope_type="sector", scope_value=sector or ""))
    for result in results:
        persist_calibration_model(db, result)
    return len(results)


def lookup_calibrated_success(
    db: Session,
    *,
    score: float,
    symbol: str,
    sector: Optional[str],
    minimum_sample_size: int = 30,
) -> Optional[CalibratedSuccess]:
    scopes = [("symbol", symbol)]
    if sector:
        scopes.append(("sector", sector))
    scopes.append(("market", "BIST"))
    for scope_type, scope_value in scopes:
        model = (
            db.query(ScoreCalibrationModel)
            .filter_by(scope_type=scope_type, scope_value=scope_value)
            .order_by(ScoreCalibrationModel.training_end.desc(), ScoreCalibrationModel.id.desc())
            .first()
        )
        if model is None:
            continue
        bounded = min(100.0, max(0.0, float(score)))
        bin_record = (
            db.query(ScoreCalibrationBin)
            .filter(
                ScoreCalibrationBin.calibration_model_id == model.id,
                ScoreCalibrationBin.score_min <= bounded,
                ScoreCalibrationBin.score_max >= bounded,
            )
            .first()
        )
        if bin_record is None:
            continue
        return CalibratedSuccess(
            raw_signal_score=bounded,
            calibrated_success_rate=bin_record.calibrated_success_rate,
            calibration_sample_count=bin_record.sample_count,
            calibration_version=model.version,
            calibration_scope=f"{scope_type}:{scope_value}",
            calibration_warning=(
                "Ornek sayisi az; oran notr degere dogru daraltilmistir."
                if bin_record.sample_count < minimum_sample_size else None
            ),
            disclaimer=DISCLAIMER,
        )
    return None
