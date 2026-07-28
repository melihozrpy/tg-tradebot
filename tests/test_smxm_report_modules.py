from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis.smart_money_engine import detect_smart_money
from app.config.instruments import parse_instruments_env, resolve_report_instruments, universe_symbols
from app.config.settings import Settings
from app.models.database import Base, User
from app.modules.backtest_engine import (
    SmxmSignalCandidate,
    SmxmVirtualPortfolioEngine,
    VirtualRiskRules,
    VirtualTradingError,
    run_smxm_backtest,
)
from app.modules.chart_engine import ChecklistVisual, ReportChartSpec, render_report_chart
from app.modules.evening_report import build_evening_report
from app.modules.morning_report import (
    build_morning_report,
    parse_economic_calendar_html,
)
from app.services.groq_service import GroqExplainer
from app.services.instrument_universe_service import scan_best_entries


def _bars(*, periods: int = 300, bullish: bool = True) -> pd.DataFrame:
    timestamps = pd.date_range("2025-01-01", periods=periods, freq="B", tz="UTC")
    slope = np.linspace(85.0, 132.0, periods)
    wave = np.sin(np.arange(periods) / 6.0) * 2.1
    close = slope + wave if bullish else 220.0 - slope + wave
    opened = close - np.sin(np.arange(periods) / 4.0) * 0.8
    high = np.maximum(opened, close) + 1.4
    low = np.minimum(opened, close) - 1.3
    volume = 1_000_000 + (np.arange(periods) % 17) * 35_000
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opened,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "is_complete": True,
        }
    )


class _Provider:
    name = "unit"

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame

    def get_ohlcv(self, symbol, timeframe, start, end):
        if symbol in {"^VIX", "DX-Y.NYB"}:
            risk = self.frame.tail(20).copy()
            risk["close"] = np.linspace(20, 19, len(risk))
            risk["open"] = risk["close"]
            risk["high"] = risk["close"] + 0.2
            risk["low"] = risk["close"] - 0.2
            return risk
        return self.frame.copy()


def _settings(**overrides):
    values = {
        "timezone_name": "Europe/Istanbul",
        "xu100_symbol": "XU100.IS",
        "vix_symbol": "^VIX",
        "dxy_symbol": "DX-Y.NYB",
        "groq_enabled": False,
        "instruments": '["THYAO","ASELS"]',
        "bist_universe_json_path": "app/config/bist_instruments.json",
        "virtual_trade_blocked_weekdays": "0,4",
        "virtual_trade_risk_percent": 1.0,
        "virtual_trade_after_loss_risk_percent": 0.5,
        "virtual_trade_minimum_rr": 2.0,
        "virtual_trade_minimum_checklist": 5,
        "virtual_portfolio_max_per_user": 3,
        "virtual_portfolio_max_strategies": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_pdf_universe_and_env_instruments_are_config_driven():
    symbols = universe_symbols("app/config/bist_instruments.json")
    assert len(symbols) == 571
    assert symbols[0] == "AAGYO"
    assert symbols[-1] == "RYGYO"
    assert parse_instruments_env('["THYAO", "ASELS", "THYAO"]') == ["THYAO", "ASELS"]
    assert resolve_report_instruments(_settings()) == ["XU100", "THYAO", "ASELS"]


def test_calendar_parser_maps_impact_and_affected_instruments():
    html = """
    <table><tr class="js-event-item" data-event-datetime="2026/07/28 15:30:00" data-impact="high">
      <td class="time">15:30</td><td class="flagCur">US</td><td class="sentiment">high</td>
      <td class="event">Fed Faiz Kararı</td><td class="act">%5</td>
      <td class="fore">%5</td><td class="prev">%5,25</td>
    </tr></table>
    """
    events = parse_economic_calendar_html(
        html,
        report_date=datetime(2026, 7, 28).date(),
        timezone_name="Europe/Istanbul",
        instruments=["XU100", "THYAO", "XAUUSD", "EURUSD"],
    )
    assert len(events) == 1
    assert events[0].impact == "high"
    assert {"XU100", "THYAO", "XAUUSD", "EURUSD"}.issubset(events[0].affected_instruments)


def test_morning_and_evening_reports_log_bias_comparison():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    now = datetime(2026, 7, 28, 5, 0, tzinfo=timezone.utc)
    provider = _Provider(_bars())
    settings = _settings()
    try:
        morning = build_morning_report(
            provider, settings, ["XU100"], db=db, now=now, calendar_events=[]
        )
        assert morning.instruments[0].checklist_score <= 6
        assert 0 <= morning.confidence.score <= 100
        evening = build_evening_report(
            provider,
            settings,
            ["XU100"],
            db=db,
            now=now.replace(hour=18),
            calendar_events=[],
        )
        assert evening.instruments[0].comparison.predicted is not None
        assert evening.instruments[0].comparison.consistent in {True, False}
    finally:
        db.close()


def test_virtual_portfolio_reduces_risk_after_loss_and_enforces_limits():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    user = User(telegram_user_id=1001)
    db.add(user)
    db.commit()
    rules = VirtualRiskRules()
    service = SmxmVirtualPortfolioEngine(db, rules)
    try:
        portfolio = service.create_portfolio(user_id=user.id, name="Ana", starting_balance=100_000)
        first = service.open_trade(
            portfolio_id=portfolio.id,
            candidate=SmxmSignalCandidate("THYAO", "long", 100, 95, 110, 2.0, 5),
            opened_at=datetime(2026, 7, 28, 10, tzinfo=timezone.utc),
        )
        assert first.risk_percent == 1.0
        service.close_trade(trade_id=first.id, exit_price=95)
        second = service.open_trade(
            portfolio_id=portfolio.id,
            candidate=SmxmSignalCandidate("ASELS", "long", 200, 190, 220, 2.0, 6),
            opened_at=datetime(2026, 7, 29, 10, tzinfo=timezone.utc),
        )
        assert second.risk_percent == 0.5
        service.create_portfolio(user_id=user.id, name="İkinci", starting_balance=10_000)
        service.create_portfolio(user_id=user.id, name="Üçüncü", starting_balance=10_000)
        with pytest.raises(VirtualTradingError):
            service.create_portfolio(user_id=user.id, name="Dördüncü", starting_balance=10_000)
    finally:
        db.close()


def test_smxm_backtest_runs_chronologically_for_at_least_one_month():
    frame = _bars(periods=320)
    start = frame.iloc[100]["timestamp"].to_pydatetime()
    end = frame.iloc[-1]["timestamp"].to_pydatetime()
    result = run_smxm_backtest(
        frame,
        instrument="THYAO",
        start_date=start,
        end_date=end,
        starting_balance=10_000,
        rules=VirtualRiskRules(blocked_weekdays=()),
        long_only=True,
    )
    assert result.starting_balance == 10_000
    assert len(result.equity_values) > 20
    assert result.max_drawdown_percent >= 0


def test_chart_engine_renders_dark_report_png(tmp_path):
    frame = _bars(periods=160)
    spec = ReportChartSpec(
        instrument="THYAO",
        timeframe="1D",
        report_kind="morning",
        direction="bullish",
        sentiment_score=72,
        checklist=tuple(ChecklistVisual(f"Madde {index}", index != 3) for index in range(1, 7)),
        entry_low=125,
        entry_high=127,
        stop=121,
        targets=(137, 143),
        rr=2.4,
        date_label="28.07.2026",
    )
    path = render_report_chart(
        frame,
        spec,
        smart_money=detect_smart_money(frame),
        output_dir=tmp_path,
        dpi=110,
    )
    with Image.open(path) as image:
        assert image.width > 1000
        assert image.height > 600


def test_groq_news_sentiment_has_deterministic_cached_fallback():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    explainer = GroqExplainer(Settings(groq_enabled=False, groq_api_key=""))
    try:
        labels, fallback = explainer.classify_news_sentiment(
            db, ["strong growth beat record", "loss risk crisis"]
        )
        assert labels == ["positive", "negative"]
        assert fallback is True
        cached_labels, cached_fallback = explainer.classify_news_sentiment(
            db, ["strong growth beat record", "loss risk crisis"]
        )
        assert cached_labels == labels
        assert cached_fallback is True
    finally:
        explainer.close()
        db.close()


def test_universe_scan_isolates_a_single_symbol_failure():
    class Provider(_Provider):
        def get_ohlcv(self, symbol, timeframe, start, end):
            if symbol == "BROKEN":
                raise RuntimeError("provider unavailable")
            return super().get_ohlcv(symbol, timeframe, start, end)

    result = scan_best_entries(
        lambda: Provider(_bars()),
        ["THYAO", "BROKEN"],
        minimum_score=0,
        max_workers=2,
        long_only=False,
        now=datetime(2026, 7, 28, tzinfo=timezone.utc),
    )
    assert result.symbols_requested == 2
    assert result.symbols_succeeded == 1
    assert result.symbols_failed == 1
    assert result.failures[0][0] == "BROKEN"
