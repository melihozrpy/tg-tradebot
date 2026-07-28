from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import matplotlib.pyplot as plt
import numpy as np

from app.config.settings import get_settings
from app.services import chart_service
from app.services.chart_service import (
    THEMES,
    _draw_candles,
    _resolve_label_positions,
    chart_cache_stats,
    clear_chart_cache,
    delete_chart_file,
    generate_intraday_chart,
    generate_professional_daily_chart,
)


def _daily(mock_provider, days: int = 420):
    end = datetime.now(timezone.utc)
    return mock_provider.get_ohlcv("THYAO", "1d", end - timedelta(days=days), end)


def _intraday(mock_provider):
    end = datetime.now(timezone.utc)
    return mock_provider.get_ohlcv("THYAO", "15m", end - timedelta(days=5), end)


def _isolated_chart_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("CHART_CACHE_DIR", str(tmp_path / "charts"))
    monkeypatch.setenv("CHART_CACHE_TTL_MINUTES", "30")
    get_settings.cache_clear()
    clear_chart_cache()


def test_real_candles_have_body_wick_and_compact_trading_axis(mock_provider):
    df = _daily(mock_provider, 30).tail(12).reset_index(drop=True)
    fig, ax = plt.subplots()
    try:
        x = _draw_candles(ax, df, THEMES["light"])
        assert len(ax.patches) == len(df)  # Open-Close gövdeleri
        assert len(ax.collections) >= 1  # High-Low fitilleri (LineCollection)
        assert np.array_equal(x, np.arange(len(df), dtype=float))
        assert all(patch.get_height() > 0 for patch in ax.patches)  # doji de görünür
    finally:
        plt.close(fig)


def test_professional_chart_has_vivid_indicators_zones_and_no_volume_panel(
    mock_provider, monkeypatch, tmp_path
):
    _isolated_chart_cache(monkeypatch, tmp_path)
    df = _daily(mock_provider)
    real_close = chart_service.plt.close
    monkeypatch.setattr(chart_service.plt, "close", lambda _fig=None: None)
    path = generate_professional_daily_chart(
        df,
        "THYAO",
        entry_zone=(9.8, 10.1),
        entry_trigger=10.2,
        stop_price=9.4,
        targets=[10.8, 11.2, 11.8],
        info_box={"Veri kalitesi": "HEALTHY (96/100)"},
    )
    fig = chart_service.plt.gcf()
    try:
        assert os.path.exists(path)
        assert len(fig.axes) == 1
        price = fig.axes[0]
        price_labels = {line.get_label() for line in price.lines}
        assert {"EMA20", "EMA50", "EMA100", "EMA200", "Bollinger", "VWAP"} <= price_labels
        assert len(price.patches) >= 120  # son 120 mum + fiyat zoneları
        assert all(axis.get_ylabel() != "Hacim" for axis in fig.axes)
        figure_text = " ".join(text.get_text() for text in fig.texts)
        assert "THYAO" in figure_text
        assert "DETAYLI" in figure_text
        assert "TEKNİK CHECKLIST" in figure_text
        assert "MONTANA MELİH" in figure_text
    finally:
        delete_chart_file(path)
        real_close(fig)


def test_label_collision_resolver_limits_and_separates_labels():
    items = [(100 + index * 0.001, f"L{index}", "#000", index) for index in range(30)]
    resolved = _resolve_label_positions(items, 99.0, 103.0, max_labels=8)
    assert len(resolved) == 8
    display_positions = [item[1] for item in resolved]
    assert all(right > left for left, right in zip(display_positions, display_positions[1:]))


def test_chart_cache_hit_and_data_change_invalidation(mock_provider, monkeypatch, tmp_path):
    _isolated_chart_cache(monkeypatch, tmp_path)
    df = _daily(mock_provider, 100)
    first = generate_professional_daily_chart(df, "THYAO")
    first_stats = chart_cache_stats()
    delete_chart_file(first)

    second = generate_professional_daily_chart(df.copy(), "THYAO")
    second_stats = chart_cache_stats()
    delete_chart_file(second)
    assert second_stats["hits"] == first_stats["hits"] + 1
    assert second_stats["entries"] == 1

    changed = df.copy()
    changed.loc[changed.index[-1], "close"] += 0.01
    third = generate_professional_daily_chart(changed, "THYAO")
    third_stats = chart_cache_stats()
    try:
        assert third_stats["misses"] == second_stats["misses"] + 1
        assert third_stats["entries"] == 2
    finally:
        delete_chart_file(third)


def test_intraday_chart_has_vivid_candles_and_optional_context_without_volume(
    mock_provider, monkeypatch, tmp_path
):
    _isolated_chart_cache(monkeypatch, tmp_path)
    df = _intraday(mock_provider)
    real_close = chart_service.plt.close
    monkeypatch.setattr(chart_service.plt, "close", lambda _fig=None: None)
    path = generate_intraday_chart(
        df,
        "THYAO",
        daily_support=9.5,
        daily_resistance=12.5,
        previous_close=10.4,
        active_alarm_points=[(df.iloc[-2]["timestamp"], float(df.iloc[-2]["close"]))],
        info_box={"Sağlayıcı": "mock"},
    )
    fig = chart_service.plt.gcf()
    try:
        assert os.path.exists(path)
        assert len(fig.axes) == 1
        price = fig.axes[0]
        assert len(price.patches) >= 140  # okunabilirlik için son 140 bar
        assert {"VWAP", "EMA20", "EMA50"} <= {line.get_label() for line in price.lines}
        assert all(axis.get_ylabel() != "Hacim" for axis in fig.axes)
        figure_text = " ".join(text.get_text() for text in fig.texts)
        assert "GÜN İÇİ" in figure_text
        assert "GÜN İÇİ CHECKLIST" in figure_text
    finally:
        delete_chart_file(path)
        real_close(fig)


def test_delete_chart_file_removes_cached_copy(mock_provider, monkeypatch, tmp_path):
    _isolated_chart_cache(monkeypatch, tmp_path)
    path = generate_intraday_chart(_intraday(mock_provider), "THYAO")
    assert delete_chart_file(path) is True
    assert not os.path.exists(path)
