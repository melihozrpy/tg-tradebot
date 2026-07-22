from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from app.analysis.market_state import MODE_CONFIRMED_CLOSE, MODE_INTRADAY_PREVIEW, determine_analysis_mode


def _make_df(last_date_utc: datetime, days: int = 30) -> pd.DataFrame:
    dates = pd.date_range(end=last_date_utc, periods=days, freq="1B", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": dates,
            "open": [10.0] * days,
            "high": [10.5] * days,
            "low": [9.5] * days,
            "close": [10.2] * days,
            "volume": [1000.0] * days,
        }
    )


def test_confirmed_close_when_last_bar_is_yesterday():
    # "Simdi" pazartesi 12:00 TR, son bar cuma (gecmis is gunu) -> kesinlesmis olmali
    now_utc = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)  # 12:00 Istanbul, Pazartesi
    last_bar = now_utc - timedelta(days=3)  # Cuma
    df = _make_df(last_bar)
    result = determine_analysis_mode(df, close_scan_time="18:20", now_utc=now_utc)
    assert result.mode == MODE_CONFIRMED_CLOSE


def test_intraday_preview_when_market_open_and_bar_is_today():
    now_utc = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)  # 13:00 Istanbul (piyasa acik, kapanis saati degil)
    df = _make_df(now_utc)  # son bar bugune ait
    result = determine_analysis_mode(df, close_scan_time="18:20", now_utc=now_utc)
    assert result.mode == MODE_INTRADAY_PREVIEW
    assert result.intraday_quote is not None
    assert len(result.analysis_df) == len(df) - 1  # bugunku (tamamlanmamis) bar analiz disi


def test_confirmed_close_after_close_scan_time_even_if_bar_is_today():
    now_utc = datetime(2026, 7, 13, 16, 0, tzinfo=timezone.utc)  # 19:00 Istanbul, kapanis saati gecti
    df = _make_df(now_utc)
    result = determine_analysis_mode(df, close_scan_time="18:20", now_utc=now_utc)
    assert result.mode == MODE_CONFIRMED_CLOSE


def test_confirmed_close_result_does_not_drop_bars():
    now_utc = datetime(2026, 7, 13, 16, 0, tzinfo=timezone.utc)
    df = _make_df(now_utc)
    result = determine_analysis_mode(df, close_scan_time="18:20", now_utc=now_utc)
    assert len(result.analysis_df) == len(df)


def test_empty_df_handled_gracefully():
    df = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    result = determine_analysis_mode(df)
    assert result.mode == MODE_CONFIRMED_CLOSE
    assert result.last_confirmed_date is None
