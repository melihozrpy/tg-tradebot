from __future__ import annotations

"""Persistent virtual monitoring and Telegram outbox for staged entries."""

import json
import logging
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy.orm import Session

from app.analysis.indicator_engine import compute_indicator_bundle, evaluate_indicator_confluence
from app.analysis.staged_entry import StagedEntryLevel, StagedEntryPlan, evaluate_staged_entry
from app.models.database import StagedEntryEvent, StagedEntryRecord, User

logger = logging.getLogger("mergen_quant.staged_entry_tracking")
ACTIVE_STATUSES = ("PENDING", "CONFIRMED")


def _as_utc(value) -> datetime | None:
    if value is None:
        return None
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime()


def save_staged_entry_plan(
    db: Session,
    *,
    user: User,
    telegram_chat_id: int,
    plan: StagedEntryPlan,
) -> StagedEntryRecord:
    """Upsert the latest virtual plan; repeated /kademe replaces only that symbol."""

    row = (
        db.query(StagedEntryRecord)
        .filter(StagedEntryRecord.user_id == user.id, StagedEntryRecord.symbol == plan.symbol)
        .first()
    )
    if row is None:
        row = StagedEntryRecord(
            user_id=user.id,
            telegram_chat_id=int(telegram_chat_id),
            symbol=plan.symbol,
            direction=plan.direction,
            zone_kind=plan.zone_kind,
            zone_low=plan.zone_low,
            zone_high=plan.zone_high,
            current_price=plan.current_price,
            status=plan.status,
            levels_json="[]",
            invalidation=plan.invalidation,
            structural_entry=plan.structural_entry,
            entry_reason=plan.entry_reason,
            plan_version=1,
        )
        db.add(row)
    else:
        # Replacing a plan must not deliver stale fill notifications from the
        # previous version after the user has requested fresh levels.
        db.query(StagedEntryEvent).filter(
            StagedEntryEvent.plan_id == row.id,
            StagedEntryEvent.sent_at.is_(None),
        ).update({StagedEntryEvent.sent_at: datetime.now(timezone.utc)}, synchronize_session=False)
        row.plan_version = int(row.plan_version or 0) + 1
    row.telegram_chat_id = int(telegram_chat_id)
    row.direction = plan.direction
    row.zone_kind = plan.zone_kind
    row.zone_low = plan.zone_low
    row.zone_high = plan.zone_high
    row.current_price = plan.current_price
    row.status = plan.status
    row.levels_json = json.dumps([asdict(level) for level in plan.levels], ensure_ascii=False)
    row.invalidation = plan.invalidation
    row.target_1 = plan.target_1
    row.target_2 = plan.target_2
    row.structural_entry = plan.structural_entry
    row.entry_reason = plan.entry_reason
    row.structure_confirmed = plan.structure_confirmed
    row.rr_is_sufficient = plan.rr_is_sufficient
    row.confluence_required = plan.confluence_required
    row.last_bar_timestamp = None
    row.cancelled_reason = None
    db.commit()
    db.refresh(row)
    return row


def _plan_from_record(row: StagedEntryRecord) -> StagedEntryPlan:
    levels_payload = json.loads(row.levels_json)
    levels = tuple(StagedEntryLevel(**item) for item in levels_payload)
    return StagedEntryPlan(
        symbol=row.symbol,
        direction=row.direction,  # type: ignore[arg-type]
        zone_kind=row.zone_kind,
        zone_low=float(row.zone_low),
        zone_high=float(row.zone_high),
        current_price=float(row.current_price),
        status=row.status,  # type: ignore[arg-type]
        levels=levels,
        invalidation=float(row.invalidation),
        target_1=float(row.target_1) if row.target_1 is not None else None,
        target_2=float(row.target_2) if row.target_2 is not None else None,
        structural_entry=float(row.structural_entry),
        entry_reason=row.entry_reason,
        structure_confirmed=bool(row.structure_confirmed),
        rr_is_sufficient=bool(row.rr_is_sufficient),
        confluence_count=0,
        confluence_required=max(3, int(row.confluence_required or 3)),
        confirmations=(),
        cancelled_reason=row.cancelled_reason,
    )


def _queue_event(
    db: Session,
    row: StagedEntryRecord,
    *,
    timestamp: datetime,
    event_type: str,
    suffix: str,
    text: str,
) -> None:
    key = f"{row.id}:{row.plan_version}:{timestamp.isoformat()}:{event_type}:{suffix}"
    if db.query(StagedEntryEvent.id).filter(StagedEntryEvent.event_key == key).first():
        return
    db.add(
        StagedEntryEvent(
            plan_id=row.id,
            telegram_chat_id=row.telegram_chat_id,
            event_key=key,
            event_type=event_type,
            message_text=text,
        )
    )


def _update_record(row: StagedEntryRecord, plan: StagedEntryPlan, timestamp: datetime) -> None:
    row.current_price = plan.current_price
    row.status = plan.status
    row.levels_json = json.dumps([asdict(level) for level in plan.levels], ensure_ascii=False)
    row.cancelled_reason = plan.cancelled_reason
    row.last_bar_timestamp = timestamp


def monitor_staged_entry_plans(db: Session, provider, settings) -> dict[str, int]:
    """Evaluate one completed 15m candle per active plan and persist events."""

    rows = db.query(StagedEntryRecord).filter(StagedEntryRecord.status.in_(ACTIVE_STATUSES)).all()
    result = {"checked": 0, "confirmed": 0, "filled": 0, "invalidated": 0, "errors": 0}
    end = datetime.now(timezone.utc)
    for row in rows:
        try:
            frame = provider.get_ohlcv(row.symbol, "15m", end - timedelta(days=58), end)
            data = frame.sort_values("timestamp").reset_index(drop=True)
            if "is_complete" in data.columns:
                complete = data["is_complete"].map(
                    lambda value: str(value).strip().casefold() in {"true", "1"}
                )
                data = data.loc[complete].reset_index(drop=True)
            if len(data) < 60:
                continue
            candle = data.iloc[-1]
            timestamp = _as_utc(candle["timestamp"])
            previous_timestamp = _as_utc(row.last_bar_timestamp)
            if timestamp is None or (previous_timestamp is not None and timestamp <= previous_timestamp):
                continue
            result["checked"] += 1
            plan = _plan_from_record(row)
            plan = replace(plan, current_price=float(candle["close"]))

            if plan.status == "PENDING" and plan.zone_low <= plan.current_price <= plan.zone_high:
                bundle = compute_indicator_bundle(data, symbol=row.symbol, timeframe="15m")
                confluence = evaluate_indicator_confluence(
                    bundle,
                    "bullish" if plan.direction == "LONG" else "bearish",
                    minimum_required=plan.confluence_required,
                )
                if plan.structure_confirmed and plan.rr_is_sufficient and confluence.qualified:
                    plan = replace(
                        plan,
                        status="CONFIRMED",
                        confluence_count=len(confluence.confirmations),
                        confirmations=confluence.confirmations,
                    )
                    result["confirmed"] += 1
                    _queue_event(
                        db,
                        row,
                        timestamp=timestamp,
                        event_type="confirmed",
                        suffix="zone",
                        text=(
                            f"✅ {row.symbol} KADEMELİ PLAN ONAYLANDI\n"
                            f"Fiyat {plan.zone_low:.2f}-{plan.zone_high:.2f} bölgesinde. "
                            f"Yapı + RR + {plan.confluence_count} indikatör teyidi hazır."
                        ),
                    )

            before = {level.order for level in plan.filled_levels}
            evaluated = evaluate_staged_entry(
                plan,
                candle_low=float(candle["low"]),
                candle_high=float(candle["high"]),
                candle_close=float(candle["close"]),
            )
            # A pending plan cannot fill merely by touching a zone; it must
            # first become CONFIRMED at the completed candle close.
            if plan.status == "PENDING" and evaluated.status != "INVALIDATED":
                evaluated = plan
            after = {level.order for level in evaluated.filled_levels}
            for order in sorted(after - before):
                level = next(item for item in evaluated.levels if item.order == order)
                result["filled"] += 1
                _queue_event(
                    db,
                    row,
                    timestamp=timestamp,
                    event_type="fill",
                    suffix=str(order),
                    text=(
                        f"🪜 {row.symbol} — KADEME {order} DOLDU\n"
                        f"Fiyat: {level.price:.2f} | Pay: %{level.allocation_percent:.0f}\n"
                        f"💰 Yeni ortalama maliyet: {evaluated.average_entry:.2f}\n"
                        f"🛑 Ortak invalidation: {evaluated.invalidation:.2f}"
                    ),
                )
            if evaluated.status == "INVALIDATED" and plan.status != "INVALIDATED":
                result["invalidated"] += 1
                _queue_event(
                    db,
                    row,
                    timestamp=timestamp,
                    event_type="invalidated",
                    suffix="close",
                    text=(
                        f"⛔ {row.symbol} KADEMELİ PLAN İPTAL\n"
                        f"{evaluated.cancelled_reason}\n"
                        "Kalan sanal kademeler otomatik iptal edildi."
                    ),
                )
            _update_record(row, evaluated, timestamp)
            db.commit()
        except Exception as exc:  # noqa: BLE001 - one plan must not stop others
            db.rollback()
            result["errors"] += 1
            logger.warning("Kademeli plan izlenemedi plan=%s error=%s", row.id, type(exc).__name__)
    return result


def pending_staged_entry_events(db: Session, *, limit: int = 100) -> list[StagedEntryEvent]:
    return (
        db.query(StagedEntryEvent)
        .filter(StagedEntryEvent.sent_at.is_(None))
        .order_by(StagedEntryEvent.created_at, StagedEntryEvent.id)
        .limit(limit)
        .all()
    )


def mark_staged_event_sent(db: Session, event: StagedEntryEvent) -> None:
    event.sent_at = datetime.now(timezone.utc)
    event.attempts = int(event.attempts or 0) + 1
    db.commit()


def mark_staged_event_failed(db: Session, event: StagedEntryEvent) -> None:
    event.attempts = int(event.attempts or 0) + 1
    db.commit()
