"""Durable de-duplication for scheduler-originated Telegram broadcasts."""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.database import ScheduledMessageDelivery

logger = logging.getLogger("mergen_quant.scheduled_delivery_dedup")


def claim_scheduled_delivery(db: Session, *, dedup_key: str, chat_id: int) -> bool:
    """Claim one scheduled broadcast for one chat exactly once.

    The database unique constraint, rather than an in-memory set, is used so
    an old/overlapping container cannot send a second copy during deployment.
    A database fault fails closed for that delivery: duplicate Telegram spam is
    worse than waiting for the next scheduled report.
    """

    key = str(dedup_key).strip()[:192]
    if not key:
        raise ValueError("scheduled delivery dedup key boş olamaz")
    row = ScheduledMessageDelivery(dedup_key=key, chat_id=int(chat_id))
    db.add(row)
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning("Zamanlanmış bildirim tekilleştirme başarısız: %s", type(exc).__name__)
        return False
