from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app.analysis.viop_engine import (
    estimate_viop_contract_risk,
    load_viop_universe,
    parse_viop_horizon,
    priority_viop_symbols,
)
from app.analysis.viop_warrant_copilot import (
    NewsRiskCheck,
    build_copilot_scenario,
    format_copilot_scenario,
)
from app.config.settings import Settings
from app.telegram.basic_viop_handlers import _parse_capital


def test_viop_universe_is_dated_and_keeps_group_one_watchlist_first():
    universe = load_viop_universe("app/config/viop_underlyings.json")

    assert universe.verified_on == "2026-08-11"
    assert universe.source_url.startswith("https://www.borsaistanbul.com/")
    assert "THYAO" in priority_viop_symbols(universe, maximum=15)
    assert all(item.market_maker_group == 1 for item in universe.underlyings[:15])


def test_viop_stop_risk_estimate_can_legitimately_reject_small_capital():
    result = estimate_viop_contract_risk(
        capital=5_000,
        entry_spot=100,
        stop_spot=99,
        multiplier=100,
        risk_percent=0.5,
    )

    assert result.risk_budget == 25
    assert result.estimated_loss_per_contract == 100
    assert result.maximum_contracts_by_stop == 0
    assert result.requires_live_margin_check is True


def test_viop_horizon_aliases_are_explicit():
    assert parse_viop_horizon(None) == "gunici"
    assert parse_viop_horizon("hafta") == "haftalik"
    assert parse_viop_horizon("ay") == "aylik"
    assert parse_viop_horizon("rastgele") is None


def test_viop_capital_parser_accepts_common_turkish_inputs_without_currency_guessing():
    assert _parse_capital("5000") == 5_000
    assert _parse_capital("5.000") == 5_000
    assert _parse_capital("10k") == 10_000
    assert _parse_capital("beşbin") is None


def _copilot_bars(periods: int = 300) -> pd.DataFrame:
    timestamps = pd.date_range("2025-01-01", periods=periods, freq="B", tz="UTC")
    close = np.linspace(80.0, 150.0, periods) + np.sin(np.arange(periods) / 4.0)
    opened = close - 0.35
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opened,
            "high": close + 1.1,
            "low": close - 1.1,
            "close": close,
            "volume": 1_000_000 + np.arange(periods) * 500,
        }
    )


def test_borsa_copilot_keeps_news_gate_and_never_formats_market_entry_from_last_close():
    frame = _copilot_bars()
    scenario = build_copilot_scenario(
        product="viop",
        symbol="THYAO",
        horizon="gunici",
        htf_frame=frame,
        ltf_frame=frame,
        settings=Settings(),
        news_check=NewsRiskCheck(False, "KAP kaynağı kapalı"),
        now=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )

    assert scenario.valid_setup is False
    assert next(item for item in scenario.checklist if item.key == "news").passed is False
    rendered = format_copilot_scenario(scenario, timezone_name="Europe/Istanbul")
    assert "6 ADIM:" in rendered
    assert "KAP kaynağı kapalı" in rendered
    if scenario.entry_low is not None:
        assert scenario.entry_low != scenario.current_price
        assert scenario.entry_high != scenario.current_price
