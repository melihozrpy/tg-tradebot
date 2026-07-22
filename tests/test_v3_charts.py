from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from app.services.chart_service import (
    delete_chart_file,
    generate_price_chart,
    generate_relative_strength_chart,
    resolve_period_days,
    temporary_chart,
)


def test_price_chart_creates_file_and_is_deletable(mock_provider):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)
    df = mock_provider.get_ohlcv("THYAO", "1d", start, end)

    path = generate_price_chart(df, "THYAO")
    assert os.path.exists(path)
    assert path.endswith(".png")

    deleted = delete_chart_file(path)
    assert deleted is True
    assert not os.path.exists(path)


def test_delete_nonexistent_file_returns_false():
    assert delete_chart_file("/tmp/definitely_not_a_real_file_xyz.png") is False


def test_temporary_chart_context_manager_cleans_up(mock_provider):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)
    df = mock_provider.get_ohlcv("THYAO", "1d", start, end)
    path = generate_price_chart(df, "THYAO")

    with temporary_chart(path) as p:
        assert os.path.exists(p)

    assert not os.path.exists(path)  # blok bitince otomatik silindi


def test_relative_strength_chart_creates_file(mock_provider):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)
    stock_df = mock_provider.get_ohlcv("THYAO", "1d", start, end)
    index_df = mock_provider.get_ohlcv("XU100", "1d", start, end)

    path = generate_relative_strength_chart(stock_df, index_df, "THYAO", "XU100")
    try:
        assert os.path.exists(path)
    finally:
        delete_chart_file(path)


def test_resolve_period_days():
    assert resolve_period_days("6ay") == 180
    assert resolve_period_days("1yil") == 365
    assert resolve_period_days("bilinmeyen") == 180  # varsayilan
