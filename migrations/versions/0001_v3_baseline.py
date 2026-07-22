"""V3 baseline: mevcut V2 semasini koru, yeni V3 tablolarini ekle

Bu migration, onceden Base.metadata.create_all() ile (Alembic izlemesi
olmadan) olusturulmus mevcut bir V2 SQLite veritabanini KAYBETMEDEN Alembic
takibine alir. checkfirst=True kullanildigi icin zaten var olan tablolara
DOKUNULMAZ; yalnizca eksik olan (V3'te eklenen) tablolar olusturulur.

Revision ID: 0001_v3_baseline
Revises:
Create Date: 2026-07-13

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.models.database import Base

# revision identifiers, used by Alembic.
revision: str = "0001_v3_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    # checkfirst=True: mevcut V2 tablolari OLDUGU GIBI birakilir, yalnizca
    # henuz var olmayan (V3'te eklenen) tablolar olusturulur. Hicbir
    # DROP/ALTER islemi yapilmaz; veri kaybi riski yoktur.
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    # Guvenlik icin downgrade yalnizca V3'te eklenen YENI tablolari kaldirir;
    # V2'den beri var olan cekirdek tablolara (signals, users, portfolio_positions,
    # vb.) ve dolayisiyla kullanicinin gecmis verisine DOKUNULMAZ.
    v3_only_tables = [
        "data_quality_logs",
        "market_breadth",
        "chart_requests",
        "alert_events",
        "price_alerts",
        "user_settings",
        "signal_performance",
        "signal_events",
        "scan_results",
        "scans",
        "sector_mappings",
        "relative_strength_snapshots",
        "market_regimes",
    ]
    bind = op.get_bind()
    for table_name in v3_only_tables:
        if table_name in Base.metadata.tables:
            Base.metadata.tables[table_name].drop(bind=bind, checkfirst=True)
