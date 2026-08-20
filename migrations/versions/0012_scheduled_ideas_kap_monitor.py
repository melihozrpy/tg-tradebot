"""Add auditable scheduled idea and KAP monitor records.

Revision ID: 0012_scheduled_ideas_kap_monitor
Revises: 0011_scheduled_delivery_dedup
Create Date: 2026-08-20
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.models.database import Base


revision: str = "0012_scheduled_ideas_kap_monitor"
down_revision: Union[str, None] = "0011_scheduled_delivery_dedup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    for name in ("scheduled_trade_ideas", "kap_monitor_events"):
        table = Base.metadata.tables.get(name)
        if table is None:
            raise RuntimeError(f"0012 metadata tablosu bulunamadı: {name}")
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for name in ("kap_monitor_events", "scheduled_trade_ideas"):
        table = Base.metadata.tables.get(name)
        if table is not None:
            table.drop(bind=bind, checkfirst=True)
