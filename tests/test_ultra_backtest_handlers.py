from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pandas as pd
from sqlalchemy.orm import sessionmaker
from telegram.ext import Application

from app.backtest.engine_v5g import BacktestConfig, SignalInstruction, TransactionCostConfig
from app.backtest.universe import UniverseBacktestEngine, UniverseBacktestRequest
from app.backtest.signal_replay import SignalReplayPlan, replay_signal_plan
from app.models.database import (
    BacktestRun,
    BacktestTrade,
    Base,
    Signal,
    SignalEvent,
    SignalStateEnum,
    SignalTypeEnum,
    User,
    WatchlistItem,
    build_engine,
)
from app.signals import EntryOrderType, ExecutionPolicy, FillModel, TransactionCostModel
from app.telegram import handlers_stage5g
from app.telegram import ultra_backtest_handlers as module


class FakeMessage:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    async def reply_text(self, text, reply_markup=None, **_kwargs):
        self.calls.append((text, reply_markup))


def fake_update(telegram_id: int = 101):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=telegram_id),
        effective_chat=SimpleNamespace(id=telegram_id + 1000),
        message=FakeMessage(),
    )


async def allow(_update):
    return False


def factory_for(tmp_path: Path):
    engine = build_engine(f"sqlite:///{(tmp_path / 'ultra-backtest.db').as_posix()}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def add_run(db, user_id: int, *, run_id: str, status: str = "COMPLETED") -> BacktestRun:
    now = datetime.now(timezone.utc)
    run = BacktestRun(
        run_id=run_id,
        user_id=user_id,
        symbol="THYAO",
        timeframe="1d",
        start_date=now - timedelta(days=30),
        end_date=now,
        initial_capital=100_000,
        commission_percent=0,
        slippage_percent=0,
        strategy_version="test",
        status=status,
        run_status=status,
        progress_percent=100,
    )
    db.add(run)
    db.flush()
    return run


def add_trade(db, run: BacktestRun, *, pnl: float, gross: float, cost: float):
    now = datetime.now(timezone.utc)
    db.add(BacktestTrade(
        backtest_run_id=run.id,
        symbol="THYAO",
        side="BUY",
        entry_time=now - timedelta(days=2),
        exit_time=now - timedelta(days=1),
        entry_price=100,
        exit_price=101,
        quantity=1,
        pnl=pnl,
        gross_pnl=gross,
        total_cost=cost,
        exit_reason="TARGET" if pnl > 0 else "STOP",
    ))


def test_timeframe_and_period_parser_supports_documented_turkish_aliases():
    assert module._parse_timeframe_period(["1g", "3y"])[0] == "1d"
    assert module._parse_timeframe_period(["1s", "730g"])[0] == "1h"
    assert module._parse_timeframe_period(["15d", "60g"])[0] == "15m"
    with pytest.raises(module.UniverseCommandError, match="10 yıl"):
        module._parse_timeframe_period(["1g", "11y"])


def test_single_symbol_backtest_parser_accepts_period_and_legacy_dates():
    timeframe, start, end = handlers_stage5g._parse_backtest_window(
        ["THYAO", "15d", "60g"],
        default_timeframe="1d",
    )
    assert timeframe == "15m"
    assert timedelta(days=59, hours=23) < end - start <= timedelta(days=60)

    timeframe, start, end = handlers_stage5g._parse_backtest_window(
        ["THYAO", "2024-01-01", "2025-01-01"],
        default_timeframe="1d",
    )
    assert timeframe == "1d"
    assert start.isoformat().startswith("2024-01-01")
    assert end.isoformat().startswith("2025-01-01")


def test_sector_membership_uses_verified_csv_rows_and_active_flag(tmp_path):
    path = tmp_path / "symbols.csv"
    path.write_text(
        "symbol,sector_index,active\nGARAN,XBANK.IS,true\nAKBNK,XBANK.IS,true\nTEST,XBANK.IS,false\nEREGL,XMANA.IS,true\n",
        encoding="utf-8",
    )
    assert module._load_sector_symbols(path, "XBANK") == ["GARAN", "AKBNK"]
    with pytest.raises(module.UniverseCommandError, match="doğrulanmış"):
        module._load_sector_symbols(path, "XGIDA")


def test_index_membership_fails_closed_when_missing_incomplete_or_unverifiable(tmp_path):
    missing = tmp_path / "missing.csv"
    with pytest.raises(module.UniverseCommandError, match="bulunamadı"):
        module._load_index_symbols(missing, "XU030", 30)

    no_freshness = tmp_path / "no-freshness.csv"
    no_freshness.write_text("symbol,index_code\nAAA,XU030\nBBB,XU030\n", encoding="utf-8")
    with pytest.raises(module.UniverseCommandError, match="güncellik"):
        module._load_index_symbols(no_freshness, "XU030", 2)

    incomplete = tmp_path / "incomplete.csv"
    incomplete.write_text("symbol,index_code,active\nAAA,XU030,true\nBBB,XU030,true\n", encoding="utf-8")
    with pytest.raises(module.UniverseCommandError, match="2/30"):
        module._load_index_symbols(incomplete, "XU030", 30)
    assert module._load_index_symbols(incomplete, "XU030", 2) == ["AAA", "BBB"]


@pytest.mark.asyncio
async def test_backtest_stats_only_uses_current_users_runs_and_trades(tmp_path, monkeypatch):
    factory = factory_for(tmp_path)
    db = factory()
    owner = User(telegram_user_id=101)
    other = User(telegram_user_id=202)
    db.add_all([owner, other]); db.flush()
    owner_run = add_run(db, owner.id, run_id="owner")
    other_run = add_run(db, other.id, run_id="other")
    add_trade(db, owner_run, pnl=50, gross=55, cost=5)
    add_trade(db, owner_run, pnl=-20, gross=-18, cost=2)
    add_trade(db, other_run, pnl=999, gross=999, cost=0)
    db.commit(); db.close()

    monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(module, "_reject_unauthorized", allow)
    update = fake_update(101)
    await module.cmd_backtest_stats(update, SimpleNamespace(args=[]))
    text = update.message.calls[-1][0]
    assert "Koşu: 1" in text
    assert "İşlem: 2" in text
    assert "Net simülasyon K/Z: 30.00 TL" in text
    assert "999" not in text


@pytest.mark.asyncio
async def test_backtest_history_alias_authorizes_then_delegates(monkeypatch):
    monkeypatch.setattr(module, "_reject_unauthorized", allow)
    history = AsyncMock()
    monkeypatch.setattr(module, "cmd_backtest_ozet", history)
    update = fake_update()
    context = SimpleNamespace(args=["THYAO"])
    await module.cmd_backtest_history_alias(update, context)
    history.assert_awaited_once_with(update, context)


def _replay_plan(*, expires_at=None, quantity=10) -> SignalReplayPlan:
    created = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return SignalReplayPlan(
        source_signal_id=42,
        source_owner_user_id=1,
        symbol="THYAO",
        timeframe="1d",
        entry_order_type=EntryOrderType.LIMIT_BUY,
        entry_price=Decimal("100"),
        entry_zone_low=None,
        entry_zone_high=None,
        stop_price=Decimal("95"),
        targets=(Decimal("105"), Decimal("110"), Decimal("115")),
        target_allocations=(Decimal("40"), Decimal("35"), Decimal("25")),
        requested_quantity=quantity,
        quantity_assumption=None,
        created_at=created,
        data_timestamp=created,
        valid_from=created,
        expires_at=expires_at,
        provider="licensed_test",
        source="test",
        strategy_version="test",
        score=Decimal("80"),
        confidence="yuksek",
        risk_reward=Decimal("3"),
        price_adjustment_mode="raw",
    )


def _bars(*rows):
    return pd.DataFrame(rows)


def _bar(day, *, open=100, high=101, low=99, close=100, complete=True, **extra):
    return {
        "timestamp": datetime(2025, 1, day, tzinfo=timezone.utc),
        "open": open,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1_000_000,
        "is_complete": complete,
        **extra,
    }


def test_signal_replay_invalidates_when_pre_entry_stop_and_entry_share_candle():
    result = replay_signal_plan(
        _replay_plan(),
        _bars(_bar(2, open=100, high=106, low=94, close=101)),
        provider_name="licensed_test",
        as_of=datetime(2025, 1, 3, tzinfo=timezone.utc),
        execution_policy=ExecutionPolicy(fill_model=FillModel.FULL_FILL),
    )
    types = [event.event_type for event in result.events]
    # OHLC alone cannot prove whether the entry or invalidation happened first.
    # The conservative replay therefore does not invent a filled position.
    assert types == ["SIGNAL_CREATED", "ENTRY_INVALIDATED"]
    assert result.final_status == "INVALIDATED"
    assert result.realized_gross_pnl == Decimal("0.00")
    assert result.realized_net_pnl == Decimal("0.00")


def test_signal_replay_excludes_incomplete_bar_and_uses_microstructure_fields():
    result = replay_signal_plan(
        _replay_plan(),
        _bars(
            _bar(2, low=90, complete=False),
            _bar(
                3,
                open=101,
                high=102,
                low=100.5,
                close=101,
                upper_limit=110,
                lower_limit=90,
                upper_limit_locked=False,
                lower_limit_locked=False,
                trading_state="CONTINUOUS",
            ),
        ),
        provider_name="licensed_test",
        as_of=datetime(2025, 1, 4, tzinfo=timezone.utc),
        execution_policy=ExecutionPolicy(fill_model=FillModel.FULL_FILL),
    )
    assert result.excluded_incomplete_bars == 1
    assert result.final_status == "PENDING_ENTRY"
    assert result.derived_unfilled_at_end is True
    assert "trading_state" in result.microstructure_columns
    assert "lower_limit_locked" in result.microstructure_columns


def test_signal_replay_records_suspension_resume_and_upper_limit_unfilled():
    result = replay_signal_plan(
        _replay_plan(),
        _bars(
            _bar(2, trading_state="SUSPENDED"),
            _bar(
                3,
                open=100,
                high=100,
                low=100,
                close=100,
                trading_state="CONTINUOUS",
                upper_limit=100,
                upper_limit_locked=True,
                available_sell_quantity=0,
            ),
        ),
        provider_name="licensed_test",
        as_of=datetime(2025, 1, 4, tzinfo=timezone.utc),
        execution_policy=ExecutionPolicy(fill_model=FillModel.FULL_FILL),
    )
    types = [event.event_type for event in result.events]
    assert "TRADING_SUSPENDED" in types
    assert "TRADING_RESUMED" in types
    assert "ORDER_REMAINED_UNFILLED" in types
    assert result.final_status == "UNFILLED"


def test_signal_replay_expires_before_first_bar_at_or_after_expiry():
    expiry = datetime(2025, 1, 3, tzinfo=timezone.utc)
    result = replay_signal_plan(
        _replay_plan(expires_at=expiry),
        _bars(_bar(2, open=101, high=102, low=100.5, close=101), _bar(3, low=90)),
        provider_name="licensed_test",
        as_of=datetime(2025, 1, 4, tzinfo=timezone.utc),
        execution_policy=ExecutionPolicy(fill_model=FillModel.FULL_FILL),
    )
    assert result.final_status == "EXPIRED"
    assert [event.event_type for event in result.events][-1] == "SIGNAL_EXPIRED"
    assert not any(event.event_type == "ENTRY_FILLED" for event in result.events)


def test_signal_replay_can_expire_with_no_completed_post_signal_bar():
    expiry = datetime(2025, 1, 2, tzinfo=timezone.utc)
    result = replay_signal_plan(
        _replay_plan(expires_at=expiry),
        _bars(_bar(2, complete=False)),
        provider_name="licensed_test",
        as_of=datetime(2025, 1, 3, tzinfo=timezone.utc),
        execution_policy=ExecutionPolicy(fill_model=FillModel.FULL_FILL),
    )
    assert result.completed_bars == 0
    assert result.excluded_incomplete_bars == 1
    assert result.final_status == "EXPIRED"
    assert result.events[-1].event_type == "SIGNAL_EXPIRED"


def test_signal_replay_runs_tp1_tp2_tp3_and_uses_only_configured_cost_model():
    result = replay_signal_plan(
        _replay_plan(),
        _bars(
            _bar(2, open=100, high=105, low=96, close=104),
            _bar(3, open=106, high=110, low=101, close=109),
            _bar(4, open=111, high=115, low=106, close=114),
        ),
        provider_name="licensed_test",
        as_of=datetime(2025, 1, 5, tzinfo=timezone.utc),
        execution_policy=ExecutionPolicy(fill_model=FillModel.FULL_FILL),
        cost_model=TransactionCostModel(commission_rate=Decimal("0.001")),
        commission_description="test maliyeti",
    )
    assert result.final_status == "TP3_HIT"
    assert [event.event_type for event in result.events if event.event_type.startswith("TP")] == [
        "TP1_REACHED", "TP2_REACHED", "TP3_REACHED",
    ]
    # Exit processing begins on the bar after entry. Later gap opens receive
    # realistic price improvement instead of an impossible same-bar target.
    assert result.realized_gross_pnl == Decimal("94.00")
    assert result.total_cost == Decimal("2.09")
    assert result.realized_net_pnl == Decimal("91.91")


class _ReplayProvider:
    name = "licensed_test"

    def __init__(self, frame):
        self.frame = frame
        self.calls = 0

    def get_ohlcv(self, symbol, timeframe, start, end):
        self.calls += 1
        assert symbol == "THYAO"
        assert timeframe == "1d"
        assert start < end
        return self.frame


def _stored_signal(owner_id, data_timestamp):
    return Signal(
        user_id=owner_id,
        symbol="THYAO",
        timeframe="1d",
        signal_type=SignalTypeEnum.BUY_CANDIDATE,
        state=SignalStateEnum.PENDING_ENTRY,
        score=80,
        confidence="yuksek",
        entry_order_type="LIMIT_BUY",
        entry_trigger=100,
        planned_entry_price=Decimal("100"),
        stop_price=95,
        current_stop_price=Decimal("95"),
        target_1=105,
        target_2=110,
        target_3=115,
        requested_quantity=Decimal("10"),
        strategy_version="test",
        data_timestamp=data_timestamp,
        provider="licensed_test",
        source="test",
        idempotency_key=f"stored-{owner_id}-{data_timestamp.timestamp()}",
        price_adjustment_mode="raw",
        valid_from=data_timestamp,
        created_at=data_timestamp,
    )


@pytest.mark.asyncio
async def test_backtest_signal_command_replays_id_without_mutating_production_signal(tmp_path, monkeypatch):
    factory = factory_for(tmp_path)
    knowledge = datetime.now(timezone.utc) - timedelta(days=4)
    db = factory()
    owner = User(telegram_user_id=101)
    db.add(owner); db.flush()
    signal = _stored_signal(owner.id, knowledge)
    db.add(signal); db.commit()
    signal_id = signal.id
    original_timestamp = signal.data_timestamp
    db.close()

    frame = pd.DataFrame([
        {
            "timestamp": knowledge + timedelta(days=1),
            "open": 100, "high": 106, "low": 94, "close": 101,
            "volume": 1_000_000, "is_complete": True,
        }
    ])
    provider = _ReplayProvider(frame)
    settings = SimpleNamespace(
        backtest_commission_rate=None,
        backtest_commission_minimum=None,
        backtest_commission_tax_rate=None,
        backtest_fill_model="full_fill",
        backtest_limit_lock_mode="conservative",
        max_daily_volume_participation_percent=1,
        breakout_minimum_volume_ratio=1.2,
    )
    monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "build_market_data_provider", lambda _settings: provider)
    monkeypatch.setattr(module, "_reject_unauthorized", allow)
    update = fake_update(101)
    await module.cmd_backtest_signal_alias(update, SimpleNamespace(args=[str(signal_id)]))
    text = update.message.calls[-1][0]
    assert f"SİNYAL #{signal_id}" in text
    assert "INVALIDATED" in text
    assert "Komisyon ayarlanmamış" in text
    assert provider.calls == 1

    db = factory()
    unchanged = db.query(Signal).filter_by(id=signal_id).one()
    assert unchanged.state == SignalStateEnum.PENDING_ENTRY
    assert unchanged.data_timestamp.replace(tzinfo=timezone.utc) == original_timestamp
    assert db.query(SignalEvent).filter_by(signal_id=signal_id).count() == 0
    db.close()


@pytest.mark.asyncio
async def test_backtest_signal_command_enforces_owned_signal_boundary(tmp_path, monkeypatch):
    factory = factory_for(tmp_path)
    knowledge = datetime.now(timezone.utc) - timedelta(days=4)
    db = factory()
    owner = User(telegram_user_id=101)
    stranger = User(telegram_user_id=202)
    db.add_all([owner, stranger]); db.flush()
    signal = _stored_signal(owner.id, knowledge)
    db.add(signal); db.commit(); signal_id = signal.id; db.close()
    provider = _ReplayProvider(pd.DataFrame())
    monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(module, "build_market_data_provider", lambda _settings: provider)
    monkeypatch.setattr(module, "_reject_unauthorized", allow)
    update = fake_update(202)
    await module.cmd_backtest_signal_alias(update, SimpleNamespace(args=[str(signal_id)]))
    assert "bulunamadı veya" in update.message.calls[-1][0]
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_backtest_signal_command_allows_unowned_legacy_analysis_with_disclosed_one_lot(tmp_path, monkeypatch):
    factory = factory_for(tmp_path)
    knowledge = datetime.now(timezone.utc) - timedelta(days=4)
    db = factory()
    signal = _stored_signal(None, knowledge)
    signal.entry_order_type = None
    signal.planned_entry_price = None
    signal.requested_quantity = None
    db.add(signal); db.commit(); signal_id = signal.id; db.close()
    provider = _ReplayProvider(pd.DataFrame([{
        "timestamp": knowledge + timedelta(days=1),
        "open": 99, "high": 103, "low": 98, "close": 101,
        "volume": 1_000_000, "is_complete": True,
    }]))
    settings = SimpleNamespace(
        backtest_commission_rate=None,
        backtest_commission_minimum=None,
        backtest_commission_tax_rate=None,
        backtest_fill_model="full_fill",
        backtest_limit_lock_mode="conservative",
        max_daily_volume_participation_percent=1,
        breakout_minimum_volume_ratio=1.2,
    )
    monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "build_market_data_provider", lambda _settings: provider)
    monkeypatch.setattr(module, "_reject_unauthorized", allow)
    update = fake_update(999)
    await module.cmd_backtest_signal_alias(update, SimpleNamespace(args=[str(signal_id)]))
    text = update.message.calls[-1][0]
    assert f"SİNYAL #{signal_id}" in text
    assert "1 lota normalize edildi" in text
    assert provider.calls == 1


def test_register_helper_exposes_complete_backtest_command_surface():
    application = Application.builder().token("123456:test-token").build()
    module.register_ultra_backtest_handlers(application)
    commands = {
        command
        for group in application.handlers.values()
        for handler in group
        for command in (getattr(handler, "commands", set()) or set())
    }
    assert {
        "backtest_signal", "backtest_gecmisi", "backtest_stats", "backtest_watchlist",
        "backtest_sector", "backtest_bist30", "backtest_bist50", "backtest_bist100",
    } <= commands


@pytest.mark.asyncio
async def test_bist_command_reports_missing_membership_and_creates_no_run(tmp_path, monkeypatch):
    factory = factory_for(tmp_path)
    settings = SimpleNamespace(
        admin_ids=[], default_total_capital=100_000,
        bist_symbols_csv_path=str(tmp_path / "bist_symbols.csv"),
    )
    monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "_reject_unauthorized", allow)
    update = fake_update()
    await module.cmd_backtest_bist30(update, SimpleNamespace(args=["1g", "3y"], bot=SimpleNamespace()))
    assert "bist_index_membership.csv" in update.message.calls[-1][0]
    db = factory()
    assert db.query(BacktestRun).count() == 0
    db.close()


@pytest.mark.asyncio
async def test_watchlist_command_schedules_background_task_without_waiting(tmp_path, monkeypatch):
    factory = factory_for(tmp_path)
    db = factory()
    user = User(telegram_user_id=101)
    db.add(user); db.flush()
    db.add(WatchlistItem(user_id=user.id, symbol="THYAO")); db.commit(); db.close()

    settings = SimpleNamespace(
        admin_ids=[], default_total_capital=100_000,
        bist_symbols_csv_path=str(tmp_path / "symbols.csv"),
        backtest_max_concurrent_per_user=1, backtest_timeout_seconds=30,
    )
    config = BacktestConfig(
        minimum_history_bars=1,
        transaction_costs=TransactionCostConfig(0, 0, 0, 0, 0),
    )
    gate = asyncio.Event()

    async def delayed(**_kwargs):
        await gate.wait()

    monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "get_strategy_config", lambda: {"strategy": {"version": "test"}})
    monkeypatch.setattr(module, "build_market_data_provider", lambda _settings: SimpleNamespace(name="fake"))
    monkeypatch.setattr(module, "_backtest_config", lambda: config)
    monkeypatch.setattr(module, "_reject_unauthorized", allow)
    monkeypatch.setattr(module, "_execute_universe_job", delayed)
    module._ACTIVE_UNIVERSE_TASKS.clear()
    update = fake_update()
    context = SimpleNamespace(args=["1g", "30g"], bot=SimpleNamespace())
    await module.cmd_backtest_watchlist(update, context)
    assert "arka planda başladı" in update.message.calls[-1][0]
    task = next(iter(module._ACTIVE_UNIVERSE_TASKS.values()))
    assert not task.done()
    gate.set()
    await task
    await asyncio.sleep(0)
    assert module._ACTIVE_UNIVERSE_TASKS == {}


def test_universe_timeout_marks_remaining_symbols_without_stopping_result_object():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    request = UniverseBacktestRequest(
        "watchlist", start, start + timedelta(days=10), symbols=("AAA", "BBB", "CCC")
    )
    engine = UniverseBacktestEngine(
        BacktestConfig(minimum_history_bars=1, transaction_costs=TransactionCostConfig(0, 0, 0, 0, 0))
    )

    def slow_failure(*_args):
        import time
        time.sleep(0.02)
        raise RuntimeError("provider down")

    result = engine.run(
        request,
        data_loader=slow_failure,
        signal_provider_factory=lambda _symbol: lambda _context: SignalInstruction(),
        timeout_seconds=0.005,
    )
    assert set(result.failures) == {"AAA", "BBB", "CCC"}
    assert "RuntimeError" in result.failures["AAA"]
    assert "BacktestTimeout" in result.failures["BBB"]


def test_one_symbol_provider_error_does_not_discard_other_universe_results():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    request = UniverseBacktestRequest(
        "watchlist", start, start + timedelta(days=10), symbols=("AAA", "BAD", "CCC")
    )
    bars = pd.DataFrame([
        {
            "timestamp": start + timedelta(days=index),
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 1000,
            "is_complete": True,
            "data_quality": "VALID",
            "price_mode": "adjusted",
        }
        for index in range(4)
    ])
    engine = UniverseBacktestEngine(
        BacktestConfig(minimum_history_bars=1, transaction_costs=TransactionCostConfig(0, 0, 0, 0, 0))
    )

    def loader(symbol, *_args):
        if symbol == "BAD":
            raise RuntimeError("provider down")
        return bars

    result = engine.run(
        request,
        data_loader=loader,
        signal_provider_factory=lambda _symbol: lambda _context: SignalInstruction(),
    )
    assert set(result.results) == {"AAA", "CCC"}
    assert set(result.failures) == {"BAD"}


def test_partial_universe_result_is_persisted_under_owning_user(tmp_path, monkeypatch):
    factory = factory_for(tmp_path)
    db = factory()
    owner = User(telegram_user_id=101)
    other = User(telegram_user_id=202)
    db.add_all([owner, other]); db.flush()
    record = add_run(db, owner.id, run_id="universe-owned", status="PENDING")
    record.scope = "watchlist"
    record.symbol = "WATCHLIST"
    record.sector = None
    db.commit(); db.close()

    now = datetime.now(timezone.utc)
    trade = SimpleNamespace(
        symbol="THYAO", entry_time=now - timedelta(days=2), exit_time=now,
        entry_price=100.0, exit_price=110.0, quantity=10.0,
        net_pnl=95.0, gross_pnl=100.0, total_cost=5.0, exit_reason="TARGET",
        stop_price=95.0, target_1=105.0, target_2=110.0, target_3=115.0,
        target_1_hit=True, target_2_hit=True, target_3_hit=False,
        mae_percent=-2.0, mfe_percent=11.0, holding_bars=2,
        market_regime="boga", sector="Ulastirma", signal_type="BUY_CANDIDATE",
        raw_signal_score=80.0,
    )
    symbol_result = SimpleNamespace(
        trades=[trade], data_version="version-a",
        metrics=SimpleNamespace(total_return_percent=9.5, max_drawdown_percent=-2.0),
    )
    result = SimpleNamespace(
        request=SimpleNamespace(symbols=("THYAO", "BAD")),
        results={"THYAO": symbol_result},
        failures={"BAD": "DataUnavailableError"},
    )
    monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    metrics = module._persist_universe_result(
        "universe-owned", owner.id, result, "Güncel üyelik kullanıldı."
    )
    assert metrics["symbols_completed"] == 1
    assert metrics["symbols_failed"] == 1
    db = factory()
    persisted = db.query(BacktestRun).filter_by(run_id="universe-owned", user_id=owner.id).one()
    assert persisted.run_status == "COMPLETED"
    assert db.query(BacktestTrade).filter_by(backtest_run_id=persisted.id).count() == 1
    assert db.query(BacktestRun).filter_by(run_id="universe-owned", user_id=other.id).count() == 0
    db.close()


@pytest.mark.asyncio
async def test_backtest_summary_no_longer_references_undefined_trades(tmp_path, monkeypatch):
    factory = factory_for(tmp_path)
    db = factory()
    user = User(telegram_user_id=101)
    db.add(user); db.flush()
    add_run(db, user.id, run_id="summary-run")
    db.commit(); db.close()
    monkeypatch.setattr(handlers_stage5g, "get_session_factory", lambda: factory)
    update = fake_update()
    await handlers_stage5g.cmd_backtest_ozet(update, SimpleNamespace(args=[]))
    text, keyboard = update.message.calls[-1]
    assert "summary-run" in text
    assert keyboard.inline_keyboard[0][0].callback_data == "stage5g_btmetric_summary-run"
