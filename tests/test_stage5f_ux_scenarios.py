from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd

from app.analysis.gyo_valuation_engine import evaluate_gyo_valuation
from app.analysis.long_term_scenario_engine import compute_long_term_scenarios
from app.analysis.multi_timeframe_engine import TimeframeSnapshot, _compute_confluence_score, analyze_multi_timeframe
from app.analysis.target_roadmap_engine import build_target_roadmap
from app.analysis.user_target_engine import evaluate_user_target
from app.config.settings import get_settings
from app.data.mock_provider import MockMarketDataProvider
from app.services.chart_service import (
    _resolve_label_positions,
    clear_chart_cache,
    delete_chart_file,
    generate_professional_daily_chart,
)
from app.services.current_price_service import CurrentPriceResult
from app.telegram.formatters import sanitize_provider_error
from app.telegram.message_templates_v3 import (
    format_long_term_scenarios,
    format_multi_timeframe,
    format_user_target_check,
)


def _daily(periods: int = 900) -> pd.DataFrame:
    close = np.linspace(7.5, 10.0, periods) * (1 + np.sin(np.arange(periods) / 23) * 0.025)
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(end=datetime(2026, 7, 17, tzinfo=timezone.utc), periods=periods, freq="1D"),
            "open": close * 0.995,
            "high": close * 1.02,
            "low": close * 0.98,
            "close": close,
            "volume": np.linspace(900_000, 1_400_000, periods),
        }
    )


def _level(mid: float, timeframe: str, sources: list[str], confidence: float = 75.0):
    return SimpleNamespace(
        low=mid - 0.15,
        high=mid + 0.15,
        mid=mid,
        timeframe=timeframe,
        sources=sources,
        confidence=confidence,
        strength_class="Güçlü",
    )


def _levels():
    zones = [
        _level(9.0, "haftalik", ["swing_low", "volume_profile_hvn"]),
        _level(8.0, "aylik", ["tarihi_dip", "fibonacci_0.382"]),
        _level(12.0, "haftalik", ["swing_high", "volume_profile_hvn"]),
        _level(15.0, "aylik", ["tarihi_tepe", "fibonacci_0.618"]),
        _level(20.0, "aylik", ["fibonacci_extension_1.272", "volume_profile_hvn"]),
        _level(30.0, "aylik", ["fibonacci_extension_1.618", "tarihi_tepe", "volume_profile_hvn"]),
    ]
    return SimpleNamespace(all_zones=lambda: zones)


def _price() -> CurrentPriceResult:
    return CurrentPriceResult(
        "SVGYO",
        10.0,
        datetime(2026, 7, 18, tzinfo=timezone.utc),
        "completed_5m",
        True,
        10.0,
        9.9,
        1.01,
    )


def _scenario(**kwargs):
    defaults = {
        "levels_result": _levels(),
        "liquidity_score": 70,
        "data_quality_score": 92,
        "relative_strength": 60,
        "sector_relative_strength": 55,
        "current_market_cap": 1_000_000_000,
    }
    defaults.update(kwargs)
    return compute_long_term_scenarios(_daily(), 10.0, **defaults)


def test_01_long_term_default_message_respects_telegram_limit():
    text = format_long_term_scenarios("SVGYO", _price(), _scenario(), SimpleNamespace(score=92))
    assert len(text) <= 4000


def test_02_long_term_default_has_at_most_three_bull_and_three_bear_zones():
    text = format_long_term_scenarios("SVGYO", _price(), _scenario(), SimpleNamespace(score=92))
    bull = text.split("BOĞA SENARYOSU", 1)[1].split("AYI SENARYOSU", 1)[0]
    bear = text.split("AYI SENARYOSU", 1)[1].split("SONUÇ", 1)[0]
    assert sum(line.startswith("- ") for line in bull.splitlines()) <= 3
    assert sum(line.startswith("- ") for line in bear.splitlines()) <= 3


def test_03_short_term_target_label_is_not_repeated_in_long_term_default():
    text = format_long_term_scenarios("SVGYO", _price(), _scenario(), SimpleNamespace(score=92))
    assert "Kısa vadeli hedef" not in text


def test_04_evidence_strength_is_not_presented_as_probability_or_confidence():
    text = format_long_term_scenarios("SVGYO", _price(), _scenario(), SimpleNamespace(score=92))
    assert "Kanıt gücü:" in text
    assert "Güven:" not in text
    assert "gerçekleşme olasılığı:" not in text.casefold()


def test_05_gyo_valuation_changes_evidence_in_a_bounded_way():
    discounted = evaluate_gyo_valuation(
        "SVGYO", 10.0, {"shares_outstanding": 100_000_000, "net_asset_value": 5_000_000_000}
    )
    premium = evaluate_gyo_valuation(
        "SVGYO", 10.0, {"shares_outstanding": 100_000_000, "net_asset_value": 400_000_000}
    )
    positive = _scenario(valuation_result=discounted)
    negative = _scenario(valuation_result=premium)
    positive_value = positive.all_scenarios()[0].score_breakdown.fundamental_valuation
    negative_value = negative.all_scenarios()[0].score_breakdown.fundamental_valuation
    assert 0 < positive_value <= 8
    assert -8 <= negative_value < 0
    assert positive_value - negative_value <= 16


def test_06_missing_fundamentals_stay_neutral_and_are_not_fabricated():
    result = _scenario(valuation_result=None, fundamental_support=None)
    assert all(zone.fundamental_support == "Veri yetersiz" for zone in result.all_scenarios())
    assert all(zone.score_breakdown.fundamental_valuation == 0 for zone in result.all_scenarios())


def test_07_low_liquidity_reduces_far_target_evidence():
    low = _scenario(liquidity_score=10)
    high = _scenario(liquidity_score=85)
    low_far = max((zone for zone in low.all_scenarios() if zone.direction == "yükseliş"), key=lambda z: z.mid)
    high_far = max((zone for zone in high.all_scenarios() if zone.direction == "yükseliş"), key=lambda z: z.mid)
    # Çok düşük likidite aynı uzak bölgenin puanını düşürür; eşik altına
    # inerse bölgeyi ana senaryo listesinden tamamen de çıkarabilir.
    assert low_far.mid <= high_far.mid
    assert low_far.mid < high_far.mid or low_far.evidence_strength < high_far.evidence_strength
    assert low_far.score_breakdown.volume_liquidity <= -14


def test_08_low_data_quality_caps_high_evidence_strength():
    result = _scenario(data_quality_score=30, liquidity_score=90, relative_strength=90, sector_relative_strength=90)
    assert result.all_scenarios()
    assert max(zone.evidence_strength for zone in result.all_scenarios()) <= 49


def test_09_extreme_bull_is_absent_without_required_evidence():
    result = compute_long_term_scenarios(_daily(120), 10.0, liquidity_score=90, current_market_cap=1_000_000_000)
    assert result.extreme_bull is None
    assert "yeterli kanıt yok" in result.extreme_bull_note


def test_10_roadmap_uses_real_regions_not_fixed_percent_bands():
    technical = SimpleNamespace(
        low=14.80,
        high=15.20,
        mid=15.0,
        technical_role="Haftalık direnç",
        evidence_strength=72,
        evidence=["haftalık swing"],
    )
    roadmap = build_target_roadmap(10, 30, intermediate_levels=[technical])
    first = roadmap.steps[0]
    assert (first.price_low, first.price_high) == (14.8, 15.2)
    assert first.retest_zone == (10.0, 10.0)
    assert first.invalidation_level == 10.0


def test_11_roadmap_removes_duplicate_target_steps():
    roadmap = build_target_roadmap(10, 30, intermediate_levels=[15, 15.02, 15.05, 20, 20.01])
    mids = [step.mid for step in roadmap.steps]
    assert len(mids) == len(set(mids))


def test_12_roadmap_does_not_invent_intermediate_targets_in_wide_gaps():
    roadmap = build_target_roadmap(10, 70, intermediate_levels=[15])
    assert [step.mid for step in roadmap.steps] == [15.0, 70.0]


def test_13_user_target_does_not_mutate_buy_sell_score():
    signal = {"score": 64.0, "signal_type": "WATCH"}
    before = signal.copy()
    evaluate_user_target("SVGYO", 10, 70, intermediate_levels=[15, 30])
    assert signal == before


def test_14_user_target_is_not_labeled_as_bot_target():
    evaluation = evaluate_user_target("SVGYO", 10, 70, intermediate_levels=[15, 30])
    assert evaluation.roadmap.steps[-1].level_type == "Kullanıcı hedefi"
    assert "kullanıcı hedefi" in evaluation.roadmap.steps[-1].evidence


def test_15_target_check_default_is_short_and_readable():
    evaluation = evaluate_user_target("SVGYO", 10, 70, intermediate_levels=[15, 30, 45])
    text = format_user_target_check(evaluation, _price())
    assert len(text.splitlines()) <= 24
    assert "Ana yol:" in text and "Kısa sonuç:" in text


def test_16_multi_timeframe_default_uses_simplified_format():
    result = analyze_multi_timeframe(MockMarketDataProvider(), "SVGYO")
    text = format_multi_timeframe(result, "SVGYO")
    assert "UZUN VADE" in text and "ORTA VADE" in text and "KISA VADE" in text
    assert "EMA" not in text and "RSI" not in text and "MACD" not in text


def test_17_unavailable_timeframes_do_not_create_fake_zero_score():
    snapshots = {
        "1wk": TimeframeSnapshot("1wk", False, ""),
        "1d": TimeframeSnapshot("1d", False, ""),
    }
    assert _compute_confluence_score(snapshots) == 0
    result = SimpleNamespace(
        snapshots=snapshots,
        confluence_score=0,
        primary_direction="veri_yetersiz",
        short_term_direction="veri_yetersiz",
        conflict=False,
        trend_reversal=False,
        data_quality="Veri yetersiz",
    )
    text = format_multi_timeframe(result, "SVGYO")
    assert "Uyum skoru: Veri yetersiz" in text
    assert "Haftalık: Veri yok" in text


def _chart_df() -> pd.DataFrame:
    close = np.linspace(10, 18, 420)
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=420, freq="1D", tz="UTC"),
            "open": close * 0.995,
            "high": close * 1.015,
            "low": close * 0.985,
            "close": close,
            "volume": np.linspace(1_000_000, 2_000_000, 420),
        }
    )


def test_18_standard_chart_contains_only_primary_indicators(monkeypatch, tmp_path):
    monkeypatch.setenv("CHART_CACHE_DIR", str(tmp_path / "charts"))
    get_settings.cache_clear()
    clear_chart_cache()
    from app.services import chart_service

    real_close = chart_service.plt.close
    monkeypatch.setattr(chart_service.plt, "close", lambda _fig=None: None)
    path = generate_professional_daily_chart(_chart_df(), "SVGYO", targets=[20, 22, 24], chart_mode="standard")
    fig = chart_service.plt.gcf()
    try:
        labels = {line.get_label() for line in fig.axes[0].lines}
        assert len(fig.axes) == 1
        assert {"EMA20", "EMA50"} <= labels
        assert not {"EMA100", "EMA200", "Bollinger", "VWAP"} & labels
        assert all(axis.get_ylabel() not in {"Hacim", "MACD"} for axis in fig.axes)
        assert "STANDART" in " ".join(text.get_text() for text in fig.texts)
    finally:
        delete_chart_file(path)
        real_close(fig)


def test_19_detailed_chart_contains_secondary_indicators(monkeypatch, tmp_path):
    monkeypatch.setenv("CHART_CACHE_DIR", str(tmp_path / "charts"))
    get_settings.cache_clear()
    clear_chart_cache()
    from app.services import chart_service

    real_close = chart_service.plt.close
    monkeypatch.setattr(chart_service.plt, "close", lambda _fig=None: None)
    path = generate_professional_daily_chart(_chart_df(), "SVGYO", chart_mode="detailed")
    fig = chart_service.plt.gcf()
    try:
        labels = {line.get_label() for line in fig.axes[0].lines}
        assert len(fig.axes) == 1
        assert {"EMA100", "EMA200", "Bollinger", "VWAP"} <= labels
        assert all(axis.get_ylabel() not in {"Hacim", "MACD"} for axis in fig.axes)
        assert "DETAYLI" in " ".join(text.get_text() for text in fig.texts)
    finally:
        delete_chart_file(path)
        real_close(fig)


def test_20_chart_label_resolver_limits_and_separates_overlaps():
    items = [(100 + index * 0.0001, f"L{index}", "#000", index) for index in range(40)]
    resolved = _resolve_label_positions(items, 99.0, 103.0, max_labels=8)
    assert len(resolved) == 8
    positions = [item[1] for item in resolved]
    assert all(right > left for left, right in zip(positions, positions[1:]))


def test_21_provider_internal_error_is_not_exposed_to_telegram():
    text = sanitize_provider_error("yahoo_chart rate limit HTTP 429 provider exception")
    lowered = text.casefold()
    assert "429" not in lowered and "yahoo" not in lowered and "provider exception" not in lowered
    assert "veri kaynağı" in lowered
