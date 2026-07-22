from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

import pandas as pd
from sqlalchemy.orm import Session

from app.models.database import SignalFeatureSnapshot, SignalOutcome

DEFAULT_HORIZONS = (1, 5, 20, 60)


@dataclass(frozen=True)
class SignalSnapshotInput:
    symbol: str
    signal_time: datetime
    signal_price: float
    last_confirmed_close: float
    signal_type: str
    raw_signal_score: float
    rule_based_confidence: str
    displayed_confidence: Optional[str]
    market_regime: Optional[str]
    benchmark_strength: Optional[float]
    sector_strength: Optional[float]
    liquidity_score: Optional[float]
    data_quality_score: Optional[float]
    trends: dict
    support_resistance: dict
    stop_price: Optional[float]
    targets: tuple[Optional[float], Optional[float], Optional[float]]
    news_impact: Optional[float]
    positive_contributions: list[dict]
    negative_contributions: list[dict]
    strategy_version: str
    provider: str
    price_adjustment_mode: str

    def canonical_payload(self) -> dict:
        payload = asdict(self)
        payload["signal_time"] = self.signal_time.isoformat()
        return payload


@dataclass(frozen=True)
class EvaluatedOutcome:
    horizon_days: int
    evaluated_at: datetime
    return_percent: Optional[float]
    benchmark_return_percent: Optional[float]
    excess_return_percent: Optional[float]
    maximum_favorable_excursion_percent: Optional[float]
    maximum_adverse_excursion_percent: Optional[float]
    target_hits: tuple[bool, bool, bool]
    stop_hit: bool
    outcome_class: str
    data_sufficiency: str


class SignalOutcomeTracker:
    """Sinyal ozelliklerini point-in-time saklar ve 1/5/20/60 gun izler."""

    def __init__(self, db: Session):
        self.db = db

    def capture(self, item: SignalSnapshotInput, *, signal_id: int | None = None) -> SignalFeatureSnapshot:
        payload = item.canonical_payload()
        snapshot_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if signal_id is not None:
            existing = self.db.query(SignalFeatureSnapshot).filter_by(signal_id=signal_id).one_or_none()
            if existing is not None:
                return existing
        features = {
            "trends": item.trends,
            "support_resistance": item.support_resistance,
            "stop_price": item.stop_price,
            "targets": list(item.targets),
            "news_impact": item.news_impact,
            "provider": item.provider,
            "price_adjustment_mode": item.price_adjustment_mode,
        }
        record = SignalFeatureSnapshot(
            signal_id=signal_id,
            symbol=item.symbol.upper(),
            signal_time=item.signal_time,
            signal_price=item.signal_price,
            last_confirmed_close=item.last_confirmed_close,
            signal_type=item.signal_type,
            raw_signal_score=item.raw_signal_score,
            rule_based_confidence=item.rule_based_confidence,
            displayed_confidence=item.displayed_confidence,
            market_regime=item.market_regime,
            benchmark_strength=item.benchmark_strength,
            sector_strength=item.sector_strength,
            liquidity_score=item.liquidity_score,
            data_quality_score=item.data_quality_score,
            features_json=json.dumps(features, ensure_ascii=False, sort_keys=True),
            positive_contributions_json=json.dumps(item.positive_contributions, ensure_ascii=False, sort_keys=True),
            negative_contributions_json=json.dumps(item.negative_contributions, ensure_ascii=False, sort_keys=True),
            strategy_version=item.strategy_version,
            snapshot_hash=snapshot_hash,
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def evaluate(
        self,
        snapshot: SignalFeatureSnapshot,
        bars: pd.DataFrame,
        *,
        benchmark_bars: Optional[pd.DataFrame] = None,
        horizons: Iterable[int] = DEFAULT_HORIZONS,
        custom_horizon_days: Optional[int] = None,
    ) -> list[EvaluatedOutcome]:
        frame = self._future_completed_bars(bars, snapshot.signal_time)
        benchmark = self._future_completed_bars(benchmark_bars, snapshot.signal_time) if benchmark_bars is not None else None
        features = json.loads(snapshot.features_json)
        stop = features.get("stop_price")
        targets = tuple(features.get("targets") or [None, None, None])
        requested = list(dict.fromkeys([*horizons, *([custom_horizon_days] if custom_horizon_days else [])]))
        output: list[EvaluatedOutcome] = []
        for horizon in requested:
            if horizon < 1:
                continue
            outcome = self._evaluate_horizon(snapshot, frame, benchmark, int(horizon), stop, targets)
            output.append(outcome)
            self._persist_outcome(snapshot.id, outcome)
        return output

    @staticmethod
    def _future_completed_bars(bars: Optional[pd.DataFrame], signal_time: datetime) -> pd.DataFrame:
        if bars is None or bars.empty:
            return pd.DataFrame()
        frame = bars.copy(deep=True)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
        cutoff = pd.Timestamp(signal_time)
        if frame["timestamp"].dt.tz is None and cutoff.tzinfo is not None:
            frame["timestamp"] = frame["timestamp"].dt.tz_localize(cutoff.tzinfo)
        elif frame["timestamp"].dt.tz is not None and cutoff.tzinfo is None:
            cutoff = cutoff.tz_localize(frame["timestamp"].dt.tz)
        frame = frame.loc[frame["timestamp"] > cutoff]
        if "is_complete" in frame.columns:
            frame = frame.loc[frame["is_complete"].fillna(False).astype(bool)]
        if "data_quality" in frame.columns:
            frame = frame.loc[~frame["data_quality"].fillna("VALID").astype(str).str.upper().eq("INVALID")]
        return frame.sort_values("timestamp").reset_index(drop=True)

    @staticmethod
    def _evaluate_horizon(snapshot, frame, benchmark, horizon, stop, targets) -> EvaluatedOutcome:
        if len(frame) < horizon:
            return EvaluatedOutcome(
                horizon, datetime.now(timezone.utc), None, None, None, None, None,
                (False, False, False), False, "VERI_YETERSIZ", "YETERSIZ",
            )
        sample = frame.iloc[:horizon]
        final_close = float(sample.iloc[-1]["close"])
        entry = float(snapshot.signal_price)
        ret = (final_close / entry - 1.0) * 100.0
        mfe = (float(sample["high"].max()) / entry - 1.0) * 100.0
        mae = (float(sample["low"].min()) / entry - 1.0) * 100.0
        target_hits = [False, False, False]
        stop_hit = False
        # Ayni mumda stop ve hedef varsa outcome takipcisi de iyimser davranmaz.
        for _, bar in sample.iterrows():
            bar_stop = stop is not None and float(bar["low"]) <= float(stop)
            bar_targets = [target is not None and float(bar["high"]) >= float(target) for target in targets]
            if bar_stop:
                stop_hit = True
                break
            for index, hit in enumerate(bar_targets):
                target_hits[index] = target_hits[index] or hit

        benchmark_return = None
        if benchmark is not None and len(benchmark) >= horizon:
            start = float(benchmark.iloc[0]["open"])
            end = float(benchmark.iloc[horizon - 1]["close"])
            if start > 0:
                benchmark_return = (end / start - 1.0) * 100.0
        excess = ret - benchmark_return if benchmark_return is not None else None
        if stop_hit:
            outcome_class = "STOP"
        elif target_hits[1] or target_hits[2]:
            outcome_class = "BASARILI"
        elif target_hits[0] or ret > 0:
            outcome_class = "KISMEN_BASARILI"
        elif ret < 0:
            outcome_class = "BASARISIZ"
        else:
            outcome_class = "SURESI_DOLDU"
        return EvaluatedOutcome(
            horizon_days=horizon,
            evaluated_at=pd.Timestamp(sample.iloc[-1]["timestamp"]).to_pydatetime(),
            return_percent=round(ret, 4),
            benchmark_return_percent=round(benchmark_return, 4) if benchmark_return is not None else None,
            excess_return_percent=round(excess, 4) if excess is not None else None,
            maximum_favorable_excursion_percent=round(mfe, 4),
            maximum_adverse_excursion_percent=round(mae, 4),
            target_hits=tuple(target_hits),
            stop_hit=stop_hit,
            outcome_class=outcome_class,
            data_sufficiency="YETERLI",
        )

    def _persist_outcome(self, snapshot_id: int, outcome: EvaluatedOutcome) -> SignalOutcome:
        record = self.db.query(SignalOutcome).filter_by(
            signal_snapshot_id=snapshot_id, horizon_days=outcome.horizon_days
        ).one_or_none()
        if record is None:
            record = SignalOutcome(signal_snapshot_id=snapshot_id, horizon_days=outcome.horizon_days)
            self.db.add(record)
        record.evaluated_at = outcome.evaluated_at
        record.return_percent = outcome.return_percent
        record.benchmark_return_percent = outcome.benchmark_return_percent
        record.excess_return_percent = outcome.excess_return_percent
        record.maximum_favorable_excursion_percent = outcome.maximum_favorable_excursion_percent
        record.maximum_adverse_excursion_percent = outcome.maximum_adverse_excursion_percent
        record.target_1_hit, record.target_2_hit, record.target_3_hit = outcome.target_hits
        record.stop_hit = outcome.stop_hit
        record.outcome_class = outcome.outcome_class
        record.data_sufficiency = outcome.data_sufficiency
        self.db.commit()
        return record


def run_signal_outcome_scan(db: Session, provider, *, now: Optional[datetime] = None) -> int:
    """Scheduler girisi; ag hatasi bir snapshot'i uydurma sonuc ile kapatmaz."""
    completed = 0
    snapshots = db.query(SignalFeatureSnapshot).all()
    tracker = SignalOutcomeTracker(db)
    current_time = now or datetime.now(timezone.utc)
    for snapshot in snapshots:
        try:
            bars = provider.get_ohlcv(snapshot.symbol, "1d", snapshot.signal_time, current_time)
            from app.analysis.data_quality import DataQualityEngine
            bars = DataQualityEngine().completed_candles(bars, "1d", current_time)
            tracker.evaluate(snapshot, bars)
            completed += 1
        except Exception:
            db.rollback()
    return completed
