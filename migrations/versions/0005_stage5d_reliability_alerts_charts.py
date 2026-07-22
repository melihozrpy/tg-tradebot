"""MERGEN QUANT Asama 5d: veri güvenilirliği, gelişmiş alarm ve grafik cache.

Revision ID: 0005_stage5d_reliability_alerts_charts
Revises: 0004_stage5_mergen_levels
Create Date: 2026-07-17

Migration yalnızca yeni tablolar ekler. Mevcut kullanıcı, portföy, sinyal,
haber, alarm, seviye ve senaryo tablolarına/verilerine dokunmaz.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.models.database import Base

revision: str = "0005_stage5d_reliability_alerts_charts"
down_revision: Union[str, None] = "0004_stage5_mergen_levels"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_TABLES = [
    "data_quality_snapshots",
    "provider_health_events",
    "provider_circuit_breakers",
    "enhanced_alarm_rules",
    "enhanced_alarm_trigger_events",
    "chart_cache_metadata",
]


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in reversed(_NEW_TABLES):
        table = Base.metadata.tables.get(table_name)
        if table is not None:
            table.drop(bind=bind, checkfirst=True)
