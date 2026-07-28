"""SMXM günlük rapor kayıtları ve çoklu sanal portföyler.

Revision ID: 0009_smxm_reports_virtual_portfolios
Revises: 0008_ultra_bist_alerts_signals
Create Date: 2026-07-28

Yalnızca yeni tablo ekler; mevcut paper/backtest verilerine dokunmaz.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.models.database import Base

revision: str = "0009_smxm_reports_virtual_portfolios"
down_revision: Union[str, None] = "0008_ultra_bist_alerts_signals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TABLES = (
    "virtual_portfolios",
    "virtual_trades",
    "market_daily_report_logs",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user_price_alerts" in inspector.get_table_names():
        alert_columns = {
            column["name"] for column in inspector.get_columns("user_price_alerts")
        }
        if "deleted_at" not in alert_columns:
            op.add_column(
                "user_price_alerts",
                sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            )
    for table_name in _TABLES:
        table = Base.metadata.tables.get(table_name)
        if table is None:
            raise RuntimeError(f"0009 metadata tablosu bulunamadı: {table_name}")
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in reversed(_TABLES):
        table = Base.metadata.tables.get(table_name)
        if table is not None:
            table.drop(bind=bind, checkfirst=True)
