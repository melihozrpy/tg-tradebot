from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app.analysis.multi_timeframe_engine import (
    TREND_DOWN,
    TREND_INSUFFICIENT,
    TREND_STRONG_UP,
    TREND_UP,
    analyze_multi_timeframe,
    compute_timeframe_snapshot,
)
from app.data.base_provider import DataUnavailableError
from app.data.mock_provider import MockMarketDataProvider


def _trending_df(periods: int = 120, drift: float = 0.01, freq: str = "1B") -> pd.DataFrame:
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=periods, freq=freq, tz="UTC")
    closes = [100.0 * (1 + drift) ** i for i in range(periods)]
    return pd.DataFrame({
        "timestamp": dates,
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [1_000_000.0] * periods,
    })


def _sideways_df(periods: int = 120, freq: str = "1B") -> pd.DataFrame:
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=periods, freq=freq, tz="UTC")
    rng = np.random.default_rng(5)
    closes = 100.0 + rng.normal(0, 0.3, size=periods)
    return pd.DataFrame({
        "timestamp": dates,
        "open": closes,
        "high": closes + 0.5,
        "low": closes - 0.5,
        "close": closes,
        "volume": [1_000_000.0] * periods,
    })


# ---------------------------------------------------------------------------
# compute_timeframe_snapshot
# ---------------------------------------------------------------------------


def test_snapshot_insufficient_data_returns_unavailable():
    snap = compute_timeframe_snapshot(pd.DataFrame(), "1d")
    assert snap.available is False
    assert snap.trend_class == TREND_INSUFFICIENT


def test_snapshot_detects_strong_uptrend():
    df = _trending_df(periods=120, drift=0.012)
    snap = compute_timeframe_snapshot(df, "1d")
    assert snap.available is True
    assert snap.trend_class in (TREND_STRONG_UP, TREND_UP)


def test_snapshot_detects_downtrend():
    df = _trending_df(periods=120, drift=-0.01)
    snap = compute_timeframe_snapshot(df, "1d")
    assert snap.available is True
    assert snap.trend_class == TREND_DOWN or "düşüş" in snap.trend_class.lower()


def test_snapshot_propagates_error_message():
    snap = compute_timeframe_snapshot(None, "5m", error="Gün içi veri alınamadı")
    assert snap.available is False
    assert snap.error == "Gün içi veri alınamadı"


# ---------------------------------------------------------------------------
# analyze_multi_timeframe (entegrasyon, mock provider ile)
# ---------------------------------------------------------------------------


def test_analyze_multi_timeframe_with_mock_provider_returns_all_timeframes():
    provider = MockMarketDataProvider()
    result = analyze_multi_timeframe(provider, "SVGYO")
    assert set(result.snapshots.keys()) == {"1wk", "1d", "1h", "15m", "5m"}
    assert 0 <= result.confluence_score <= 100


def test_analyze_multi_timeframe_missing_timeframe_does_not_break_others(monkeypatch):
    provider = MockMarketDataProvider()
    original_get_ohlcv = provider.get_ohlcv

    def flaky_get_ohlcv(symbol, timeframe, start, end):
        if timeframe == "5m":
            raise DataUnavailableError("5 dakikalik veri yok (simule edilmis).")
        return original_get_ohlcv(symbol, timeframe, start, end)

    monkeypatch.setattr(provider, "get_ohlcv", flaky_get_ohlcv)
    result = analyze_multi_timeframe(provider, "SVGYO")
    assert result.snapshots["5m"].available is False
    assert result.snapshots["1d"].available is True
    assert result.snapshots["1wk"].available is True


def test_big_trend_down_small_trend_up_flags_counter_trend():
    """Buyuk zaman dilimi (gunluk/haftalik) dususte, kucuk zaman dilimi (5dk)
    yukseliste ise 'karsi trend' uyarisi verilmeli ve guclu AL olusmamali."""

    snapshots = {
        "1wk": compute_timeframe_snapshot(_trending_df(120, drift=-0.01), "1wk"),
        "1d": compute_timeframe_snapshot(_trending_df(120, drift=-0.01), "1d"),
        "1h": compute_timeframe_snapshot(_sideways_df(120), "1h"),
        "15m": compute_timeframe_snapshot(_trending_df(120, drift=0.012), "15m"),
        "5m": compute_timeframe_snapshot(_trending_df(120, drift=0.012), "5m"),
    }

    big_tf_down = any(
        s.available and s.trend_class in ("Düşüş", "Güçlü düşüş") for tf, s in snapshots.items() if tf in ("1wk", "1d")
    )
    small_tf_up = any(
        s.available and s.trend_class in ("Yükseliş", "Güçlü yükseliş") for tf, s in snapshots.items() if tf in ("5m", "15m")
    )
    assert big_tf_down is True
    assert small_tf_up is True


def test_confluence_score_high_when_all_timeframes_agree():
    df = _trending_df(200, drift=0.01)
    snapshots = {tf: compute_timeframe_snapshot(df, tf) for tf in ("1wk", "1d", "1h", "15m", "5m")}
    from app.analysis.multi_timeframe_engine import _compute_confluence_score

    score = _compute_confluence_score(snapshots)
    assert score >= 60


def test_confluence_score_low_when_timeframes_conflict():
    up_df = _trending_df(200, drift=0.012)
    down_df = _trending_df(200, drift=-0.012)
    snapshots = {
        "1wk": compute_timeframe_snapshot(up_df, "1wk"),
        "1d": compute_timeframe_snapshot(down_df, "1d"),
        "1h": compute_timeframe_snapshot(up_df, "1h"),
        "15m": compute_timeframe_snapshot(down_df, "15m"),
        "5m": compute_timeframe_snapshot(up_df, "5m"),
    }
    from app.analysis.multi_timeframe_engine import _compute_confluence_score

    score = _compute_confluence_score(snapshots)
    assert score < 60
