from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.database import UserPriceAlert


def owned_alert(db: Session, user_id: int, reference: str) -> UserPriceAlert | None:
    ref = reference.strip().upper()
    query = db.query(UserPriceAlert).filter(UserPriceAlert.user_id == user_id)
    if ref.startswith("ALR-"):
        return query.filter(UserPriceAlert.public_id == ref).one_or_none()
    if ref.isdigit():
        return query.filter(UserPriceAlert.id == int(ref)).one_or_none()
    return None


def page_alerts(db: Session, user_id: int, *, status: str | None = None, symbol: str | None = None,
                page: int = 1, page_size: int = 8):
    query = db.query(UserPriceAlert).filter(
        UserPriceAlert.user_id == user_id,
        UserPriceAlert.status != "DELETED",
    )
    if status:
        query = query.filter(UserPriceAlert.status == status.upper())
    if symbol:
        query = query.filter(UserPriceAlert.normalized_symbol == symbol.upper().removesuffix(".IS"))
    total = query.count()
    items = query.order_by(UserPriceAlert.created_at.desc()).offset((max(page, 1) - 1) * page_size).limit(page_size).all()
    return items, total


def due_alerts(db: Session, now: datetime):
    return db.query(UserPriceAlert).filter(
        UserPriceAlert.status.in_(["ACTIVE", "TRIGGERED", "SNOOZED", "ACKNOWLEDGED"]),
        UserPriceAlert.deleted_at.is_(None),
    ).all()
