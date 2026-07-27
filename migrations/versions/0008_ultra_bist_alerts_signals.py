"""Kalici fiyat alarmlari ve ayrintili BIST sinyal yasam dongusu.

Revision ID: 0008_ultra_bist_alerts_signals
Revises: 0007_stage5g_backtest_paper_validation
Create Date: 2026-07-25

Migration yalnizca ekleme yapar. Eski alarm/sinyal kayitlarini degistirmez;
SQLite ve PostgreSQL uzerinde eksik tablo, sutun ve indeksleri kontrol ederek
calisir.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.models.database import Base

revision: str = "0008_ultra_bist_alerts_signals"
down_revision: Union[str, None] = "0007_stage5g_backtest_paper_validation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_TABLES = (
    "signal_targets",
    "signal_event_deliveries",
    "signal_transition_error_audits",
    "market_session_events",
    "user_price_alerts",
    "price_alert_triggers",
    "price_alert_deliveries",
    "alarm_import_jobs",
    "alarm_import_rows",
    "user_alarm_settings",
)

_NEW_SIGNAL_STATES = (
    "PENDING_ENTRY",
    "TP1_HIT",
    "TP2_HIT",
    "TP3_HIT",
    "STOPPED",
    "CLOSED_MANUALLY",
    "EXIT_PENDING",
    "UNFILLED",
    "SUSPENDED",
    "CORPORATE_ACTION_ADJUSTED",
)

_ADDITIVE_COLUMNS: dict[str, tuple[sa.Column, ...]] = {
    "signals": (
        # SQLite ALTER TABLE sonradan FK constraint ekleyemez. Fresh veritabani
        # metadata'dan FK'li kurulur; eski SQLite semada sahiplik uygulama
        # katmaninda korunur. PostgreSQL FK'si upgrade sonunda ayrica eklenir.
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("side", sa.String(8), nullable=False, server_default="BUY"),
        sa.Column("entry_order_type", sa.String(24), nullable=True),
        sa.Column("planned_entry_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("raw_planned_entry_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("actual_entry_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("requested_quantity", sa.Numeric(18, 4), nullable=True),
        sa.Column("filled_quantity", sa.Numeric(18, 4), nullable=True),
        sa.Column("remaining_quantity", sa.Numeric(18, 4), nullable=True),
        sa.Column("average_fill_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("current_stop_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("invalidation_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fill_method", sa.String(48), nullable=True),
        sa.Column("fill_source", sa.String(48), nullable=True),
        sa.Column("price_adjustment_mode", sa.String(24), nullable=True),
        sa.Column("market_rule_version", sa.String(32), nullable=True),
        sa.Column("monitoring_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source", sa.String(48), nullable=True),
    ),
    "signal_events": (
        sa.Column("event_type", sa.String(48), nullable=True),
        sa.Column("planned_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("execution_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("requested_quantity", sa.Numeric(18, 4), nullable=True),
        sa.Column("executed_quantity", sa.Numeric(18, 4), nullable=True),
        sa.Column("provider", sa.String(48), nullable=True),
        sa.Column("source", sa.String(48), nullable=True),
        sa.Column("candle_open_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("unique_dedup_key", sa.String(160), nullable=True),
    ),
    "alarm_import_rows": (
        sa.Column("base_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("percentage_value", sa.Numeric(12, 6), nullable=True),
        sa.Column("near_tolerance", sa.Numeric(18, 6), nullable=True),
        sa.Column("sound_name", sa.String(16), nullable=True),
    ),
    # A partially applied/manual 0008 schema must also be repairable.  On a
    # normal 0007 -> 0008 run this table does not exist yet and Base metadata
    # creates it below with the same column.
    "signal_event_deliveries": (
        sa.Column("payload_text", sa.Text(), nullable=True),
    ),
}


def _extend_postgresql_signal_enum(bind) -> None:
    if bind.dialect.name != "postgresql":
        return
    # Eski PostgreSQL surumlerinde ALTER TYPE ... ADD VALUE transaction icinde
    # calismaz. Alembic autocommit blogu hem eski hem yeni surumlerle uyumludur.
    with op.get_context().autocommit_block():
        for state in _NEW_SIGNAL_STATES:
            # Degerler sabit kaynak kodu tuple'indan gelir; kullanici girdisi yoktur.
            op.execute(sa.text(f"ALTER TYPE signalstateenum ADD VALUE IF NOT EXISTS '{state}'"))


def _add_missing_columns(table_name: str, columns: tuple[sa.Column, ...]) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return
    existing = {item["name"] for item in inspector.get_columns(table_name)}
    for column in columns:
        if column.name not in existing:
            op.add_column(table_name, column)


def _create_index_if_missing(
    table_name: str,
    index_name: str,
    columns: list[str],
    *,
    unique: bool = False,
) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return
    existing = {item["name"] for item in inspector.get_indexes(table_name)}
    if index_name not in existing:
        op.create_index(index_name, table_name, columns, unique=unique)


def _create_postgresql_signal_user_fk_if_missing(bind) -> None:
    if bind.dialect.name != "postgresql":
        return
    foreign_keys = sa.inspect(bind).get_foreign_keys("signals")
    name = "fk_signals_user_id_users"
    if not any(
        item.get("referred_table") == "users"
        and item.get("constrained_columns") == ["user_id"]
        for item in foreign_keys
    ):
        op.create_foreign_key(name, "signals", "users", ["user_id"], ["id"])


def _widen_postgresql_telegram_identifiers(bind) -> None:
    """Prevent valid Telegram IDs from overflowing PostgreSQL INTEGER.

    SQLite INTEGER already stores signed 64-bit values.  Fresh PostgreSQL
    tables are created from BIGINT metadata; this path repairs pre-0008 core
    tables and any partially-created 0008 table.
    """

    if bind.dialect.name != "postgresql":
        return
    targets = {
        "users": ("telegram_user_id",),
        "sector_mappings": ("set_by_telegram_user_id",),
        "signal_event_deliveries": ("telegram_user_id", "chat_id", "telegram_message_id"),
        "user_price_alerts": ("telegram_user_id", "chat_id"),
        "price_alert_deliveries": ("telegram_user_id", "chat_id", "telegram_message_id"),
        "alarm_import_jobs": ("telegram_user_id", "chat_id"),
    }
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    for table_name, column_names in targets.items():
        if table_name not in table_names:
            continue
        columns = {item["name"]: item for item in inspector.get_columns(table_name)}
        for column_name in column_names:
            info = columns.get(column_name)
            if info is None or isinstance(info["type"], sa.BigInteger):
                continue
            op.alter_column(
                table_name,
                column_name,
                existing_type=info["type"],
                type_=sa.BigInteger(),
                existing_nullable=info.get("nullable", True),
                postgresql_using=f'"{column_name}"::bigint',
            )


def upgrade() -> None:
    bind = op.get_bind()
    _extend_postgresql_signal_enum(bind)
    _widen_postgresql_telegram_identifiers(bind)

    # Once mevcut cekirdek tablolara eksik sutunlari ekle. Fresh kurulumda
    # 0001 guncel metadata ile bunlari zaten yaratmis olabilecegi icin check
    # zorunludur.
    for table_name, columns in _ADDITIVE_COLUMNS.items():
        _add_missing_columns(table_name, columns)

    for table_name in _NEW_TABLES:
        table = Base.metadata.tables.get(table_name)
        if table is None:
            raise RuntimeError(f"0008 metadata tablosu bulunamadi: {table_name}")
        table.create(bind=bind, checkfirst=True)

    _create_index_if_missing("signals", "ix_signals_user_id", ["user_id"])
    _create_postgresql_signal_user_fk_if_missing(bind)
    _create_index_if_missing(
        "signal_events",
        "ix_signal_events_unique_dedup_key",
        ["unique_dedup_key"],
        unique=True,
    )
    _create_index_if_missing(
        "signal_event_deliveries",
        "ix_signal_event_delivery_recovery",
        ["status", "attempted_at"],
    )


def downgrade() -> None:
    # Veri kaybi riskini azaltmak icin eski tablolara eklenen sutunlar ve
    # PostgreSQL enum degerleri korunur. Yalnizca 0008'e ait yeni tablolar,
    # dis anahtar bagimliliklarinin ters sirasinda kaldirilir.
    bind = op.get_bind()
    drop_order = (
        "signal_transition_error_audits",
        "market_session_events",
        "alarm_import_rows",
        "price_alert_deliveries",
        "price_alert_triggers",
        "user_price_alerts",
        "alarm_import_jobs",
        "user_alarm_settings",
        "signal_event_deliveries",
        "signal_targets",
    )
    for table_name in drop_order:
        table = Base.metadata.tables.get(table_name)
        if table is not None:
            table.drop(bind=bind, checkfirst=True)
