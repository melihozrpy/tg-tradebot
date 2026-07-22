"""Asama 4: GDELT haber radari + haber etkisi + Groq aciklama tablolari

Bu migration SADECE yeni tablolar ekler (news_articles, news_events,
news_impact_snapshots, groq_explanations, provider_health_logs).
0001/0002'deki gibi checkfirst=True kullanilir; mevcut hicbir tabloya
DOKUNULMAZ, kullanici/portfoy/sinyal/anomali verisi kaybolmaz.

Revision ID: 0003_stage4_news_ai
Revises: 0002_stage3_anomaly
Create Date: 2026-07-16

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.models.database import Base

# revision identifiers, used by Alembic.
revision: str = "0003_stage4_news_ai"
down_revision: Union[str, None] = "0002_stage3_anomaly"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_TABLES = [
    "news_articles",
    "news_events",
    "news_impact_snapshots",
    "groq_explanations",
    "provider_health_logs",
]


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
