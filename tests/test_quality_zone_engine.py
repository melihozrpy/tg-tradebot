from __future__ import annotations

import pandas as pd
import pytest
from PIL import Image

from app.analysis.breakout_scenario_engine import compute_breakout_scenarios
from app.analysis.quality_zone_engine import format_quality_zone_scenario, select_closest_quality_zone
from app.analysis.smart_money_engine import PriceZone, SmartMoneyResult, StructureEvent
from app.modules.scenario_chart import render_breakout_scenario_chart
from app.services.enhanced_alert_service import AlarmEvaluationContext, _next_breakout_target


def _smart() -> SmartMoneyResult:
    return SmartMoneyResult(
        fvg=(
            PriceZone("FVG", 101.20, 102.10, 70, "bearish", 70, 68),
            PriceZone("FVG", 94.10, 95.20, 75, "bullish", 75, 73),
        ),
        order_blocks=(
            PriceZone("OB", 96.40, 97.20, 78, "bullish", 78, 76),
            PriceZone("OB", 103.40, 104.25, 79, "bearish", 79, 77),
        ),
        structure=(StructureEvent("MSS", 99.40, 80, "bullish", 80, 77),),
    )


def _frame() -> pd.DataFrame:
    close = [96.0 + index * 0.08 + ((index % 5) - 2) * 0.12 for index in range(80)]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=80, freq="B", tz="UTC"),
            "open": [value - 0.10 for value in close],
            "high": [value + 0.45 for value in close],
            "low": [value - 0.40 for value in close],
            "close": close,
            "volume": [1_000_000] * 80,
        }
    )


def test_closest_quality_zone_uses_mss_and_exact_zone_prices():
    scenario = select_closest_quality_zone(
        99.00,
        1.50,
        _smart(),
        support_levels=(92.80, 90.10),
        resistance_levels=(101.20, 103.40, 107.60),
    )
    assert scenario is not None
    assert scenario.direction == "LONG"
    assert scenario.zone_kind == "OB"
    assert scenario.zone_low == 96.40
    assert scenario.zone_high == 97.20
    assert scenario.entry == pytest.approx(96.80)
    assert scenario.structure_kind == "MSS"
    assert scenario.structure_confirmed is True
    text = format_quality_zone_scenario(scenario)
    assert "EN YAKIN KALİTELİ BÖLGE" in text
    assert "96.40-97.20" in text
    assert "Güncel fiyattan doğrudan giriş önerilmez" in text


def test_dynamic_breakout_targets_use_next_pd_array_not_atr_projection():
    class Zone:
        low = 99.0
        high = 100.0
        mid = 99.5
        confidence = 80.0

    result = compute_breakout_scenarios(
        resistance_zone=Zone(),
        support_zone=None,
        current_price=100.5,
        atr_value=4.0,
        pd_array_levels=((101.35, "bearish FVG bölgesi"), (103.80, "üst swing likiditesi")),
    )
    case = result.resistance_breakout
    assert case is not None
    assert case.level_already_broken is True
    assert case.target_1 == 101.35
    assert case.target_1_reason == "bearish FVG bölgesi"
    assert case.target_2 == 103.80


def test_breakout_scenario_chart_renders_two_real_candle_panels(tmp_path):
    class Resistance:
        low = 101.0
        high = 102.0
        mid = 101.5
        confidence = 80.0

    class Support:
        low = 97.0
        high = 98.0
        mid = 97.5
        confidence = 80.0

    result = compute_breakout_scenarios(
        resistance_zone=Resistance(),
        support_zone=Support(),
        current_price=100.0,
        atr_value=1.2,
        pd_array_levels=((104.0, "üst OB"), (106.0, "üst swing"), (95.0, "alt FVG"), (93.0, "alt swing")),
    )
    output = render_breakout_scenario_chart(
        _frame(), symbol="THYAO", result=result, output_dir=tmp_path, dpi=100
    )
    with Image.open(output) as image:
        assert image.width > 1000
        assert image.height > 500


def test_automatic_alarm_breakout_target_uses_next_timeframe_zone():
    class Levels:
        @staticmethod
        def all_zones():
            return [
                type("Level", (), {"mid": 102.50, "timeframe": "gunluk"})(),
                type("Level", (), {"mid": 94.00, "timeframe": "haftalik"})(),
            ]

    frame = _frame().copy()
    for column in ("open", "high", "low", "close"):
        frame[column] = frame[column] - 10.0
    context = AlarmEvaluationContext(symbol="THYAO", timeframe="gunluk", df=frame, levels=Levels())
    target, reason = _next_breakout_target(context, frame, boundary=100.0, direction="up")
    assert target == 102.50
    assert "gunluk" in reason
