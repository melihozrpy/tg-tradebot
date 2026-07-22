"""V3.2 (Asama 3): anormal hareket motoru tablolari

Bu migration SADECE yeni tablolar ekler (anomalies, anomaly_notifications).
0001'deki gibi checkfirst=True kullanilir; mevcut hicbir tabloya DOKUNULMAZ,
kullanici verisi kaybolmaz.

Revision ID: 0002_stage3_anomaly
Revises: 0001_v3_baseline
Create Date: 2026-07-15

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.models.database import Base

# revision identifiers, used by Alembic.
revision: str = "0002_stage3_anomaly"
down_revision: Union[str, None] = "0001_v3_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_TABLES = ["anomalies", "anomaly_notifications"]


def upgrade() -> None:
    bind = op.get_bind()
    # checkfirst=True: yalnizca eksik olan (bu asamada eklenen) tablolar
    # olusturulur; mevcut tablolara/veriye DOKUNULMAZ.
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in _NEW_TABLES:
        if table_name in Base.metadata.tables:
            Base.metadata.tables[table_name].drop(bind=bind, checkfirst=True)
