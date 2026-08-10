"""Add durable deduplication for scheduled Telegram broadcasts.

Revision ID: 0011_scheduled_delivery_dedup
Revises: 0010_technical_screener_news_cache
Create Date: 2026-08-10
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.models.database import Base

revision: str = "0011_scheduled_delivery_dedup"
down_revision: Union[str, None] = "0010_technical_screener_news_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    table = Base.metadata.tables.get("scheduled_message_deliveries")
    if table is None:
        raise RuntimeError("0011 metadata tablosu bulunamadı: scheduled_message_deliveries")
    table.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    table = Base.metadata.tables.get("scheduled_message_deliveries")
    if table is not None:
        table.drop(bind=op.get_bind(), checkfirst=True)
