from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app.analysis.anomaly_engine import ANOMALY_GAP_DOWN, detect_anomalies
from app.analysis.corporate_actions_engine import (
    apply_price_adjustments,
    classify_price_gap,
    normalize_corporate_actions,
)
from app.analysis.gyo_valuation_engine import evaluate_gyo_valuation


def _fundamentals():
    return {
        "shares_outstanding": 100_000_000,
        "net_asset_value": 2_000_000_000,
        "total_equity": 1_500_000_000,
        "previous_equity": 1_250_000_000,
        "total_assets": 3_000_000_000,
        "total_debt": 700_000_000,
        "cash": 200_000_000,
        "property_portfolio_value": 2_500_000_000,
        "rental_income": 100_000_000,
        "net_income": 250_000_000,
        "financial_period_date": "2026-06-30",
    }


def test_gyo_is_classified_from_market_cap_to_nav():
    result = evaluate_gyo_valuation("SVGYO", 10, _fundamentals(), sector_name="Gayrimenkul Yatırım Ortaklığı")
    assert result.applicable
    assert result.current_market_cap == 1_000_000_000
    assert result.market_cap_to_nav == 0.5
    assert result.classification == "Çok iskontolu"


def test_non_gyo_never_uses_gyo_formula():
    result = evaluate_gyo_valuation("THYAO", 10, _fundamentals(), sector_name="Ulaştırma")
    assert result.applicable is False
    assert result.classification == "Uygulanamaz"
    assert result.market_cap_to_nav is None


def test_nav_discount_calculation_is_correct():
    result = evaluate_gyo_valuation("SVGYO", 15, _fundamentals())
    assert result.market_cap_to_nav == 0.75
    assert result.nav_discount_premium_percent == 25.0


def test_missing_valuation_data_is_not_fabricated():
    result = evaluate_gyo_valuation("SVGYO", 15, {"status": "unavailable"})
    assert result.classification == "Veri yetersiz"
    assert result.net_asset_value is None
    assert any("Veri bulunamadı" in warning for warning in result.warnings)


def test_old_financial_period_is_marked_stale():
    data = _fundamentals()
    data["financial_period_date"] = "2024-12-31"
    result = evaluate_gyo_valuation(
        "SVGYO", 15, data, as_of=datetime(2026, 7, 18, tzinfo=timezone.utc)
    )
    assert result.data_is_stale is True
    assert any("Veri eski" in warning for warning in result.warnings)


def test_split_is_normalized_with_adjustment_factor():
    events = normalize_corporate_actions("TEST", [{"type": "split", "date": "2026-01-03", "ratio": "2:1"}])
    assert events[0].share_ratio == 2
    assert events[0].adjustment_factor == 0.5


def test_raw_and_adjusted_series_are_kept_separate():
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-01-02", "2026-01-03"], utc=True),
            "open": [100, 50], "high": [102, 52], "low": [98, 49], "close": [100, 50],
            "volume": [1000, 2000],
        }
    )
    events = normalize_corporate_actions("TEST", [{"type": "split", "date": "2026-01-03", "ratio": "2:1"}])
    adjusted = apply_price_adjustments(df, events, mode="adjusted")
    raw = apply_price_adjustments(df, events, mode="raw")
    assert adjusted.iloc[0]["close"] == 50
    assert raw.iloc[0]["close"] == 100
    assert adjusted.iloc[0]["raw_price"] == 100
    assert adjusted.iloc[0]["adjusted_price"] == 50


def test_bonus_or_split_gap_is_excluded_from_normal_gap_alarm():
    events = normalize_corporate_actions("TEST", [{"type": "bonus", "date": "2026-01-03", "ratio": 2}])
    classification = classify_price_gap(events, datetime(2026, 1, 3).date())
    assert classification["excluded_from_gap_alarm"] is True
    assert classification["kind"] == "capital_adjustment"


def test_dividend_gap_is_classified_separately():
    events = normalize_corporate_actions("TEST", [{"type": "dividend", "date": "2026-01-03", "amount": 2}])
    classification = classify_price_gap(events, datetime(2026, 1, 3).date())
    assert classification["kind"] == "dividend_gap"
    assert classification["excluded_from_gap_alarm"] is True


def test_anomaly_engine_does_not_report_split_gap_as_normal_gap():
    timestamps = pd.date_range("2025-12-10", periods=25, freq="1D", tz="UTC")
    close = [100.0] * 24 + [50.0]
    df = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [100.0] * 24 + [50.0],
            "high": [101.0] * 24 + [51.0],
            "low": [99.0] * 24 + [49.0],
            "close": close,
            "volume": [1000.0] * 25,
        }
    )
    actions = [{"type": "split", "date": timestamps[-1], "ratio": "2:1"}]
    result = detect_anomalies(df, "TEST", corporate_actions=actions)
    assert ANOMALY_GAP_DOWN not in {event.anomaly_type for event in result.events}
    assert "normal gap" in result.note
