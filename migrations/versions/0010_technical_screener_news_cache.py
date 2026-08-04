"""Add technical scanner/news state and persistent virtual staged-entry tracking.

Revision ID: 0010_technical_screener_news_cache
Revises: 0009_smxm_reports_virtual_portfolios
Create Date: 2026-08-04

Only new tables are created; no existing row or column is modified.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.models.database import Base

revision: str = "0010_technical_screener_news_cache"
down_revision: Union[str, None] = "0009_smxm_reports_virtual_portfolios"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (
    "ema_cross_state",
    "rsi_alert_state",
    "news_cache",
    "staged_entry_plans",
    "staged_entry_events",
)


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in _TABLES:
        table = Base.metadata.tables.get(table_name)
        if table is None:
            raise RuntimeError(f"0010 metadata tablosu bulunamadi: {table_name}")
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in reversed(_TABLES):
        table = Base.metadata.tables.get(table_name)
        if table is not None:
            table.drop(bind=bind, checkfirst=True)
