"""Asama 5 (MERGEN QUANT): cok zamanli seviyeler, cakisan bolgeler,
fiyat senaryolari, kirilim senaryolari, donemsel goreceli guc ve
gelismis alarm olay tablolari.

Bu migration SADECE yeni tablolar ekler (timeframe_levels, confluence_zones,
price_scenarios, breakout_scenarios, relative_strength_periods,
enhanced_alert_events). 0001/0002/0003'teki gibi checkfirst=True kullanilir;
mevcut hicbir tabloya DOKUNULMAZ, kullanici/portfoy/sinyal/haber/anomali/
alarm verisi kaybolmaz. Hem fresh database hem de mevcut Stage 4
database uzerinde sorunsuz calisir; tekrar calistirildiginda da
veritabanini bozmaz (checkfirst=True idempotenttir).

Revision ID: 0004_stage5_mergen_levels
Revises: 0003_stage4_news_ai
Create Date: 2026-07-17

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from app.models.database import Base

# revision identifiers, used by Alembic.
revision: str = "0004_stage5_mergen_levels"
down_revision: Union[str, None] = "0003_stage4_news_ai"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_TABLES = [
    "timeframe_levels",
    "confluence_zones",
    "price_scenarios",
    "breakout_scenarios",
    "relative_strength_periods",
    "enhanced_alert_events",
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
