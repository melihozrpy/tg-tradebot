from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app.analysis.relative_strength_periods_engine import (
    BENCHMARK_SECTOR,
    BENCHMARK_XU100,
    PERIOD_1AY,
    PERIOD_1HAFTA,
    PERIOD_3AY,
    PERIOD_6AY,
    compute_relative_strength_periods,
)


def _df(dates, base=100.0, drift=0.001):
    closes = [base * (1 + drift) ** i for i in range(len(dates))]
    return pd.DataFrame({
        "timestamp": dates, "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1000.0] * len(dates),
    })


def test_periods_insufficient_data_marks_each_period_unavailable():
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=10, freq="1B", tz="UTC")
    stock_df = _df(dates)
    index_df = _df(dates)
    result = compute_relative_strength_periods(stock_df, index_df, "TEST", BENCHMARK_XU100, "XU100.IS")
    for period_name in (PERIOD_1HAFTA, PERIOD_1AY, PERIOD_3AY, PERIOD_6AY):
        p = result.periods[period_name]
        # 5 gunluk periyot 10 barla hesaplanabilir olmali, digerleri yetersiz.
        if period_name == PERIOD_1HAFTA:
            assert p.available is True
        else:
            assert p.available is False
            assert "yetersiz" in p.note.lower()


def test_periods_computes_all_four_with_enough_history():
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=150, freq="1B", tz="UTC")
    stock_df = _df(dates, base=100.0, drift=0.004)  # daha guclu
    index_df = _df(dates, base=100.0, drift=0.001)
    result = compute_relative_strength_periods(stock_df, index_df, "TEST", BENCHMARK_XU100, "XU100.IS")
    for period_name in (PERIOD_1HAFTA, PERIOD_1AY, PERIOD_3AY, PERIOD_6AY):
        p = result.periods[period_name]
        assert p.available is True
        assert 0 <= p.strength_score <= 100
        assert p.diff_pct > 0  # hisse endeksten daha guclu yukseliyor
        assert p.classification is not None
    assert result.overall_trend in ("guclenen", "zayiflayan", "yatay")


def test_periods_no_fake_score_when_no_benchmark_data():
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=150, freq="1B", tz="UTC")
    stock_df = _df(dates)
    result = compute_relative_strength_periods(stock_df, None, "TEST", BENCHMARK_SECTOR, "")
    for p in result.periods.values():
        assert p.available is False
        assert p.strength_score is None
