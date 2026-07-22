from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from app.analysis.relative_strength_engine import compute_relative_strength


def _df(dates, base=100.0, drift=0.001):
    closes = [base * (1 + drift) ** i for i in range(len(dates))]
    return pd.DataFrame({
        "timestamp": dates, "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1000.0] * len(dates),
    })


def test_relative_strength_requires_common_dates():
    dates_a = pd.date_range(end=datetime.now(timezone.utc), periods=70, freq="1B", tz="UTC")
    dates_b = pd.date_range(end=datetime.now(timezone.utc) - timedelta(days=200), periods=70, freq="1B", tz="UTC")
    stock_df = _df(dates_a)
    index_df = _df(dates_b)
    result = compute_relative_strength(stock_df, index_df)
    assert result.available is False
    assert "ortak" in result.note.lower() or "yetersiz" in result.note.lower()


def test_relative_strength_computes_with_aligned_dates():
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=70, freq="1B", tz="UTC")
    stock_df = _df(dates, base=100.0, drift=0.003)  # daha guclu yukselen
    index_df = _df(dates, base=100.0, drift=0.001)
    result = compute_relative_strength(stock_df, index_df)
    assert result.available is True
    assert result.relative_score is not None
    assert 0 <= result.relative_score <= 100
    assert result.classification is not None


def test_relative_strength_duplicate_dates_do_not_break_common_day_count():
    """Ayni gun icin tekrarli satirlar 'ortak islem gunu sayisi 0/yanlis' hatasina
    yol acmamali; dedup sonrasi hesaplama normal sekilde calismali."""
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=70, freq="1B", tz="UTC")
    stock_df = _df(dates, base=100.0, drift=0.003)
    index_df = _df(dates, base=100.0, drift=0.001)

    # Ilk gunu iki kez ekleyerek yinelenen tarih senaryosunu simule et.
    stock_df = pd.concat([stock_df.iloc[[0]], stock_df], ignore_index=True)
    index_df = pd.concat([index_df.iloc[[0]], index_df], ignore_index=True)

    result = compute_relative_strength(stock_df, index_df)
    assert result.available is True
    assert result.relative_score is not None
    assert 0 <= result.relative_score <= 100


def test_relative_strength_stock_outperforms_gets_higher_score():
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=70, freq="1B", tz="UTC")
    strong_stock = _df(dates, base=100.0, drift=0.01)
    weak_stock = _df(dates, base=100.0, drift=-0.005)
    index_df = _df(dates, base=100.0, drift=0.001)

    strong_result = compute_relative_strength(strong_stock, index_df)
    weak_result = compute_relative_strength(weak_stock, index_df)
    assert strong_result.relative_score > weak_result.relative_score


def test_relative_strength_empty_data_unavailable():
    empty = pd.DataFrame(columns=["timestamp", "close"])
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=70, freq="1B", tz="UTC")
    result = compute_relative_strength(empty, _df(dates))
    assert result.available is False
