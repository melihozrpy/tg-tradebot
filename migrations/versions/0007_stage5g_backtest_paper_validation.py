"""MERGEN QUANT Asama 5g: backtest, sanal islem ve sinyal dogrulama.

Revision ID: 0007_stage5g_backtest_paper_validation
Revises: 0006_stage5e_long_term_targets_valuation
Create Date: 2026-07-18

Migration yalnizca ekleme yapar. Var olan backtest/paper kayitlarini degistirmez
ve SQLite uzerinde hem eski hem de fresh veritabaniyla calisir.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.models.database import Base

revision: str = "0007_stage5g_backtest_paper_validation"
down_revision: Union[str, None] = "0006_stage5e_long_term_targets_valuation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_TABLES = [
    "backtest_windows",
    "backtest_daily_equity",
    "backtest_metrics",
    "paper_accounts",
    "paper_trade_events",
    "signal_feature_snapshots",
    "signal_outcomes",
    "score_calibration_models",
    "score_calibration_bins",
    "signal_score_contributions",
    "validation_reports",
]

_ADDITIVE_COLUMNS: dict[str, list[sa.Column]] = {
    "backtest_runs": [
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("scope", sa.String(32), nullable=True),
        sa.Column("sector", sa.String(128), nullable=True),
        sa.Column("strategy_name", sa.String(64), nullable=True),
        sa.Column("market_regime_filter", sa.String(32), nullable=True),
        sa.Column("config_snapshot", sa.Text(), nullable=True),
        sa.Column("data_version", sa.String(64), nullable=True),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("price_adjustment_mode", sa.String(16), nullable=True),
        sa.Column("transaction_cost_config", sa.Text(), nullable=True),
        sa.Column("seed", sa.Integer(), nullable=True),
        sa.Column("run_status", sa.String(24), nullable=True),
        sa.Column("progress_percent", sa.Float(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    ],
    "backtest_trades": [
        sa.Column("gross_pnl", sa.Float(), nullable=True),
        sa.Column("total_cost", sa.Float(), nullable=True),
        sa.Column("net_return_percent", sa.Float(), nullable=True),
        sa.Column("stop_price", sa.Float(), nullable=True),
        sa.Column("target_1", sa.Float(), nullable=True),
        sa.Column("target_2", sa.Float(), nullable=True),
        sa.Column("target_3", sa.Float(), nullable=True),
        sa.Column("target_1_hit", sa.Boolean(), nullable=True),
        sa.Column("target_2_hit", sa.Boolean(), nullable=True),
        sa.Column("target_3_hit", sa.Boolean(), nullable=True),
        sa.Column("mae_percent", sa.Float(), nullable=True),
        sa.Column("mfe_percent", sa.Float(), nullable=True),
        sa.Column("holding_bars", sa.Integer(), nullable=True),
        sa.Column("market_regime", sa.String(32), nullable=True),
        sa.Column("sector", sa.String(128), nullable=True),
        sa.Column("signal_type", sa.String(32), nullable=True),
        sa.Column("raw_signal_score", sa.Float(), nullable=True),
    ],
    "paper_trades": [
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("signal_id", sa.Integer(), nullable=True),
        sa.Column("signal_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("original_quantity", sa.Float(), nullable=True),
        sa.Column("remaining_quantity", sa.Float(), nullable=True),
        sa.Column("stop_price", sa.Float(), nullable=True),
        sa.Column("target_1", sa.Float(), nullable=True),
        sa.Column("target_2", sa.Float(), nullable=True),
        sa.Column("target_3", sa.Float(), nullable=True),
        sa.Column("partial_exit_config", sa.Text(), nullable=True),
        sa.Column("current_price", sa.Float(), nullable=True),
        sa.Column("realized_pnl", sa.Float(), nullable=True),
        sa.Column("unrealized_pnl", sa.Float(), nullable=True),
        sa.Column("max_favorable_pnl", sa.Float(), nullable=True),
        sa.Column("max_adverse_pnl", sa.Float(), nullable=True),
        sa.Column("close_reason", sa.String(32), nullable=True),
        sa.Column("data_provider", sa.String(32), nullable=True),
        sa.Column("data_quality", sa.String(16), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    ],
}


def _add_missing_columns(table_name: str, columns: list[sa.Column]) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return
    existing = {item["name"] for item in inspector.get_columns(table_name)}
    for column in columns:
        if column.name not in existing:
            op.add_column(table_name, column)


def upgrade() -> None:
    # Fresh DB'de onceki migrationlar guncel metadata ile bu tablolari zaten
    # yaratmis olabilir; checkfirst bu nedenle zorunludur.
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)
    for table_name, columns in _ADDITIVE_COLUMNS.items():
        _add_missing_columns(table_name, columns)


def downgrade() -> None:
    # Geri alma da eski tablolardan sutun/veri silmez. Yalnizca 5g'ye ait yeni
    # tablolari kaldirir; bu, kullanici verisini koruyan ihtiyatli davranistir.
    bind = op.get_bind()
    for table_name in reversed(_NEW_TABLES):
        table = Base.metadata.tables.get(table_name)
        if table is not None:
            table.drop(bind=bind, checkfirst=True)
