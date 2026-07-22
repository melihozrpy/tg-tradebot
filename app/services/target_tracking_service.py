from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.database import (
    TargetPerformanceSummary,
    TargetRoadmapStepRecord,
    TargetTrackingRecord,
)
from app.utils.financial_formatter import percent_change, round_money

STATUS_ACTIVE = "Aktif"
STATUS_REACHED = "Hedefe ulaşıldı"
STATUS_PARTIAL = "Kısmen ulaşıldı"
STATUS_INVALID = "Geçersiz oldu"
STATUS_EXPIRED = "Süresi doldu"
STATUS_INSUFFICIENT = "Veri yetersiz"


@dataclass
class TargetPerformanceReport:
    symbol: Optional[str]
    total_targets: int
    reached_targets: int
    partially_reached_targets: int
    invalidated_targets: int
    expired_targets: int
    success_rate: Optional[float]
    average_days_to_target: Optional[float]
    average_max_drawdown_percent: Optional[float]
    average_max_upside_percent: Optional[float]
    invalidation_rate: Optional[float]
    by_horizon: dict
    extreme_bull_success_rate: Optional[float]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def save_target_tracking(
    db: Session,
    *,
    symbol: str,
    current_price: float,
    target_low: float,
    target_high: Optional[float] = None,
    target_type: str,
    time_horizon: Optional[str],
    confidence: Optional[float],
    technical_reasons: Optional[list[str]],
    fundamental_status: Optional[str],
    invalidation_level: Optional[float],
    data_timestamp: datetime,
    market_regime: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> tuple[TargetTrackingRecord, bool]:
    symbol = symbol.upper()
    low = round_money(target_low)
    high = round_money(target_high if target_high is not None else target_low)
    current = round_money(current_price)
    if low is None or high is None or current is None or low <= 0 or high < low:
        raise ValueError("Geçerli hedef bölgesi ve güncel fiyat gerekli.")
    timestamp = _aware(data_timestamp)
    existing = (
        db.query(TargetTrackingRecord)
        .filter(
            TargetTrackingRecord.symbol == symbol,
            TargetTrackingRecord.target_type == target_type,
            TargetTrackingRecord.target_low == low,
            TargetTrackingRecord.target_high == high,
            TargetTrackingRecord.data_timestamp == timestamp,
        )
        .first()
    )
    if existing is not None:
        return existing, False
    row = TargetTrackingRecord(
        symbol=symbol, created_price=current, target_low=low, target_high=high,
        target_type=target_type, time_horizon=time_horizon, confidence=confidence,
        technical_reasons_json=json.dumps(technical_reasons or [], ensure_ascii=False),
        fundamental_status=fundamental_status, invalidation_level=round_money(invalidation_level),
        status=STATUS_ACTIVE, data_timestamp=timestamp, market_regime=market_regime,
        nearest_price=current, lowest_price_after_creation=current,
        highest_price_after_creation=current, max_drawdown_percent=0.0,
        max_upside_percent=0.0, expires_at=expires_at,
    )
    db.add(row)
    try:
        db.commit()
        db.refresh(row)
        return row, True
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(TargetTrackingRecord)
            .filter(
                TargetTrackingRecord.symbol == symbol,
                TargetTrackingRecord.target_type == target_type,
                TargetTrackingRecord.target_low == low,
                TargetTrackingRecord.target_high == high,
                TargetTrackingRecord.data_timestamp == timestamp,
            )
            .one()
        )
        return existing, False


def persist_roadmap_steps(db: Session, record: TargetTrackingRecord, roadmap) -> list[TargetRoadmapStepRecord]:
    existing_sequences = {
        seq for (seq,) in db.query(TargetRoadmapStepRecord.sequence)
        .filter(TargetRoadmapStepRecord.target_record_id == record.id).all()
    }
    rows: list[TargetRoadmapStepRecord] = []
    for step in roadmap.steps:
        if step.sequence in existing_sequences:
            continue
        row = TargetRoadmapStepRecord(
            target_record_id=record.id, symbol=record.symbol, sequence=step.sequence,
            price_low=step.price_low, price_high=step.price_high, price_mid=step.mid,
            level_type=step.level_type, confidence=step.confidence,
            breakout_condition=step.breakout_condition, volume_condition=step.volume_condition,
            next_target=step.next_target,
            correction_zone_json=json.dumps(step.correction_zone, ensure_ascii=False),
            invalidation_level=step.invalidation_level,
            estimated_duration=step.estimated_duration, status=step.status,
        )
        db.add(row)
        rows.append(row)
    if rows:
        db.commit()
    return rows


def update_target_records(
    db: Session,
    symbol: str,
    *,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    timestamp: datetime,
) -> list[TargetTrackingRecord]:
    now = _aware(timestamp)
    rows = (
        db.query(TargetTrackingRecord)
        .filter(
            TargetTrackingRecord.symbol == symbol.upper(),
            TargetTrackingRecord.status.in_([STATUS_ACTIVE, STATUS_PARTIAL]),
        )
        .all()
    )
    updated: list[TargetTrackingRecord] = []
    for row in rows:
        if now < _aware(row.data_timestamp):
            continue
        row.lowest_price_after_creation = min(row.lowest_price_after_creation or bar_low, bar_low)
        row.highest_price_after_creation = max(row.highest_price_after_creation or bar_high, bar_high)
        row.nearest_price = max(row.nearest_price or bar_close, min(bar_high, row.target_low))
        row.max_drawdown_percent = percent_change(row.lowest_price_after_creation, row.created_price)
        row.max_upside_percent = percent_change(row.highest_price_after_creation, row.created_price)
        # Aynı mumda hem hedef hem geçersizlik varsa muhafazakâr olarak
        # geçersizlik önce gelir.
        if row.invalidation_level is not None and bar_low <= row.invalidation_level:
            row.status = STATUS_INVALID
            row.invalidated_at = now
        elif bar_high >= row.target_high:
            row.status = STATUS_REACHED
            row.reached_at = now
        elif bar_high >= row.target_low:
            row.status = STATUS_PARTIAL
        elif row.expires_at is not None and now >= _aware(row.expires_at):
            row.status = STATUS_EXPIRED
        row.updated_at = datetime.now(timezone.utc)
        updated.append(row)
    if updated:
        db.commit()
    return updated


def _report_for_rows(rows: list[TargetTrackingRecord], symbol: Optional[str]) -> TargetPerformanceReport:
    total = len(rows)
    reached = [row for row in rows if row.status == STATUS_REACHED]
    partial = [row for row in rows if row.status == STATUS_PARTIAL]
    invalid = [row for row in rows if row.status == STATUS_INVALID]
    expired = [row for row in rows if row.status == STATUS_EXPIRED]
    days = [
        (_aware(row.reached_at) - _aware(row.data_timestamp)).total_seconds() / 86400
        for row in reached if row.reached_at is not None
    ]
    drawdowns = [row.max_drawdown_percent for row in rows if row.max_drawdown_percent is not None]
    upsides = [row.max_upside_percent for row in rows if row.max_upside_percent is not None]
    horizons: dict[str, dict] = {}
    for horizon in sorted({row.time_horizon or "Belirsiz" for row in rows}):
        scoped = [row for row in rows if (row.time_horizon or "Belirsiz") == horizon]
        hit = sum(row.status == STATUS_REACHED for row in scoped)
        horizons[horizon] = {"total": len(scoped), "success_rate": round(hit / len(scoped) * 100, 2) if scoped else None}
    extreme = [row for row in rows if "aşırı boğa" in row.target_type.casefold()]
    extreme_hits = sum(row.status == STATUS_REACHED for row in extreme)
    return TargetPerformanceReport(
        symbol=symbol, total_targets=total, reached_targets=len(reached),
        partially_reached_targets=len(partial), invalidated_targets=len(invalid),
        expired_targets=len(expired),
        success_rate=round(len(reached) / total * 100, 2) if total else None,
        average_days_to_target=round(mean(days), 2) if days else None,
        average_max_drawdown_percent=round(mean(drawdowns), 2) if drawdowns else None,
        average_max_upside_percent=round(mean(upsides), 2) if upsides else None,
        invalidation_rate=round(len(invalid) / total * 100, 2) if total else None,
        by_horizon=horizons,
        extreme_bull_success_rate=(round(extreme_hits / len(extreme) * 100, 2) if extreme else None),
    )


def compute_target_performance(db: Session, symbol: Optional[str] = None) -> TargetPerformanceReport:
    query = db.query(TargetTrackingRecord)
    if symbol:
        query = query.filter(TargetTrackingRecord.symbol == symbol.upper())
    return _report_for_rows(query.all(), symbol.upper() if symbol else None)


def list_target_history(db: Session, symbol: str, limit: int = 20) -> list[TargetTrackingRecord]:
    return (
        db.query(TargetTrackingRecord)
        .filter(TargetTrackingRecord.symbol == symbol.upper())
        .order_by(TargetTrackingRecord.created_at.desc())
        .limit(limit)
        .all()
    )
