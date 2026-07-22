"""MERGEN QUANT Aşama 5e: uzun hedefler, değerleme ve sermaye işlemleri.

Revision ID: 0006_stage5e_long_term_targets_valuation
Revises: 0005_stage5d_reliability_alerts_charts
Create Date: 2026-07-18
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.models.database import Base

revision: str = "0006_stage5e_long_term_targets_valuation"
down_revision: Union[str, None] = "0005_stage5d_reliability_alerts_charts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_TABLES = [
    "long_term_scenarios",
    "user_price_targets",
    "target_tracking_records",
    "target_roadmap_steps",
    "valuation_snapshots",
    "corporate_action_events",
    "target_realism_snapshots",
    "target_performance_summaries",
]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in reversed(_NEW_TABLES):
        table = Base.metadata.tables.get(table_name)
        if table is not None:
            table.drop(bind=bind, checkfirst=True)
