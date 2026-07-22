from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from app.analysis.corporate_actions_engine import apply_price_adjustments, normalize_corporate_actions
from app.analysis.target_roadmap_engine import build_target_roadmap
from app.services.chart_service import delete_chart_file, generate_long_term_chart


def _daily(periods=900):
    dates = pd.date_range(end=datetime(2026, 7, 17, tzinfo=timezone.utc), periods=periods, freq="1D")
    close = np.linspace(5, 30, periods) * (1 + np.sin(np.arange(periods) / 20) * 0.04)
    return pd.DataFrame(
        {
            "timestamp": dates, "open": close * 0.995, "high": close * 1.02,
            "low": close * 0.98, "close": close, "volume": np.linspace(1e6, 2e6, periods),
        }
    )


def test_weekly_logarithmic_chart_is_generated():
    path = generate_long_term_chart(_daily(), "SVGYO", timeframe="weekly", current_price=30)
    try:
        assert Path(path).exists() and Path(path).stat().st_size > 1000
    finally:
        delete_chart_file(path)


def test_monthly_logarithmic_chart_is_generated():
    path = generate_long_term_chart(_daily(), "SVGYO", timeframe="monthly", current_price=30)
    try:
        assert Path(path).exists() and Path(path).stat().st_size > 1000
    finally:
        delete_chart_file(path)


def test_user_target_and_roadmap_can_be_drawn():
    roadmap = build_target_roadmap(30, 70, intermediate_levels=[35, 45, 55])
    path = generate_long_term_chart(
        _daily(), "SVGYO", timeframe="weekly", current_price=30,
        user_target=70, roadmap=roadmap, speculation_risk="Yüksek",
    )
    try:
        assert Path(path).exists()
    finally:
        delete_chart_file(path)


def test_split_adjusted_series_remains_chartable():
    df = _daily(400)
    split_date = pd.Timestamp(df.iloc[-100]["timestamp"]).date()
    events = normalize_corporate_actions("SVGYO", [{"type": "split", "date": split_date, "ratio": "2:1"}])
    adjusted = apply_price_adjustments(df, events, mode="adjusted")
    path = generate_long_term_chart(
        adjusted, "SVGYO", timeframe="weekly", current_price=float(adjusted.iloc[-1]["close"]),
        corporate_actions=events,
    )
    try:
        assert Path(path).exists()
    finally:
        delete_chart_file(path)
