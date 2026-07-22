from __future__ import annotations

import numpy as np
import pandas as pd

from app.analysis.breakout_scenario_engine import compute_breakout_scenarios
from app.analysis.confluence_zone_engine import find_confluence_zones, strongest_confluence
from app.analysis.price_scenario_engine import compute_price_scenarios
from app.analysis.timeframe_levels_engine import compute_timeframe_levels
from app.telegram.bot import build_telegram_application
from app.telegram.message_templates_v3 import format_breakout_scenarios, format_price_scenarios


def _all_registered_commands(application) -> set[str]:
    commands = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            cmds = getattr(handler, "commands", None)
            if cmds:
                commands.update(cmds)
    return commands


def _oscillating_df(n_days: int = 400, floor: float = 90.0, ceiling: float = 110.0, seed: int = 31) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n_days, tz="UTC")
    mid = (floor + ceiling) / 2
    amplitude = (ceiling - floor) / 2
    closes = mid + amplitude * 0.85 * np.sin(np.linspace(0, 14 * np.pi, n_days)) + rng.normal(0, 0.3, n_days)
    closes = np.clip(closes, floor + 0.5, ceiling - 0.5)
    highs = closes + rng.uniform(0.2, 1.0, n_days)
    lows = closes - rng.uniform(0.2, 1.0, n_days)
    opens = closes + rng.normal(0, 0.3, n_days)
    volumes = rng.uniform(800_000, 1_200_000, n_days)
    return pd.DataFrame(
        {"timestamp": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes}
    )


def test_senaryo_and_kirilsanaryo_commands_registered():
    application = build_telegram_application()
    commands = _all_registered_commands(application)
    assert "senaryo" in commands
    assert "kirilsanaryo" in commands
    # onceki asamalarin komutlari da hala kayitli olmali
    assert "seviyeler" in commands
    assert "analiz" in commands


def test_format_price_scenarios_contains_sections():
    df = _oscillating_df()
    current_price = float(df["close"].iloc[-1])
    levels = compute_timeframe_levels(df, current_price)
    supports, resistances = find_confluence_zones(levels, current_price)
    result = compute_price_scenarios(levels, supports, resistances, current_price)

    text = format_price_scenarios("THYAO", current_price, result)
    assert "MERGEN QUANT" in text
    assert "DÜŞÜŞ SENARYOLARI" in text
    assert "YÜKSELİŞ SENARYOLARI" in text
    assert "yatırım tavsiyesi değildir" in text.lower()
    assert "maksimum dip" not in text.lower()
    assert "maksimum yükseliş" not in text.lower()


def test_format_breakout_scenarios_contains_sections():
    df = _oscillating_df()
    current_price = float(df["close"].iloc[-1])
    levels = compute_timeframe_levels(df, current_price)
    supports, resistances = find_confluence_zones(levels, current_price)
    best_support, best_resistance = strongest_confluence(levels, current_price)
    resistance_zone = best_resistance or levels.daily.main_resistance
    support_zone = best_support or levels.daily.main_support

    result = compute_breakout_scenarios(
        resistance_zone=resistance_zone,
        support_zone=support_zone,
        current_price=current_price,
        atr_value=1.5,
        relative_volume=1.6,
        adx=25.0,
        liquidity_score=80.0,
    )

    text = format_breakout_scenarios("THYAO", current_price, result)
    assert "MERGEN QUANT" in text
    assert "DİRENÇ KIRILIRSA" in text
    assert "DESTEK KIRILIRSA" in text
    assert "sahte kırılım" in text.lower() or "kirilim" in text.lower()
    assert "yatırım tavsiyesi değildir" in text.lower()


def test_format_price_scenarios_handles_unreliable_case():
    from app.analysis.price_scenario_engine import PriceScenarioResult

    result = PriceScenarioResult(current_price=10.0, reliable=False, note="Guvenilir senaryo hesaplanamadi.")
    text = format_price_scenarios("TESTX", 10.0, result)
    assert "Guvenilir senaryo hesaplanamadi" in text


def test_format_breakout_scenarios_handles_unreliable_case():
    from app.analysis.breakout_scenario_engine import BreakoutScenarioResult

    result = BreakoutScenarioResult(reliable=False, note="Guvenilir seviye hesaplanamadi.")
    text = format_breakout_scenarios("TESTX", 10.0, result)
    assert "Guvenilir seviye hesaplanamadi" in text
