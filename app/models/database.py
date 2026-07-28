from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    Index,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

from app.config.settings import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Kullanicilar / izleme listesi
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    # Telegram user/chat identifiers are signed 64-bit values.  PostgreSQL
    # INTEGER is only 32-bit and already rejects valid modern Telegram IDs.
    telegram_user_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(128), nullable=True)
    is_admin = Column(Boolean, default=False, nullable=False)
    total_capital = Column(Float, default=100000.0, nullable=False)
    cash_balance = Column(Float, nullable=True)  # V3: /nakit_ayarla ile yonetilir; None ise ayri takip edilmiyor demektir
    risk_per_trade_percent = Column(Float, default=0.75, nullable=False)
    maximum_daily_loss_percent = Column(Float, default=2.0, nullable=False)
    maximum_open_positions = Column(Integer, default=5, nullable=False)
    kill_switch_active = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    watchlist_items = relationship("WatchlistItem", back_populates="user", cascade="all, delete-orphan")
    portfolio_positions = relationship("PortfolioPosition", back_populates="user", cascade="all, delete-orphan")


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("user_id", "symbol", name="uq_watchlist_user_symbol"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    symbol = Column(String(16), nullable=False, index=True)
    active_timeframes = Column(String(64), default="1d", nullable=False)
    minimum_signal_score = Column(Float, default=65.0, nullable=False)
    alarm_cooldown_minutes = Column(Integer, default=120, nullable=False)
    user_cost = Column(Float, nullable=True)
    user_lot = Column(Float, nullable=True)
    custom_support = Column(Float, nullable=True)
    custom_resistance = Column(Float, nullable=True)
    kap_alert_enabled = Column(Boolean, default=False, nullable=False)
    broker_flow_alert_enabled = Column(Boolean, default=False, nullable=False)
    is_muted = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    user = relationship("User", back_populates="watchlist_items")


# ---------------------------------------------------------------------------
# Piyasa verisi onbellegi
# ---------------------------------------------------------------------------


class OHLCVCache(Base):
    __tablename__ = "ohlcv_cache"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "timestamp", name="uq_ohlcv_symbol_tf_ts"),
    )

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    timeframe = Column(String(8), nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)
    provider = Column(String(32), nullable=False)
    fetched_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class SystemHealth(Base):
    __tablename__ = "system_health"

    id = Column(Integer, primary_key=True)
    component = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False)  # ok | degraded | down
    detail = Column(Text, nullable=True)
    checked_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Sinyaller
# ---------------------------------------------------------------------------


class SignalStateEnum(str, enum.Enum):
    CREATED = "CREATED"
    PREVIEW = "PREVIEW"  # V3: gun ici on analiz, kesin sinyal degil
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    WAITING_TRIGGER = "WAITING_TRIGGER"  # V3: kesinlesmis ama tetik seviyesi henuz gecilmedi
    CONFIRMED = "CONFIRMED"
    SENT = "SENT"
    ACTIVE = "ACTIVE"
    TARGET_1_HIT = "TARGET_1_HIT"
    TARGET_2_HIT = "TARGET_2_HIT"
    TARGET_3_HIT = "TARGET_3_HIT"
    STOP_HIT = "STOP_HIT"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    PENDING_ENTRY = "PENDING_ENTRY"
    TP1_HIT = "TP1_HIT"
    TP2_HIT = "TP2_HIT"
    TP3_HIT = "TP3_HIT"
    STOPPED = "STOPPED"
    CLOSED_MANUALLY = "CLOSED_MANUALLY"
    EXIT_PENDING = "EXIT_PENDING"
    UNFILLED = "UNFILLED"
    SUSPENDED = "SUSPENDED"
    CORPORATE_ACTION_ADJUSTED = "CORPORATE_ACTION_ADJUSTED"


class SignalTypeEnum(str, enum.Enum):
    STRONG_BUY_CANDIDATE = "STRONG_BUY_CANDIDATE"
    BUY_CANDIDATE = "BUY_CANDIDATE"
    WATCH = "WATCH"
    NEUTRAL = "NEUTRAL"
    WEAK_RISK = "WEAK_RISK"
    REDUCE_POSITION = "REDUCE_POSITION"
    STRONG_RISK = "STRONG_RISK"


class Signal(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    timeframe = Column(String(8), nullable=False)
    signal_type = Column(Enum(SignalTypeEnum), nullable=False)
    state = Column(Enum(SignalStateEnum), default=SignalStateEnum.CREATED, nullable=False)
    score = Column(Float, nullable=False)
    confidence = Column(String(16), nullable=False)  # dusuk | orta | yuksek
    entry_zone_low = Column(Float, nullable=True)
    entry_zone_high = Column(Float, nullable=True)
    entry_trigger = Column(Float, nullable=True)
    stop_price = Column(Float, nullable=True)
    target_1 = Column(Float, nullable=True)
    target_2 = Column(Float, nullable=True)
    target_3 = Column(Float, nullable=True)
    risk_reward = Column(Float, nullable=True)
    market_regime = Column(String(32), nullable=True)
    relative_strength_score = Column(Float, nullable=True)
    sector_strength_score = Column(Float, nullable=True)
    analysis_mode = Column(String(24), default="confirmed_close", nullable=False)  # confirmed_close | intraday_preview
    trading_date = Column(DateTime(timezone=True), nullable=True)  # verinin ait oldugu islem gunu (UTC gece yarisi)
    strategy_version = Column(String(16), nullable=False)
    data_timestamp = Column(DateTime(timezone=True), nullable=False)
    provider = Column(String(32), nullable=False)
    source = Column(String(48), nullable=True)
    idempotency_key = Column(String(128), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    side = Column(String(8), nullable=False, default="BUY")
    entry_order_type = Column(String(24), nullable=True)
    planned_entry_price = Column(Numeric(18, 6), nullable=True)
    raw_planned_entry_price = Column(Numeric(18, 6), nullable=True)
    actual_entry_price = Column(Numeric(18, 6), nullable=True)
    requested_quantity = Column(Numeric(18, 4), nullable=True)
    filled_quantity = Column(Numeric(18, 4), nullable=True)
    remaining_quantity = Column(Numeric(18, 4), nullable=True)
    average_fill_price = Column(Numeric(18, 6), nullable=True)
    current_stop_price = Column(Numeric(18, 6), nullable=True)
    invalidation_price = Column(Numeric(18, 6), nullable=True)
    valid_from = Column(DateTime(timezone=True), nullable=True)
    activated_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    fill_method = Column(String(48), nullable=True)
    fill_source = Column(String(48), nullable=True)
    price_adjustment_mode = Column(String(24), nullable=True)
    market_rule_version = Column(String(32), nullable=True)
    monitoring_enabled = Column(Boolean, nullable=False, default=True)
    row_version = Column(Integer, nullable=False, default=1)

    reasons = relationship("SignalReason", back_populates="signal", cascade="all, delete-orphan")
    events = relationship("SignalEvent", back_populates="signal", cascade="all, delete-orphan")


class SignalReason(Base):
    __tablename__ = "signal_reasons"

    id = Column(Integer, primary_key=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=False)
    category = Column(String(32), nullable=False)  # trend|volume|momentum|regime|risk
    description = Column(Text, nullable=False)
    is_risk = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    signal = relationship("Signal", back_populates="reasons")


class AlertDelivery(Base):
    __tablename__ = "alert_deliveries"

    id = Column(Integer, primary_key=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    symbol = Column(String(16), nullable=False, index=True)
    message_type = Column(String(32), nullable=False)
    delivered_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"

    id = Column(Integer, primary_key=True)
    version = Column(String(16), unique=True, nullable=False)
    config_snapshot = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Portfoy
# ---------------------------------------------------------------------------


class PortfolioPosition(Base):
    __tablename__ = "portfolio_positions"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    symbol = Column(String(16), nullable=False, index=True)
    lot = Column(Float, nullable=False)
    average_cost = Column(Float, nullable=False)
    stop_price = Column(Float, nullable=True)
    target_1 = Column(Float, nullable=True)
    target_2 = Column(Float, nullable=True)
    strategy = Column(String(64), nullable=True)
    risk_amount = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)
    opened_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    closed_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="portfolio_positions")


# ---------------------------------------------------------------------------
# Paper trading
# ---------------------------------------------------------------------------


class PaperOrder(Base):
    __tablename__ = "paper_orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    symbol = Column(String(16), nullable=False, index=True)
    side = Column(String(4), nullable=False)  # BUY | SELL
    order_type = Column(String(8), nullable=False)  # MARKET | LIMIT
    quantity = Column(Float, nullable=False)
    limit_price = Column(Float, nullable=True)
    status = Column(String(16), default="PENDING", nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("paper_orders.id"), nullable=False)
    symbol = Column(String(16), nullable=False, index=True)
    side = Column(String(4), nullable=False)
    quantity = Column(Float, nullable=False)
    fill_price = Column(Float, nullable=False)
    commission = Column(Float, default=0.0, nullable=False)
    slippage = Column(Float, default=0.0, nullable=False)
    executed_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)
    signal_snapshot_id = Column(Integer, nullable=True)
    status = Column(String(32), default="ACTIVE", nullable=True, index=True)
    opened_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    entry_price = Column(Float, nullable=True)
    original_quantity = Column(Float, nullable=True)
    remaining_quantity = Column(Float, nullable=True)
    stop_price = Column(Float, nullable=True)
    target_1 = Column(Float, nullable=True)
    target_2 = Column(Float, nullable=True)
    target_3 = Column(Float, nullable=True)
    partial_exit_config = Column(Text, nullable=True)
    current_price = Column(Float, nullable=True)
    realized_pnl = Column(Float, default=0.0, nullable=True)
    unrealized_pnl = Column(Float, default=0.0, nullable=True)
    max_favorable_pnl = Column(Float, default=0.0, nullable=True)
    max_adverse_pnl = Column(Float, default=0.0, nullable=True)
    close_reason = Column(String(32), nullable=True)
    data_provider = Column(String(32), nullable=True)
    data_quality = Column(String(16), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=True)


class DailyPerformance(Base):
    __tablename__ = "daily_performance"
    __table_args__ = (UniqueConstraint("scope", "trade_date", name="uq_daily_perf_scope_date"),)

    id = Column(Integer, primary_key=True)
    scope = Column(String(16), nullable=False)  # paper | backtest
    trade_date = Column(DateTime(timezone=True), nullable=False)
    realized_pnl = Column(Float, default=0.0, nullable=False)
    unrealized_pnl = Column(Float, default=0.0, nullable=False)
    equity = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    timeframe = Column(String(8), nullable=False)
    start_date = Column(DateTime(timezone=True), nullable=False)
    end_date = Column(DateTime(timezone=True), nullable=False)
    initial_capital = Column(Float, nullable=False)
    commission_percent = Column(Float, default=0.0, nullable=False)
    slippage_percent = Column(Float, default=0.0, nullable=False)
    strategy_version = Column(String(16), nullable=False)
    metrics_json = Column(Text, nullable=True)
    status = Column(String(16), default="COMPLETED", nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    run_id = Column(String(64), nullable=True, unique=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    scope = Column(String(32), default="symbol", nullable=True)
    sector = Column(String(128), nullable=True)
    strategy_name = Column(String(64), nullable=True)
    market_regime_filter = Column(String(32), nullable=True)
    config_snapshot = Column(Text, nullable=True)
    data_version = Column(String(64), nullable=True)
    provider = Column(String(32), nullable=True)
    price_adjustment_mode = Column(String(16), nullable=True)
    transaction_cost_config = Column(Text, nullable=True)
    seed = Column(Integer, nullable=True)
    run_status = Column(String(24), default="PENDING", nullable=True, index=True)
    progress_percent = Column(Float, default=0.0, nullable=True)
    error_detail = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=True)


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id = Column(Integer, primary_key=True)
    backtest_run_id = Column(Integer, ForeignKey("backtest_runs.id"), nullable=False)
    symbol = Column(String(16), nullable=False)
    side = Column(String(4), nullable=False)
    entry_time = Column(DateTime(timezone=True), nullable=False)
    exit_time = Column(DateTime(timezone=True), nullable=True)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    quantity = Column(Float, nullable=False)
    pnl = Column(Float, nullable=True)
    exit_reason = Column(String(32), nullable=True)
    gross_pnl = Column(Float, nullable=True)
    total_cost = Column(Float, default=0.0, nullable=True)
    net_return_percent = Column(Float, nullable=True)
    stop_price = Column(Float, nullable=True)
    target_1 = Column(Float, nullable=True)
    target_2 = Column(Float, nullable=True)
    target_3 = Column(Float, nullable=True)
    target_1_hit = Column(Boolean, default=False, nullable=True)
    target_2_hit = Column(Boolean, default=False, nullable=True)
    target_3_hit = Column(Boolean, default=False, nullable=True)
    mae_percent = Column(Float, nullable=True)
    mfe_percent = Column(Float, nullable=True)
    holding_bars = Column(Integer, nullable=True)
    market_regime = Column(String(32), nullable=True)
    sector = Column(String(128), nullable=True)
    signal_type = Column(String(32), nullable=True)
    raw_signal_score = Column(Float, nullable=True)


class BacktestWindow(Base):
    __tablename__ = "backtest_windows"
    __table_args__ = (UniqueConstraint("backtest_run_id", "window_index", name="uq_backtest_window_run_index"),)

    id = Column(Integer, primary_key=True)
    backtest_run_id = Column(Integer, ForeignKey("backtest_runs.id"), nullable=False, index=True)
    window_index = Column(Integer, nullable=False)
    mode = Column(String(16), nullable=False)
    train_start = Column(DateTime(timezone=True), nullable=False)
    train_end = Column(DateTime(timezone=True), nullable=False)
    validation_start = Column(DateTime(timezone=True), nullable=False)
    validation_end = Column(DateTime(timezone=True), nullable=False)
    test_start = Column(DateTime(timezone=True), nullable=False)
    test_end = Column(DateTime(timezone=True), nullable=False)
    selected_parameters = Column(Text, nullable=True)
    in_sample_metrics = Column(Text, nullable=True)
    validation_metrics = Column(Text, nullable=True)
    out_of_sample_metrics = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class BacktestDailyEquity(Base):
    __tablename__ = "backtest_daily_equity"
    __table_args__ = (UniqueConstraint("backtest_run_id", "trading_date", name="uq_backtest_equity_run_date"),)

    id = Column(Integer, primary_key=True)
    backtest_run_id = Column(Integer, ForeignKey("backtest_runs.id"), nullable=False, index=True)
    trading_date = Column(DateTime(timezone=True), nullable=False)
    strategy_equity = Column(Float, nullable=False)
    benchmark_equity = Column(Float, nullable=True)
    drawdown_percent = Column(Float, nullable=True)
    exposure_percent = Column(Float, nullable=True)


class BacktestMetric(Base):
    __tablename__ = "backtest_metrics"
    __table_args__ = (UniqueConstraint("backtest_run_id", "scope_type", "scope_value", name="uq_backtest_metric_scope"),)

    id = Column(Integer, primary_key=True)
    backtest_run_id = Column(Integer, ForeignKey("backtest_runs.id"), nullable=False, index=True)
    scope_type = Column(String(32), nullable=False)
    scope_value = Column(String(128), nullable=False)
    metrics_json = Column(Text, nullable=False)
    sample_count = Column(Integer, default=0, nullable=False)
    evidence_class = Column(String(32), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class PaperAccount(Base):
    __tablename__ = "paper_accounts"
    __table_args__ = (UniqueConstraint("user_id", name="uq_paper_account_user"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    initial_capital = Column(Float, nullable=False)
    cash_balance = Column(Float, nullable=False)
    realized_pnl = Column(Float, default=0.0, nullable=False)
    currency = Column(String(8), default="TRY", nullable=False)
    status = Column(String(16), default="ACTIVE", nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class PaperTradeEvent(Base):
    __tablename__ = "paper_trade_events"

    id = Column(Integer, primary_key=True)
    paper_trade_id = Column(Integer, ForeignKey("paper_trades.id"), nullable=False, index=True)
    event_type = Column(String(32), nullable=False)
    event_time = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    price = Column(Float, nullable=True)
    quantity = Column(Float, nullable=True)
    commission = Column(Float, default=0.0, nullable=False)
    slippage = Column(Float, default=0.0, nullable=False)
    payload_json = Column(Text, nullable=True)


class SignalFeatureSnapshot(Base):
    __tablename__ = "signal_feature_snapshots"

    id = Column(Integer, primary_key=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True, unique=True, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    signal_time = Column(DateTime(timezone=True), nullable=False)
    signal_price = Column(Float, nullable=False)
    last_confirmed_close = Column(Float, nullable=False)
    signal_type = Column(String(32), nullable=False)
    raw_signal_score = Column(Float, nullable=False)
    rule_based_confidence = Column(String(24), nullable=False)
    displayed_confidence = Column(String(24), nullable=True)
    market_regime = Column(String(32), nullable=True)
    benchmark_strength = Column(Float, nullable=True)
    sector_strength = Column(Float, nullable=True)
    liquidity_score = Column(Float, nullable=True)
    data_quality_score = Column(Float, nullable=True)
    features_json = Column(Text, nullable=False)
    positive_contributions_json = Column(Text, nullable=True)
    negative_contributions_json = Column(Text, nullable=True)
    strategy_version = Column(String(32), nullable=False)
    snapshot_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class SignalOutcome(Base):
    __tablename__ = "signal_outcomes"
    __table_args__ = (UniqueConstraint("signal_snapshot_id", "horizon_days", name="uq_signal_outcome_snapshot_horizon"),)

    id = Column(Integer, primary_key=True)
    signal_snapshot_id = Column(Integer, ForeignKey("signal_feature_snapshots.id"), nullable=False, index=True)
    horizon_days = Column(Integer, nullable=False)
    evaluated_at = Column(DateTime(timezone=True), nullable=False)
    return_percent = Column(Float, nullable=True)
    benchmark_return_percent = Column(Float, nullable=True)
    excess_return_percent = Column(Float, nullable=True)
    maximum_favorable_excursion_percent = Column(Float, nullable=True)
    maximum_adverse_excursion_percent = Column(Float, nullable=True)
    target_1_hit = Column(Boolean, default=False, nullable=False)
    target_2_hit = Column(Boolean, default=False, nullable=False)
    target_3_hit = Column(Boolean, default=False, nullable=False)
    stop_hit = Column(Boolean, default=False, nullable=False)
    outcome_class = Column(String(32), nullable=False)
    data_sufficiency = Column(String(24), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class ScoreCalibrationModel(Base):
    __tablename__ = "score_calibration_models"

    id = Column(Integer, primary_key=True)
    version = Column(String(32), nullable=False, unique=True)
    method = Column(String(32), nullable=False)
    scope_type = Column(String(16), nullable=False)
    scope_value = Column(String(128), nullable=False)
    training_end = Column(DateTime(timezone=True), nullable=False)
    sample_count = Column(Integer, nullable=False)
    brier_score = Column(Float, nullable=True)
    calibration_error = Column(Float, nullable=True)
    model_json = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class ScoreCalibrationBin(Base):
    __tablename__ = "score_calibration_bins"
    __table_args__ = (UniqueConstraint("calibration_model_id", "score_min", "score_max", name="uq_calibration_bin_range"),)

    id = Column(Integer, primary_key=True)
    calibration_model_id = Column(Integer, ForeignKey("score_calibration_models.id"), nullable=False, index=True)
    score_min = Column(Integer, nullable=False)
    score_max = Column(Integer, nullable=False)
    expected_success_rate = Column(Float, nullable=False)
    observed_success_rate = Column(Float, nullable=False)
    calibrated_success_rate = Column(Float, nullable=False)
    sample_count = Column(Integer, nullable=False)


class SignalScoreContribution(Base):
    __tablename__ = "signal_score_contributions"

    id = Column(Integer, primary_key=True)
    signal_snapshot_id = Column(Integer, ForeignKey("signal_feature_snapshots.id"), nullable=False, index=True)
    factor_key = Column(String(64), nullable=False)
    description = Column(Text, nullable=False)
    contribution = Column(Float, nullable=False)
    source_engine = Column(String(64), nullable=False)
    source_field = Column(String(128), nullable=False)
    data_available = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class ValidationReport(Base):
    __tablename__ = "validation_reports"

    id = Column(Integer, primary_key=True)
    backtest_run_id = Column(Integer, ForeignKey("backtest_runs.id"), nullable=True, index=True)
    report_type = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False)
    evidence_class = Column(String(32), nullable=True)
    checks_json = Column(Text, nullable=False)
    strategy_version = Column(String(32), nullable=True)
    data_version = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    actor = Column(String(64), nullable=False)  # system | telegram:<id> | admin:<id>
    action = Column(String(64), nullable=False)
    entity = Column(String(64), nullable=True)
    entity_id = Column(String(64), nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Placeholder tablolar (FAZ 3+ icin arayuz olarak simdiden hazir)
# ---------------------------------------------------------------------------


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    id = Column(Integer, primary_key=True)
    index_symbol = Column(String(16), nullable=False)
    regime = Column(String(32), nullable=False)
    breadth_advancers = Column(Integer, nullable=True)
    breadth_decliners = Column(Integer, nullable=True)
    snapshot_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class TechnicalFeature(Base):
    __tablename__ = "technical_features"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    timeframe = Column(String(8), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    features_json = Column(Text, nullable=False)


class FundamentalSnapshot(Base):
    __tablename__ = "fundamental_snapshots"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    status = Column(String(16), default="unavailable", nullable=False)
    payload_json = Column(Text, nullable=True)
    snapshot_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class KapDisclosure(Base):
    __tablename__ = "kap_disclosures"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    status = Column(String(16), default="unavailable", nullable=False)
    title = Column(Text, nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)


class BrokerFlowSnapshot(Base):
    __tablename__ = "broker_flow_snapshots"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    status = Column(String(16), default="unavailable", nullable=False)
    payload_json = Column(Text, nullable=True)
    snapshot_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class StrategyConfigRecord(Base):
    __tablename__ = "strategy_configs"

    id = Column(Integer, primary_key=True)
    version = Column(String(16), nullable=False)
    key = Column(String(64), nullable=False)
    value = Column(String(64), nullable=False)


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    symbol = Column(String(16), nullable=False, index=True)
    alert_type = Column(String(32), nullable=False)
    last_triggered_at = Column(DateTime(timezone=True), nullable=True)
    cooldown_minutes = Column(Integer, default=120, nullable=False)


# ---------------------------------------------------------------------------
# V3: Piyasa rejimi gecmisi, goreceli guc, sektor, tarama, sinyal olaylari,
# performans, kullanici ayarlari, fiyat alarmlari, grafik istekleri,
# piyasa genisligi, veri kalitesi loglari
# ---------------------------------------------------------------------------


class MarketRegimeRecord(Base):
    __tablename__ = "market_regimes"

    id = Column(Integer, primary_key=True)
    index_symbol = Column(String(16), nullable=False, index=True)
    regime = Column(String(32), nullable=False)
    detail = Column(Text, nullable=True)
    daily_change_percent = Column(Float, nullable=True)
    return_20d_percent = Column(Float, nullable=True)
    trading_date = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class RelativeStrengthSnapshot(Base):
    __tablename__ = "relative_strength_snapshots"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    benchmark_symbol = Column(String(16), nullable=False)
    return_5d_stock = Column(Float, nullable=True)
    return_5d_index = Column(Float, nullable=True)
    return_20d_stock = Column(Float, nullable=True)
    return_20d_index = Column(Float, nullable=True)
    return_60d_stock = Column(Float, nullable=True)
    return_60d_index = Column(Float, nullable=True)
    relative_score = Column(Float, nullable=True)
    classification = Column(String(16), nullable=True)
    trading_date = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class SectorMappingRecord(Base):
    """sector_map.yaml icindeki eslestirmenin veritabani gölgesi/gecmisi.

    Ana kaynak (source of truth) hala YAML dosyasidir (app/services/sector_service.py);
    bu tablo Telegram'dan yapilan degisikliklerin denetim/gecmis kaydi icindir.
    """

    __tablename__ = "sector_mappings"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    sector_name = Column(String(128), nullable=False)
    sector_index = Column(String(16), nullable=False)
    set_by_telegram_user_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class Scan(Base):
    __tablename__ = "scans"

    id = Column(Integer, primary_key=True)
    scan_type = Column(String(24), default="evening", nullable=False)  # evening | manual
    started_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    symbols_scanned = Column(Integer, default=0, nullable=False)
    symbols_succeeded = Column(Integer, default=0, nullable=False)
    symbols_failed = Column(Integer, default=0, nullable=False)
    market_regime = Column(String(32), nullable=True)
    status = Column(String(16), default="RUNNING", nullable=False)  # RUNNING | COMPLETED | FAILED
    triggered_by_user_id = Column(Integer, nullable=True)


class ScanResult(Base):
    __tablename__ = "scan_results"

    id = Column(Integer, primary_key=True)
    scan_id = Column(Integer, ForeignKey("scans.id"), nullable=False)
    symbol = Column(String(16), nullable=False, index=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=True)
    score = Column(Float, nullable=True)
    signal_type = Column(String(32), nullable=True)
    data_available = Column(Boolean, default=True, nullable=False)
    error_detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class SignalEvent(Base):
    """Bir sinyalin yasam dongusu boyunca gecirdigi durum degisiklikleri
    (state machine audit log). Gecmise donuk seviyeler DEGISTIRILMEZ; yeni
    bir olay eklenir.
    """

    __tablename__ = "signal_events"

    id = Column(Integer, primary_key=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=False)
    from_state = Column(String(32), nullable=True)
    to_state = Column(String(32), nullable=False)
    price_at_event = Column(Float, nullable=True)
    trading_date = Column(DateTime(timezone=True), nullable=True)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    event_type = Column(String(48), nullable=True)
    planned_price = Column(Numeric(18, 6), nullable=True)
    execution_price = Column(Numeric(18, 6), nullable=True)
    requested_quantity = Column(Numeric(18, 4), nullable=True)
    executed_quantity = Column(Numeric(18, 4), nullable=True)
    provider = Column(String(48), nullable=True)
    source = Column(String(48), nullable=True)
    candle_open_time = Column(DateTime(timezone=True), nullable=True)
    metadata_json = Column(Text, nullable=True)
    unique_dedup_key = Column(String(160), nullable=True, unique=True, index=True)

    signal = relationship("Signal", back_populates="events")


class SignalTarget(Base):
    __tablename__ = "signal_targets"
    __table_args__ = (UniqueConstraint("signal_id", "target_number", name="uq_signal_target_number"),)

    id = Column(Integer, primary_key=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=False, index=True)
    target_number = Column(Integer, nullable=False)
    raw_target_price = Column(Numeric(18, 6), nullable=True)
    target_price = Column(Numeric(18, 6), nullable=False)
    allocation_percent = Column(Numeric(7, 4), nullable=False)
    target_quantity = Column(Numeric(18, 4), nullable=True)
    status = Column(String(24), nullable=False, default="PENDING")
    reached_at = Column(DateTime(timezone=True), nullable=True)
    executed_at = Column(DateTime(timezone=True), nullable=True)
    execution_price = Column(Numeric(18, 6), nullable=True)
    realized_quantity = Column(Numeric(18, 4), nullable=True)
    gross_pnl = Column(Numeric(20, 6), nullable=True)
    costs = Column(Numeric(20, 6), nullable=True)
    net_pnl = Column(Numeric(20, 6), nullable=True)
    notification_sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class SignalEventDelivery(Base):
    __tablename__ = "signal_event_deliveries"
    __table_args__ = (
        UniqueConstraint("signal_event_id", name="uq_signal_event_delivery_event"),
        Index("ix_signal_event_delivery_due", "status", "scheduled_for"),
        Index("ix_signal_event_delivery_recovery", "status", "attempted_at"),
    )

    id = Column(Integer, primary_key=True)
    signal_event_id = Column(Integer, ForeignKey("signal_events.id"), nullable=False)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    telegram_user_id = Column(BigInteger, nullable=False, index=True)
    chat_id = Column(BigInteger, nullable=False)
    status = Column(String(24), nullable=False, default="PENDING", index=True)
    scheduled_for = Column(DateTime(timezone=True), nullable=False, index=True)
    attempted_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    telegram_message_id = Column(BigInteger, nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)
    error_code = Column(String(64), nullable=True)
    # Snapshot at enqueue time: a delayed delivery must not be formatted from
    # a Signal row that has meanwhile advanced to another TP/state.
    payload_text = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class SignalTransitionErrorAudit(Base):
    """Durable audit record for a rejected signal lifecycle transition.

    The runtime rolls its state transaction back first and then commits this
    immutable row in a fresh transaction, so a failed transition cannot partly
    mutate the signal while its rejection silently disappears.
    """

    __tablename__ = "signal_transition_error_audits"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_signal_transition_error_audit_key"),
        Index("ix_signal_transition_error_audit_signal_time", "signal_id", "event_time"),
    )

    id = Column(Integer, primary_key=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    previous_status = Column(String(32), nullable=False)
    attempted_status = Column(String(32), nullable=False)
    event_type = Column(String(48), nullable=False)
    event_time = Column(DateTime(timezone=True), nullable=False)
    dedup_key = Column(String(160), nullable=False)
    reason = Column(Text, nullable=False)
    provider = Column(String(48), nullable=True)
    source = Column(String(48), nullable=True)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class SignalPerformance(Base):
    """Periyodik olarak hesaplanan agrega sinyal basari metrikleri
    (bkz. /performans komutu). Her hesaplama yeni bir satir olarak
    saklanir (gecmis degistirilmez, yeni snapshot eklenir)."""

    __tablename__ = "signal_performance"

    id = Column(Integer, primary_key=True)
    period_days = Column(Integer, nullable=False)
    sample_size = Column(Integer, nullable=False)
    total_signals = Column(Integer, default=0, nullable=False)
    active_signals = Column(Integer, default=0, nullable=False)
    target_1_hit_rate = Column(Float, nullable=True)
    target_2_hit_rate = Column(Float, nullable=True)
    target_3_hit_rate = Column(Float, nullable=True)
    stop_hit_rate = Column(Float, nullable=True)
    average_return_percent = Column(Float, nullable=True)
    average_loss_percent = Column(Float, nullable=True)
    average_r_multiple = Column(Float, nullable=True)
    profit_factor = Column(Float, nullable=True)
    expected_value = Column(Float, nullable=True)
    average_duration_days = Column(Float, nullable=True)
    metrics_json = Column(Text, nullable=True)  # detayli kirilim (rejime/skora/sembole gore)
    is_reliable = Column(Boolean, default=False, nullable=False)
    computed_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class UserSettings(Base):
    """Kullanici bazinda Telegram'dan degistirilebilir ayarlar."""

    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    minimum_signal_score = Column(Float, nullable=True)
    minimum_risk_reward = Column(Float, nullable=True)
    evening_report_enabled = Column(Boolean, default=True, nullable=False)
    evening_report_time = Column(String(8), default="18:20", nullable=False)
    top_candidate_count = Column(Integer, default=5, nullable=False)
    quiet_hours_start = Column(String(8), nullable=True)
    quiet_hours_end = Column(String(8), nullable=True)
    intraday_preview_enabled = Column(Boolean, default=True, nullable=False)
    chart_type = Column(String(16), default="candle", nullable=False)  # candle | line
    maximum_open_positions = Column(Integer, nullable=True)
    maximum_sector_exposure_percent = Column(Float, nullable=True)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class PriceAlert(Base):
    __tablename__ = "price_alerts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    symbol = Column(String(16), nullable=False, index=True)
    alert_type = Column(String(24), nullable=False)  # ust|alt|hacim|skor_ustunde|skor_altinda|sinyal|rejim
    threshold_value = Column(Float, nullable=True)
    threshold_text = Column(String(32), nullable=True)  # orn. sinyal turu metni
    cooldown_minutes = Column(Integer, default=120, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    last_triggered_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id = Column(Integer, primary_key=True)
    price_alert_id = Column(Integer, ForeignKey("price_alerts.id"), nullable=False)
    triggered_value = Column(Float, nullable=True)
    message = Column(Text, nullable=True)
    triggered_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# Kalıcı kullanıcı fiyat alarmları (0008). Eski price_alerts korunur; yeni
# alan normalleştirilmiş yaşam döngüsü, outbox ve toplu/OCR içe aktarma sunar.
# ---------------------------------------------------------------------------


class UserPriceAlert(Base):
    __tablename__ = "user_price_alerts"
    __table_args__ = (
        Index("ix_user_price_alert_status_symbol", "status", "normalized_symbol"),
        Index("ix_user_price_alert_user_status", "user_id", "status"),
        UniqueConstraint("user_id", "public_id", name="uq_user_price_alert_public_id"),
    )

    id = Column(Integer, primary_key=True)
    public_id = Column(String(16), nullable=False, unique=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    telegram_user_id = Column(BigInteger, nullable=False, index=True)
    chat_id = Column(BigInteger, nullable=False)
    symbol = Column(String(16), nullable=False)
    normalized_symbol = Column(String(16), nullable=False, index=True)
    exchange = Column(String(16), nullable=False, default="BIST")
    condition_type = Column(String(32), nullable=False)
    target_price = Column(Numeric(18, 6), nullable=False)
    base_price = Column(Numeric(18, 6), nullable=True)
    percentage_value = Column(Numeric(12, 6), nullable=True)
    near_tolerance = Column(Numeric(18, 6), nullable=True)
    status = Column(String(24), nullable=False, default="ACTIVE", index=True)
    mode = Column(String(24), nullable=False, default="PERSISTENT")
    repeat_interval_seconds = Column(Integer, nullable=False, default=60)
    sound_mode = Column(String(24), nullable=False, default="FIRST_TRIGGER")
    sound_name = Column(String(16), nullable=False, default="zil")
    market_hours_only = Column(Boolean, nullable=False, default=True)
    note = Column(Text, nullable=True)
    rearm_enabled = Column(Boolean, nullable=False, default=True)
    reset_band_value = Column(Numeric(18, 6), nullable=True)
    last_observed_price = Column(Numeric(18, 6), nullable=True)
    previous_valid_price = Column(Numeric(18, 6), nullable=True)
    previous_price_timestamp = Column(DateTime(timezone=True), nullable=True)
    last_price_timestamp = Column(DateTime(timezone=True), nullable=True)
    last_provider = Column(String(48), nullable=True)
    last_freshness_seconds = Column(Integer, nullable=True)
    last_evaluated_at = Column(DateTime(timezone=True), nullable=True)
    last_triggered_at = Column(DateTime(timezone=True), nullable=True)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    snoozed_until = Column(DateTime(timezone=True), nullable=True, index=True)
    next_delivery_at = Column(DateTime(timezone=True), nullable=True, index=True)
    trigger_count = Column(Integer, nullable=False, default=0)
    source_type = Column(String(16), nullable=False, default="TEXT")
    import_job_id = Column(Integer, nullable=True, index=True)
    row_version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class VirtualPortfolio(Base):
    """Bir kullanıcının bağımsız SMXM sanal hesabı.

    Eski ``paper_accounts`` tablosu geriye uyumluluk için korunur. Bu model,
    istenen üç ayrı portföy ve iki strateji senaryosunu birbirinden ayırır.
    """

    __tablename__ = "virtual_portfolios"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_virtual_portfolio_user_name"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(96), nullable=False)
    starting_balance = Column(Float, nullable=False)
    current_balance = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class VirtualTrade(Base):
    __tablename__ = "virtual_trades"

    id = Column(Integer, primary_key=True)
    portfolio_id = Column(Integer, ForeignKey("virtual_portfolios.id"), nullable=False, index=True)
    instrument = Column(String(24), nullable=False, index=True)
    direction = Column(String(8), nullable=False)  # long | short
    entry_price = Column(Float, nullable=False)
    sl = Column(Float, nullable=False)
    tp = Column(Float, nullable=False)
    size = Column(Float, nullable=False)
    risk_percent = Column(Float, nullable=False)
    opened_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    exit_price = Column(Float, nullable=True)
    pnl = Column(Float, nullable=True)
    status = Column(String(16), default="open", nullable=False, index=True)
    setup_checklist_score = Column(Integer, nullable=False)
    strategy_name = Column(String(64), default="smxm", nullable=False)
    planned_rr = Column(Float, nullable=False)
    notes = Column(Text, nullable=True)


class MarketDailyReportLog(Base):
    """Sabah biasını ve akşam gerçekleşmesini aynı işlem günü için saklar."""

    __tablename__ = "market_daily_report_logs"
    __table_args__ = (
        UniqueConstraint(
            "report_date", "report_type", "symbol", name="uq_market_report_date_type_symbol"
        ),
    )

    id = Column(Integer, primary_key=True)
    report_date = Column(DateTime(timezone=True), nullable=False, index=True)
    report_type = Column(String(16), nullable=False, index=True)  # morning | evening
    symbol = Column(String(24), nullable=False, index=True)
    predicted_direction = Column(String(16), nullable=True)
    actual_direction = Column(String(16), nullable=True)
    confidence_score = Column(Float, nullable=True)
    checklist_passed = Column(Integer, nullable=True)
    checklist_total = Column(Integer, nullable=True)
    open_price = Column(Float, nullable=True)
    high_price = Column(Float, nullable=True)
    low_price = Column(Float, nullable=True)
    close_price = Column(Float, nullable=True)
    daily_change_percent = Column(Float, nullable=True)
    consistent = Column(Boolean, nullable=True)
    news_json = Column(Text, nullable=True)
    report_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class PriceAlertTrigger(Base):
    __tablename__ = "price_alert_triggers"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_price_alert_trigger_key"),)

    id = Column(Integer, primary_key=True)
    alert_id = Column(Integer, ForeignKey("user_price_alerts.id"), nullable=False, index=True)
    trigger_sequence = Column(Integer, nullable=False)
    triggered_price = Column(Numeric(18, 6), nullable=False)
    target_price_snapshot = Column(Numeric(18, 6), nullable=False)
    condition_type_snapshot = Column(String(32), nullable=False)
    detected_at = Column(DateTime(timezone=True), nullable=False)
    data_timestamp = Column(DateTime(timezone=True), nullable=False)
    provider = Column(String(48), nullable=False)
    freshness_seconds = Column(Integer, nullable=False)
    status = Column(String(24), nullable=False, default="OPEN")
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    idempotency_key = Column(String(180), nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class PriceAlertDelivery(Base):
    __tablename__ = "price_alert_deliveries"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_price_alert_delivery_key"),
        Index("ix_price_alert_delivery_due", "status", "scheduled_for"),
    )

    id = Column(Integer, primary_key=True)
    trigger_id = Column(Integer, ForeignKey("price_alert_triggers.id"), nullable=False, index=True)
    alert_id = Column(Integer, ForeignKey("user_price_alerts.id"), nullable=False, index=True)
    telegram_user_id = Column(BigInteger, nullable=False, index=True)
    chat_id = Column(BigInteger, nullable=False)
    delivery_type = Column(String(24), nullable=False, default="TEXT")
    scheduled_for = Column(DateTime(timezone=True), nullable=False, index=True)
    attempted_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    telegram_message_id = Column(BigInteger, nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    status = Column(String(24), nullable=False, default="PENDING", index=True)
    error_code = Column(String(64), nullable=True)
    error_message_sanitized = Column(Text, nullable=True)
    next_retry_at = Column(DateTime(timezone=True), nullable=True, index=True)
    idempotency_key = Column(String(180), nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class AlarmImportJob(Base):
    __tablename__ = "alarm_import_jobs"
    id = Column(Integer, primary_key=True)
    public_id = Column(String(18), nullable=False, unique=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    telegram_user_id = Column(BigInteger, nullable=False, index=True)
    chat_id = Column(BigInteger, nullable=False)
    source_type = Column(String(16), nullable=False)
    status = Column(String(24), nullable=False, default="PREVIEW")
    total_rows = Column(Integer, nullable=False, default=0)
    valid_rows = Column(Integer, nullable=False, default=0)
    invalid_rows = Column(Integer, nullable=False, default=0)
    duplicate_rows = Column(Integer, nullable=False, default=0)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class AlarmImportRow(Base):
    __tablename__ = "alarm_import_rows"
    __table_args__ = (UniqueConstraint("import_job_id", "row_number", name="uq_alarm_import_row_number"),)
    id = Column(Integer, primary_key=True)
    import_job_id = Column(Integer, ForeignKey("alarm_import_jobs.id"), nullable=False, index=True)
    row_number = Column(Integer, nullable=False)
    raw_text = Column(Text, nullable=True)
    parsed_symbol = Column(String(16), nullable=True)
    parsed_price = Column(Numeric(18, 6), nullable=True)
    parsed_condition = Column(String(32), nullable=True)
    base_price = Column(Numeric(18, 6), nullable=True)
    percentage_value = Column(Numeric(12, 6), nullable=True)
    near_tolerance = Column(Numeric(18, 6), nullable=True)
    sound_name = Column(String(16), nullable=True)
    parsed_mode = Column(String(24), nullable=True)
    repeat_interval_seconds = Column(Integer, nullable=True)
    note = Column(Text, nullable=True)
    confidence = Column(Float, nullable=True)
    status = Column(String(24), nullable=False)
    validation_error = Column(Text, nullable=True)
    user_corrected = Column(Boolean, nullable=False, default=False)
    created_alert_id = Column(Integer, ForeignKey("user_price_alerts.id"), nullable=True)


class UserAlarmSetting(Base):
    __tablename__ = "user_alarm_settings"
    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    default_repeat_interval_seconds = Column(Integer, nullable=False, default=60)
    default_alarm_mode = Column(String(24), nullable=False, default="PERSISTENT")
    default_sound_mode = Column(String(24), nullable=False, default="FIRST_TRIGGER")
    default_sound_name = Column(String(16), nullable=False, default="zil")
    group_simultaneous_alerts = Column(Boolean, nullable=False, default=True)
    market_hours_only = Column(Boolean, nullable=False, default=True)
    quiet_hours_enabled = Column(Boolean, nullable=False, default=False)
    quiet_hours_start = Column(String(8), nullable=True)
    quiet_hours_end = Column(String(8), nullable=True)
    timezone = Column(String(48), nullable=False, default="Europe/Istanbul")
    max_active_alerts = Column(Integer, nullable=False, default=500)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ChartRequest(Base):
    """Uretilen ve Telegram'a gonderilen grafik dosyalarinin denetim kaydi.
    Dosyanin kendisi gonderim sonrasi diskten SILINIR; burada sadece meta veri kalir.
    """

    __tablename__ = "chart_requests"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    symbol = Column(String(16), nullable=False, index=True)
    chart_type = Column(String(16), default="price", nullable=False)  # price | relative_strength
    period = Column(String(8), default="6ay", nullable=False)
    file_deleted = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class MarketBreadthRecord(Base):
    __tablename__ = "market_breadth"

    id = Column(Integer, primary_key=True)
    universe_size = Column(Integer, nullable=False)
    advancers = Column(Integer, nullable=False)
    decliners = Column(Integer, nullable=False)
    unchanged = Column(Integer, nullable=False)
    above_ema20_ratio = Column(Float, nullable=True)
    above_ema50_ratio = Column(Float, nullable=True)
    new_20d_highs = Column(Integer, nullable=True)
    new_20d_lows = Column(Integer, nullable=True)
    rising_volume_ratio = Column(Float, nullable=True)
    trading_date = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class DataQualityLog(Base):
    __tablename__ = "data_quality_logs"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    timeframe = Column(String(8), nullable=False)
    is_valid = Column(Boolean, nullable=False)
    issues_json = Column(Text, nullable=True)
    warnings_json = Column(Text, nullable=True)
    provider = Column(String(32), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# V3.2 (Asama 3): Anormal hareket motoru
# ---------------------------------------------------------------------------


class Anomaly(Base):
    """Tespit edilen anormal hareketlerin (hacim patlamasi, gap, destek/direnc
    kirilimi, volatilite patlamasi) kalici kaydi. Hicbir anomali UYDURULMAZ;
    yalnizca app.analysis.anomaly_engine tarafindan gercek OHLCV verisinden
    hesaplanan olaylar buraya yazilir."""

    __tablename__ = "anomalies"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    timeframe = Column(String(8), nullable=False, default="1d")
    anomaly_type = Column(String(32), nullable=False, index=True)
    severity = Column(String(16), nullable=False)
    description = Column(Text, nullable=False)
    value = Column(Float, nullable=True)
    price_at_detection = Column(Float, nullable=True)
    detected_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class AnomalyNotification(Base):
    """Bir kullaniciya HANGI anomalinin gonderildigini izler; ayni anomali
    icin kullaniciya tekrar tekrar bildirim gonderilmesini engeller."""

    __tablename__ = "anomaly_notifications"

    id = Column(Integer, primary_key=True)
    anomaly_id = Column(Integer, ForeignKey("anomalies.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    sent_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("anomaly_id", "user_id", name="uq_anomaly_notification_user"),)


# ---------------------------------------------------------------------------
# V3.2 (Asama 4): GDELT haber radari, haber etkisi, Groq (opsiyonel AI
# aciklama) ve saglayici saglik loglari.
#
# ONEMLI: Bu tablolar sadece haber/AI GORUNURLUGU icin ek, opsiyonel bir
# katmandir. Hicbir satir uydurulmaz; GDELT/Groq calismazsa veya haber
# bulunamazsa bu tablolara kayit ATILMAZ (bos sonuc UYDURULMAZ). Mevcut
# sinyal/analiz akisi bu tablolar olmadan da COKMEDEN calismaya devam eder.
# ---------------------------------------------------------------------------


class NewsArticle(Base):
    """GDELT'ten gelen HAM haber kaydi (tekillestirilmis).

    Ayni haberin farkli kaynaklardaki kopyalari `dedup_key` uzerinden
    birlestirilir; ayni dedup_key icin yalnizca TEK bir satir tutulur."""

    __tablename__ = "news_articles"
    __table_args__ = (UniqueConstraint("dedup_key", name="uq_news_article_dedup_key"),)

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    title = Column(Text, nullable=False)
    source = Column(String(255), nullable=True)
    url = Column(Text, nullable=False)
    published_at = Column(DateTime(timezone=True), nullable=True)
    language = Column(String(8), nullable=True)
    company_match_confidence = Column(Float, nullable=False, default=0.0)
    matched_alias = Column(String(255), nullable=True)
    dedup_key = Column(String(64), nullable=False, index=True)
    duplicate_source_count = Column(Integer, nullable=False, default=1)
    provider = Column(String(32), nullable=False, default="gdelt")
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    events = relationship("NewsEvent", back_populates="article", cascade="all, delete-orphan")


class NewsEvent(Base):
    """Bir haber icin NewsImpactEngine tarafindan hesaplanan, kural tabanli
    ve aciklanabilir etki degerlendirmesi (bolum 2, Asama 4)."""

    __tablename__ = "news_events"

    id = Column(Integer, primary_key=True)
    article_id = Column(Integer, ForeignKey("news_articles.id"), nullable=False)
    symbol = Column(String(16), nullable=False, index=True)
    category = Column(String(32), nullable=False)
    impact_score = Column(Float, nullable=False)  # -100..+100
    confidence_score = Column(Float, nullable=False)  # 0..100
    source_confidence = Column(Float, nullable=False, default=50.0)
    company_match_confidence = Column(Float, nullable=False, default=0.0)
    news_age_hours = Column(Float, nullable=True)
    rationale = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    article = relationship("NewsArticle", back_populates="events")


class NewsImpactSnapshot(Base):
    """Bir sembol icin belirli bir pencerede (orn. 24s/7g) toplu haber etkisi
    ozeti; /analiz ve /analiz_detay mesajlarinda kullanilir."""

    __tablename__ = "news_impact_snapshots"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    window_label = Column(String(16), nullable=False)  # "24h" | "7d"
    article_count = Column(Integer, nullable=False, default=0)
    impact_score = Column(Float, nullable=True)  # -100..+100, None = haber yok
    confidence_score = Column(Float, nullable=True)  # 0..100
    top_events_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class GroqExplanation(Base):
    """Groq'tan alinan (opsiyonel) sade Turkce aciklamalarin onbellegi.

    Ayni analiz icin (ayni cache_key ile) tekrar Groq'a istek ATILMAZ;
    onbellekteki cevap dondurulur."""

    __tablename__ = "groq_explanations"
    __table_args__ = (UniqueConstraint("cache_key", name="uq_groq_explanation_cache_key"),)

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    kind = Column(String(32), nullable=False)  # teknik | coklu_zaman | haber | risk
    cache_key = Column(String(128), nullable=False, index=True)
    model = Column(String(64), nullable=True)
    response_text = Column(Text, nullable=False)
    is_fallback = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProviderHealthLog(Base):
    """GDELT/Groq gibi opsiyonel dis saglayicilarin saglik/hata durumunu izler
    (bolum 5-6, Asama 4); saglayici anahtarlari/gizli degerleri ASLA burada
    tutulmaz, yalnizca durum/hata mesaji tutulur (loglama maskeleme filtresi
    ile birlikte calisir)."""

    __tablename__ = "provider_health_logs"

    id = Column(Integer, primary_key=True)
    provider = Column(String(32), nullable=False, index=True)  # gdelt | groq
    status = Column(String(16), nullable=False)  # ok | degraded | error | disabled
    detail = Column(Text, nullable=True)
    checked_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


# ---------------------------------------------------------------------------
# Engine / Session
# ---------------------------------------------------------------------------


def build_engine(database_url: str | None = None):
    url = database_url or get_settings().database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()
    return engine


_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


# ---------------------------------------------------------------------------
# MERGEN QUANT - Asama 5: cok zamanli seviyeler, cakisan bolgeler,
# senaryolar, kirilim senaryolari, donemsel goreceli guc, gelismis alarmlar.
# Bunlar TAMAMEN EK (additive) tablolardir; mevcut hicbir tabloya veya
# eski kullanici/portfoy/sinyal/haber/anomali verisine dokunmaz.
# ---------------------------------------------------------------------------


class TimeframeLevel(Base):
    """Gunluk/haftalik/aylik destek-direnc seviyelerinin son hesaplanan
    anlik goruntusu (snapshot). Her /analiz veya /seviyeler cagrisinda
    yeniden hesaplanip upsert edilir; gecmis kayitlar alarm/backtest
    icin tutulabilir."""

    __tablename__ = "timeframe_levels"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    timeframe = Column(String(16), nullable=False)  # gunluk | haftalik | aylik
    level_type = Column(String(24), nullable=False)  # destek_1|destek_2|ana_destek|direnc_1|direnc_2|ana_direnc
    low = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    mid = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    touches = Column(Integer, default=0, nullable=False)
    rejections = Column(Integer, default=0, nullable=False)
    last_test_date = Column(String(16), nullable=True)
    sources_json = Column(Text, nullable=True)
    volume_confirmed = Column(Boolean, default=False, nullable=False)
    computed_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class ConfluenceZoneRecord(Base):
    """Birden fazla zaman diliminin cakistigi guclu destek/direnc bolgeleri."""

    __tablename__ = "confluence_zones"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    kind = Column(String(8), nullable=False)  # destek | direnc
    low = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    mid = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    timeframes_json = Column(Text, nullable=True)
    sources_json = Column(Text, nullable=True)
    total_touches = Column(Integer, default=0, nullable=False)
    volume_confirmed = Column(Boolean, default=False, nullable=False)
    computed_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class PriceScenario(Base):
    """Dusus/yukselis senaryo bolgeleri (kesin fiyat tahmini DEGIL,
    teknik olarak izlenen senaryo bolgeleri)."""

    __tablename__ = "price_scenarios"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    direction = Column(String(16), nullable=False)  # dusus | yukselis
    scenario_type = Column(String(24), nullable=False)  # yakin|ana|guclu_kirilim|asiri
    low = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    activation_condition = Column(Text, nullable=True)
    computed_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class BreakoutScenario(Base):
    """'Bu seviye kirilirsa ne olur?' motorunun ciktisi."""

    __tablename__ = "breakout_scenarios"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    level_type = Column(String(8), nullable=False)  # direnc | destek
    level_price = Column(Float, nullable=False)
    confirmation_close_level = Column(Float, nullable=True)
    min_volume_note = Column(Text, nullable=True)
    target_1 = Column(Float, nullable=True)
    target_2 = Column(Float, nullable=True)
    failure_level = Column(Float, nullable=True)
    false_breakout_risk_note = Column(Text, nullable=True)
    computed_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class RelativeStrengthPeriod(Base):
    """XU100 ve sektore gore donemsel (1 hafta/1 ay/3 ay/6 ay) goreceli guc."""

    __tablename__ = "relative_strength_periods"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    benchmark = Column(String(16), nullable=False)  # xu100 | sektor
    period = Column(String(8), nullable=False)  # 1hafta|1ay|3ay|6ay
    stock_return_pct = Column(Float, nullable=True)
    benchmark_return_pct = Column(Float, nullable=True)
    diff_pct = Column(Float, nullable=True)
    classification = Column(String(24), nullable=True)
    strength_score = Column(Float, nullable=True)
    computed_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class EnhancedAlertEvent(Base):
    """Asama 5 gelismis alarm turlerinin tetiklenme kaydi (cooldown/idempotency
    icin price_alerts/alert_events ile birlikte, onlarin YERINE degil ek olarak)."""

    __tablename__ = "enhanced_alert_events"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    symbol = Column(String(16), nullable=False, index=True)
    alert_type = Column(String(48), nullable=False)
    current_price = Column(Float, nullable=True)
    triggered_level = Column(Float, nullable=True)
    level_timeframe = Column(String(16), nullable=True)
    level_confidence = Column(Float, nullable=True)
    volume_ratio = Column(Float, nullable=True)
    market_regime = Column(String(32), nullable=True)
    xu100_strength = Column(String(24), nullable=True)
    sector_strength = Column(String(24), nullable=True)
    related_scenario = Column(String(48), nullable=True)
    main_risk = Column(Text, nullable=True)
    candle_key = Column(String(32), nullable=True)  # ayni alarmin ayni mumda tekrarini onlemek icin
    data_time = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# MERGEN QUANT - Asama 5d: veri güvenilirliği, kalıcı alarm kural/dedup
# durumu ve grafik cache meta verisi. Tümü additive tablolardır.
# ---------------------------------------------------------------------------


class DataQualitySnapshot(Base):
    __tablename__ = "data_quality_snapshots"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    timeframe = Column(String(8), nullable=False, index=True)
    status = Column(String(16), nullable=False)
    quality_score = Column(Integer, nullable=False, default=0)
    data_age_minutes = Column(Float, nullable=True)
    last_bar_time = Column(DateTime(timezone=True), nullable=True)
    missing_bar_count = Column(Integer, nullable=False, default=0)
    duplicate_bar_count = Column(Integer, nullable=False, default=0)
    outlier_count = Column(Integer, nullable=False, default=0)
    incomplete_bar_count = Column(Integer, nullable=False, default=0)
    provider = Column(String(48), nullable=True)
    fallback_used = Column(Boolean, nullable=False, default=False)
    cache_used = Column(Boolean, nullable=False, default=False)
    cache_age_minutes = Column(Float, nullable=True)
    warnings_json = Column(Text, nullable=True)
    issues_json = Column(Text, nullable=True)
    usable_for_analysis = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class ProviderHealthEvent(Base):
    __tablename__ = "provider_health_events"

    id = Column(Integer, primary_key=True)
    provider = Column(String(48), nullable=False, index=True)
    status = Column(String(16), nullable=False)
    event_type = Column(String(32), nullable=False, default="health_check")
    circuit_state = Column(String(16), nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    detail = Column(Text, nullable=True)
    occurred_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class ProviderCircuitBreakerRecord(Base):
    __tablename__ = "provider_circuit_breakers"
    __table_args__ = (UniqueConstraint("provider", name="uq_provider_circuit_breaker"),)

    id = Column(Integer, primary_key=True)
    provider = Column(String(48), nullable=False, index=True)
    state = Column(String(16), nullable=False, default="CLOSED")
    consecutive_failures = Column(Integer, nullable=False, default=0)
    opened_at = Column(DateTime(timezone=True), nullable=True)
    last_failure = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class EnhancedAlarmRule(Base):
    __tablename__ = "enhanced_alarm_rules"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    alert_type = Column(String(64), nullable=False, index=True)
    timeframe = Column(String(16), nullable=True)
    threshold_value = Column(Float, nullable=True)
    threshold_text = Column(String(64), nullable=True)
    target_index = Column(Integer, nullable=True)
    cooldown_minutes = Column(Integer, nullable=False, default=120)
    is_active = Column(Boolean, nullable=False, default=True)
    last_state_key = Column(String(128), nullable=True)
    last_evaluated_candle_key = Column(String(64), nullable=True)
    last_triggered_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class EnhancedAlarmTriggerEvent(Base):
    __tablename__ = "enhanced_alarm_trigger_events"
    __table_args__ = (
        UniqueConstraint("rule_id", "candle_key", "state_key", name="uq_enhanced_alarm_rule_candle_state"),
    )

    id = Column(Integer, primary_key=True)
    rule_id = Column(Integer, ForeignKey("enhanced_alarm_rules.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    alert_type = Column(String(64), nullable=False)
    candle_key = Column(String(64), nullable=False)
    state_key = Column(String(128), nullable=False)
    current_price = Column(Float, nullable=True)
    triggered_level_low = Column(Float, nullable=True)
    triggered_level_high = Column(Float, nullable=True)
    timeframe = Column(String(16), nullable=True)
    close_confirmed = Column(Boolean, nullable=False, default=False)
    volume_ratio = Column(Float, nullable=True)
    level_confidence = Column(Float, nullable=True)
    provider = Column(String(48), nullable=True)
    fallback_used = Column(Boolean, nullable=False, default=False)
    data_quality_score = Column(Integer, nullable=True)
    message = Column(Text, nullable=True)
    triggered_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class ChartCacheMetadata(Base):
    __tablename__ = "chart_cache_metadata"
    __table_args__ = (UniqueConstraint("cache_key", name="uq_chart_cache_key"),)

    id = Column(Integer, primary_key=True)
    cache_key = Column(String(128), nullable=False, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    timeframe = Column(String(16), nullable=False)
    chart_type = Column(String(32), nullable=False)
    data_timestamp = Column(DateTime(timezone=True), nullable=True)
    file_path = Column(Text, nullable=True)
    file_size = Column(Integer, nullable=True)
    hit_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# MERGEN QUANT Aşama 5e: uzun hedef, değerleme ve sermaye işlemleri.
# Yalnızca additive tablolar; önceki şema ve kayıtlar değiştirilmez.
# ---------------------------------------------------------------------------


class LongTermScenario(Base):
    __tablename__ = "long_term_scenarios"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    direction = Column(String(16), nullable=False)
    scenario_class = Column(String(64), nullable=False)
    price_low = Column(Float, nullable=False)
    price_high = Column(Float, nullable=False)
    price_mid = Column(Float, nullable=False)
    required_change_percent = Column(Float, nullable=True)
    required_price_multiple = Column(Float, nullable=True)
    confidence = Column(Float, nullable=True)
    time_horizon = Column(String(64), nullable=True)
    activation_json = Column(Text, nullable=True)
    invalidation_json = Column(Text, nullable=True)
    evidence_json = Column(Text, nullable=True)
    fundamental_support = Column(String(64), nullable=True)
    speculation_risk = Column(String(128), nullable=True)
    data_timestamp = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class UserPriceTarget(Base):
    __tablename__ = "user_price_targets"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    target_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=False)
    required_change_percent = Column(Float, nullable=True)
    required_price_multiple = Column(Float, nullable=True)
    technical_class = Column(String(48), nullable=True)
    fundamental_class = Column(String(48), nullable=True)
    risk_class = Column(String(48), nullable=True)
    realism_score = Column(Float, nullable=True)
    data_timestamp = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class TargetRoadmapStepRecord(Base):
    __tablename__ = "target_roadmap_steps"
    __table_args__ = (
        UniqueConstraint("target_record_id", "sequence", name="uq_target_roadmap_record_sequence"),
    )

    id = Column(Integer, primary_key=True)
    target_record_id = Column(Integer, ForeignKey("target_tracking_records.id"), nullable=True, index=True)
    user_target_id = Column(Integer, ForeignKey("user_price_targets.id"), nullable=True, index=True)
    symbol = Column(String(16), nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    price_low = Column(Float, nullable=False)
    price_high = Column(Float, nullable=False)
    price_mid = Column(Float, nullable=False)
    level_type = Column(String(48), nullable=True)
    confidence = Column(Float, nullable=True)
    breakout_condition = Column(Text, nullable=True)
    volume_condition = Column(Text, nullable=True)
    next_target = Column(Float, nullable=True)
    correction_zone_json = Column(Text, nullable=True)
    invalidation_level = Column(Float, nullable=True)
    estimated_duration = Column(String(64), nullable=True)
    status = Column(String(32), nullable=False, default="Bekleniyor")
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ValuationSnapshot(Base):
    __tablename__ = "valuation_snapshots"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    valuation_type = Column(String(32), nullable=False)
    classification = Column(String(48), nullable=False)
    market_cap = Column(Float, nullable=True)
    shares_outstanding = Column(Float, nullable=True)
    net_asset_value = Column(Float, nullable=True)
    nav_per_share = Column(Float, nullable=True)
    market_cap_to_nav = Column(Float, nullable=True)
    discount_premium_percent = Column(Float, nullable=True)
    financial_period_date = Column(DateTime(timezone=True), nullable=True)
    data_is_stale = Column(Boolean, nullable=False, default=False)
    payload_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class CorporateActionRecord(Base):
    __tablename__ = "corporate_action_events"
    __table_args__ = (
        UniqueConstraint("symbol", "corporate_action_type", "effective_date", name="uq_corporate_action_symbol_type_date"),
    )

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    corporate_action_type = Column(String(64), nullable=False)
    effective_date = Column(DateTime(timezone=True), nullable=True, index=True)
    raw_price = Column(Float, nullable=True)
    adjusted_price = Column(Float, nullable=True)
    adjustment_factor = Column(Float, nullable=True)
    cash_amount = Column(Float, nullable=True)
    share_ratio = Column(Float, nullable=True)
    old_share_count = Column(Float, nullable=True)
    new_share_count = Column(Float, nullable=True)
    source = Column(String(48), nullable=True)
    payload_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class MarketSessionEvent(Base):
    """Provider-originated BIST session interruption/resumption audit row.

    These rows are deliberately separate from ``SignalEvent``: one exchange
    session event may affect many signals, while a signal event remains the
    immutable per-signal lifecycle audit.  ``unique_dedup_key`` lets a future
    licensed/reference-data ingestor persist the same provider event exactly
    once after a worker restart.
    """

    __tablename__ = "market_session_events"
    __table_args__ = (
        UniqueConstraint("unique_dedup_key", name="uq_market_session_event_dedup_key"),
        Index("ix_market_session_event_symbol_started", "symbol", "started_at"),
        Index("ix_market_session_event_type_started", "event_type", "started_at"),
    )

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    event_type = Column(String(48), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    source = Column(String(48), nullable=False)
    metadata_json = Column(Text, nullable=True)
    unique_dedup_key = Column(String(160), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class TargetRealismSnapshot(Base):
    __tablename__ = "target_realism_snapshots"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    current_price = Column(Float, nullable=False)
    target_price = Column(Float, nullable=False)
    current_market_cap = Column(Float, nullable=True)
    target_market_cap = Column(Float, nullable=True)
    technical_class = Column(String(48), nullable=True)
    fundamental_class = Column(String(48), nullable=True)
    liquidity_risk = Column(String(48), nullable=True)
    valuation_risk = Column(String(48), nullable=True)
    speculation_risk = Column(String(48), nullable=True)
    manipulation_indicator = Column(Text, nullable=True)
    realism_score = Column(Float, nullable=True)
    payload_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class TargetTrackingRecord(Base):
    __tablename__ = "target_tracking_records"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "target_type", "target_low", "target_high", "data_timestamp",
            name="uq_target_tracking_symbol_zone_timestamp",
        ),
    )

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=False, index=True)
    created_price = Column(Float, nullable=False)
    target_low = Column(Float, nullable=False)
    target_high = Column(Float, nullable=False)
    target_type = Column(String(64), nullable=False)
    time_horizon = Column(String(64), nullable=True, index=True)
    confidence = Column(Float, nullable=True)
    technical_reasons_json = Column(Text, nullable=True)
    fundamental_status = Column(String(64), nullable=True)
    invalidation_level = Column(Float, nullable=True)
    status = Column(String(32), nullable=False, default="Aktif", index=True)
    data_timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    market_regime = Column(String(48), nullable=True, index=True)
    nearest_price = Column(Float, nullable=True)
    lowest_price_after_creation = Column(Float, nullable=True)
    highest_price_after_creation = Column(Float, nullable=True)
    max_drawdown_percent = Column(Float, nullable=True)
    max_upside_percent = Column(Float, nullable=True)
    reached_at = Column(DateTime(timezone=True), nullable=True)
    invalidated_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class TargetPerformanceSummary(Base):
    __tablename__ = "target_performance_summaries"
    __table_args__ = (
        UniqueConstraint("symbol", "time_horizon", "market_regime", name="uq_target_performance_scope"),
    )

    id = Column(Integer, primary_key=True)
    symbol = Column(String(16), nullable=True, index=True)
    time_horizon = Column(String(64), nullable=True)
    market_regime = Column(String(48), nullable=True)
    total_targets = Column(Integer, nullable=False, default=0)
    reached_targets = Column(Integer, nullable=False, default=0)
    partially_reached_targets = Column(Integer, nullable=False, default=0)
    invalidated_targets = Column(Integer, nullable=False, default=0)
    expired_targets = Column(Integer, nullable=False, default=0)
    success_rate = Column(Float, nullable=True)
    average_days_to_target = Column(Float, nullable=True)
    average_max_drawdown_percent = Column(Float, nullable=True)
    average_max_upside_percent = Column(Float, nullable=True)
    invalidation_rate = Column(Float, nullable=True)
    calculated_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)
    return _SessionLocal


def init_db() -> None:
    Base.metadata.create_all(bind=get_engine())


def get_db_session():
    session_factory = get_session_factory()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()
