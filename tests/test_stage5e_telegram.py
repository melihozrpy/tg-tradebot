from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd

from app.analysis.corporate_actions_engine import normalize_corporate_actions
from app.analysis.gyo_valuation_engine import evaluate_gyo_valuation
from app.analysis.long_term_scenario_engine import compute_long_term_scenarios
from app.analysis.user_target_engine import evaluate_user_target
from app.services.current_price_service import CurrentPriceResult
from app.telegram.bot import build_telegram_application
from app.telegram.message_templates_v3 import (
    format_corporate_actions,
    format_long_term_scenarios,
    format_target_history,
    format_target_performance_stage5e,
    format_target_roadmap,
    format_user_target_check,
    format_valuation,
    split_long_message,
)


def _commands(app):
    result = set()
    for handlers in app.handlers.values():
        for handler in handlers:
            result.update(getattr(handler, "commands", set()) or set())
    return result


def _price():
    return CurrentPriceResult(
        "SVGYO", 12.72, datetime(2026, 7, 18, tzinfo=timezone.utc),
        "completed_5m", True, 13.0, 12.5, -2.15,
    )


def _df():
    close = np.linspace(8, 13, 250)
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=250, freq="1D", tz="UTC"),
            "open": close * 0.99, "high": close * 1.02, "low": close * 0.98,
            "close": close, "volume": [1_000_000] * 250,
        }
    )


def test_all_stage5e_commands_are_registered():
    commands = _commands(build_telegram_application())
    expected = {
        "cokluzaman", "uzunsenaryo", "hedefkontrol", "hedefyolu", "degerleme",
        "uzungrafik", "sermaye_islemleri", "hedefgecmisi", "hedefbasari",
    }
    assert expected <= commands


def test_long_term_scenario_message_has_price_metadata():
    result = compute_long_term_scenarios(_df(), 12.72)
    text = format_long_term_scenarios("SVGYO", _price(), result)
    assert "Güncel fiyat:" in text
    assert "Son kesinleşmiş kapanış:" in text
    assert "Fiyat kaynağı:" in text
    assert "kesin fiyat tahminleri değildir" in text


def test_target_check_message_marks_user_target_as_independent():
    evaluation = evaluate_user_target("SVGYO", 12.72, 70, intermediate_levels=[15, 20, 30])
    text = format_user_target_check(evaluation, _price())
    assert "KULLANICI HEDEFİ: 70.00 TL" in text
    assert "5.50x" in text
    assert "botun teknik hedefi veya AL sinyali değildir" in text


def test_target_roadmap_message_stays_within_split_limit():
    evaluation = evaluate_user_target("SVGYO", 12.72, 70, intermediate_levels=[15, 20, 30, 40, 50, 60])
    text = format_target_roadmap("SVGYO", evaluation.roadmap, _price())
    assert "Hedef Yolu".upper() in text.upper()
    assert all(len(part) <= 4000 for part in split_long_message(text))


def test_valuation_message_reports_missing_data_explicitly():
    result = evaluate_gyo_valuation("SVGYO", 12.72, {"status": "unavailable"})
    text = format_valuation(result, _price())
    assert "Veri yetersiz" in text
    assert "Veri bulunamadı" in text


def test_corporate_actions_message_contains_price_mode_note():
    events = normalize_corporate_actions("SVGYO", [{"type": "split", "date": "2026-01-01", "ratio": "2:1"}])
    text = format_corporate_actions("SVGYO", events, _price())
    assert "Hisse bölünmesi" in text
    assert "ayarlı seri" in text


def test_target_history_and_performance_messages_smoke():
    row = SimpleNamespace(
        data_timestamp=datetime(2026, 1, 1), target_type="Ana hedef",
        target_low=20, target_high=21, status="Aktif", confidence=60,
    )
    assert "Ana hedef" in format_target_history("SVGYO", [row])
    report = SimpleNamespace(
        symbol="SVGYO", total_targets=1, reached_targets=0, partially_reached_targets=0,
        invalidated_targets=0, expired_targets=0, success_rate=0,
        average_days_to_target=None, average_max_drawdown_percent=None,
        average_max_upside_percent=None, invalidation_rate=0,
        extreme_bull_success_rate=None, by_horizon={},
    )
    assert "HEDEF BAŞARISI" in format_target_performance_stage5e(report)
