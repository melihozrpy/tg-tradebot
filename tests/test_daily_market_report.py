from types import SimpleNamespace

import numpy as np
import pandas as pd

from app.services.daily_market_report_service import build_daily_market_report, format_daily_market_report


class Provider:
    def get_ohlcv(self, symbol, timeframe, start, end):
        rows = 90
        close = np.linspace(9000, 11000, rows)
        return pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=rows, freq="B", tz="UTC"),
            "open": close - 10, "high": close + 30, "low": close - 30,
            "close": close, "volume": 1_000_000,
        })


def test_daily_report_explains_yesterday_direction_index_and_rate(monkeypatch):
    monkeypatch.setattr(
        "app.services.market_breadth_service.compute_market_breadth",
        lambda *args, **kwargs: SimpleNamespace(advancers=34, decliners=16),
    )
    settings = SimpleNamespace(
        xu100_symbol="XU100.IS", bist_symbols_csv_path="unused.csv", tcmb_policy_rate_percent=42.5,
    )
    report = build_daily_market_report(Provider(), settings)
    text = format_daily_market_report(report)
    assert report.direction == "YUKARI"
    assert "Dünün kapanışı" in text
    assert "XU100" in text and "Ana yön: YUKARI" in text
    assert "Yükselen 34 / Düşen 16" in text
    assert "%42.50" in text


def test_daily_report_never_invents_missing_rate(monkeypatch):
    monkeypatch.setattr("app.services.market_breadth_service.compute_market_breadth", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    settings = SimpleNamespace(xu100_symbol="XU100.IS", bist_symbols_csv_path="unused.csv", tcmb_policy_rate_percent=None)
    assert "Veri bağlı değil" in format_daily_market_report(build_daily_market_report(Provider(), settings))
