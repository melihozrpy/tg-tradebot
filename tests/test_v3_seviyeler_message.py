from __future__ import annotations

import numpy as np
import pandas as pd

from app.analysis.confluence_zone_engine import find_confluence_zones
from app.analysis.timeframe_levels_engine import compute_timeframe_levels
from app.telegram.message_templates_v3 import format_seviyeler


def _oscillating_df(n_days: int = 400, floor: float = 90.0, ceiling: float = 110.0, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n_days, tz="UTC")
    mid = (floor + ceiling) / 2
    amplitude = (ceiling - floor) / 2
    closes = mid + amplitude * 0.85 * np.sin(np.linspace(0, 14 * np.pi, n_days)) + rng.normal(0, 0.3, n_days)
    closes = np.clip(closes, floor + 0.5, ceiling - 0.5)
    highs = closes + rng.uniform(0.2, 1.0, n_days)
    lows = closes - rng.uniform(0.2, 1.0, n_days)
    opens = closes + rng.normal(0, 0.3, n_days)
    volumes = rng.uniform(800_000, 1_200_000, n_days)
    return pd.DataFrame(
        {"timestamp": dates, "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes}
    )


def test_format_seviyeler_contains_all_timeframes_and_confluence():
    df = _oscillating_df()
    current_price = float(df["close"].iloc[-1])
    levels = compute_timeframe_levels(df, current_price)
    supports, resistances = find_confluence_zones(levels, current_price)

    text = format_seviyeler("THYAO", current_price, levels, supports, resistances)

    assert "MERGEN QUANT" in text
    assert "THYAO" in text
    assert "Günlük Seviyeler" in text
    assert "Haftalık Seviyeler" in text
    assert "Aylık Seviyeler" in text
    assert "Çakışan Güçlü Bölgeler" in text
    assert "yatırım tavsiyesi değildir" in text.lower()


def test_format_seviyeler_handles_unreliable_timeframe():
    tiny_df = pd.DataFrame(
        {
            "timestamp": pd.bdate_range(end=pd.Timestamp.today(), periods=5, tz="UTC"),
            "open": [10.0] * 5,
            "high": [10.5] * 5,
            "low": [9.5] * 5,
            "close": [10.0] * 5,
            "volume": [1000.0] * 5,
        }
    )
    levels = compute_timeframe_levels(tiny_df, current_price=10.0)
    supports, resistances = find_confluence_zones(levels, 10.0)
    text = format_seviyeler("TESTX", 10.0, levels, supports, resistances)
    assert "Guvenilir seviye hesaplanamadi" in text
    assert "tespit edilmedi" in text
