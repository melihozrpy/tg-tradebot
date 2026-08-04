from __future__ import annotations

import numpy as np
import pandas as pd

from app.analysis.indicator_engine import (
    compute_indicator_bundle,
    evaluate_indicator_confluence,
)
from app.analysis.quality_zone_engine import QualityZoneScenario
from app.analysis.staged_entry import build_staged_entry_plan, evaluate_staged_entry
from app.modules.scenario_chart import generate_scenario_chart


def _frame(count: int = 260) -> pd.DataFrame:
    index = np.arange(count, dtype=float)
    close = 100.0 + index * 0.18 + np.sin(index / 5.0)
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=count, freq="D", tz="UTC"),
            "open": close - 0.25,
            "high": close + 0.8,
            "low": close - 0.9,
            "close": close,
            "volume": 1_000_000 + index * 2_000,
        }
    )


def _scenario(current_price: float = 112.0) -> QualityZoneScenario:
    return QualityZoneScenario(
        zone_kind="OB",
        zone_low=104.0,
        zone_high=106.0,
        direction="LONG",
        location="altinda",
        distance_points=6.0,
        distance_percent=5.35,
        current_price=current_price,
        entry=106.0,
        entry_reason="bullish Order Block'un ust siniri ilk retest seviyesidir",
        invalidation=103.5,
        target_1=111.0,
        target_1_label="yapisal direnc",
        target_2=116.0,
        target_2_label="ust likidite",
        rr_1=2.0,
        rr_2=4.0,
        quality_score=82,
        structure_kind="MSS",
        structure_direction="bullish",
        structure_confirmed=True,
        rr_is_sufficient=True,
    )


def test_shared_indicator_bundle_contains_all_requested_series() -> None:
    bundle = compute_indicator_bundle(_frame(), symbol="THYAO", timeframe="1d")

    expected = {
        "vwap",
        "anchored_vwap",
        "ema20",
        "ema50",
        "ema100",
        "ema200",
        "supertrend",
        "supertrend_direction",
        "rsi14",
        "macd",
        "macd_signal",
        "macd_histogram",
        "adx14",
        "bb_upper",
        "bb_mid",
        "bb_lower",
        "obv",
    }
    assert expected.issubset(bundle.frame.columns)
    assert bundle.volume_profile.poc is not None
    assert bundle.volume_profile.val <= bundle.volume_profile.poc <= bundle.volume_profile.vah


def test_confluence_never_qualifies_with_less_than_three_confirmations() -> None:
    bundle = compute_indicator_bundle(_frame(), symbol="THYAO", timeframe="1d")
    result = evaluate_indicator_confluence(bundle, "bullish", minimum_required=3)
    assert result.minimum_required == 3
    assert result.qualified is (len(result.confirmations) >= 3)


def test_staged_entry_uses_zone_levels_and_never_current_close() -> None:
    confluence = evaluate_indicator_confluence(
        compute_indicator_bundle(_frame(), symbol="THYAO", timeframe="1d"),
        "bullish",
        minimum_required=3,
    )
    plan = build_staged_entry_plan(_scenario(), symbol="THYAO", confluence=confluence)

    assert [level.allocation_percent for level in plan.levels] == [40.0, 35.0, 25.0]
    assert [level.price for level in plan.levels] == [106.0, 105.0, 104.0]
    assert all(level.price != plan.current_price for level in plan.levels)
    assert plan.status == "PENDING"


def test_hard_close_beyond_invalidation_cancels_remaining_stages() -> None:
    plan = build_staged_entry_plan(_scenario(current_price=105.0), symbol="THYAO")
    updated = evaluate_staged_entry(plan, candle_low=102.9, candle_high=106.1, candle_close=103.2)

    assert updated.status == "INVALIDATED"
    assert updated.cancelled_reason is not None
    assert not any(level.filled for level in updated.levels)


def test_clean_pending_scenario_chart_is_rendered(tmp_path) -> None:
    confluence = evaluate_indicator_confluence(
        compute_indicator_bundle(_frame(), symbol="THYAO", timeframe="1d"),
        "bullish",
        minimum_required=3,
    )
    plan = build_staged_entry_plan(_scenario(), symbol="THYAO", confluence=confluence)
    output = generate_scenario_chart(
        _frame(), symbol="THYAO", plan=plan, output_dir=tmp_path, dpi=100
    )
    image = tmp_path / output.split("\\")[-1].split("/")[-1]
    assert image.exists()
    assert image.stat().st_size > 10_000
