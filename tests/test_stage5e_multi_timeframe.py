from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app.analysis.multi_timeframe_engine import (
    STAGE5E_TIMEFRAMES,
    TREND_STRONG_DOWN,
    TREND_STRONG_UP,
    TimeframeSnapshot,
    _compute_confluence_score,
    analyze_multi_timeframe,
    resample_completed_4h,
)
from app.data.mock_provider import MockMarketDataProvider
from app.telegram.message_templates_v3 import format_multi_timeframe


def _session_hours(day: str, closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range(f"{day} 07:00:00+00:00", periods=len(closes), freq="1h")
    return pd.DataFrame(
        {
            "timestamp": idx,
            "open": [x - 0.1 for x in closes],
            "high": [x + 0.3 for x in closes],
            "low": [x - 0.3 for x in closes],
            "close": closes,
            "volume": [100, 200, 300, 400, 500, 600, 700, 800][: len(closes)],
        }
    )


def test_four_hour_resample_applies_ohlcv_rules():
    df = _session_hours("2026-07-17", [10, 11, 12, 13, 14, 15, 16, 17])
    result = resample_completed_4h(df, now=datetime(2026, 7, 17, 16, tzinfo=timezone.utc))
    assert len(result) == 2
    first = result.iloc[0]
    assert first["open"] == 9.9
    assert first["high"] == 13.3
    assert first["low"] == 9.7
    assert first["close"] == 13
    assert first["volume"] == 1000


def test_incomplete_four_hour_bucket_is_excluded():
    df = _session_hours("2026-07-17", [10, 11, 12, 13, 14, 15])
    result = resample_completed_4h(df, now=datetime(2026, 7, 17, 13, 30, tzinfo=timezone.utc))
    assert len(result) == 1
    assert result.iloc[0]["close"] == 13


def test_stage5e_analysis_contains_all_six_timeframes():
    result = analyze_multi_timeframe(MockMarketDataProvider(), "SVGYO", STAGE5E_TIMEFRAMES)
    assert set(result.snapshots) == set(STAGE5E_TIMEFRAMES)


def test_high_timeframe_weights_dominate_small_timeframes():
    snapshots = {
        "1wk": TimeframeSnapshot("1wk", True, "", TREND_STRONG_DOWN, trend_strength=80),
        "1d": TimeframeSnapshot("1d", True, "", TREND_STRONG_DOWN, trend_strength=80),
        "4h": TimeframeSnapshot("4h", True, "", TREND_STRONG_DOWN, trend_strength=70),
        "1h": TimeframeSnapshot("1h", True, "", TREND_STRONG_DOWN, trend_strength=65),
        "15m": TimeframeSnapshot("15m", True, "", TREND_STRONG_UP, trend_strength=90),
        "5m": TimeframeSnapshot("5m", True, "", TREND_STRONG_UP, trend_strength=90),
    }
    down_score = _compute_confluence_score(snapshots)
    assert down_score >= 55


def test_missing_timeframe_does_not_create_fake_score():
    snapshots = {
        "1wk": TimeframeSnapshot("1wk", False, ""),
        "1d": TimeframeSnapshot("1d", False, ""),
    }
    assert _compute_confluence_score(snapshots) == 0


def test_stage5e_multi_timeframe_message_is_readable():
    result = analyze_multi_timeframe(MockMarketDataProvider(), "SVGYO", STAGE5E_TIMEFRAMES)
    text = format_multi_timeframe(result, "SVGYO")
    assert "UZUN VADE" in text
    assert "4 Saatlik" in text
    assert "Uyum skoru" in text
    assert "Veri kalitesi" in text
