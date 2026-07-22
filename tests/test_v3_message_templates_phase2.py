from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.analysis.liquidity_engine import compute_liquidity
from app.analysis.multi_timeframe_engine import analyze_multi_timeframe
from app.services.intraday_service import run_intraday_preview
from app.telegram.message_templates_v3 import (
    format_intraday_preview,
    format_liquidity,
    format_multi_timeframe,
)


def test_format_intraday_preview_smoke(mock_provider):
    result = run_intraday_preview(mock_provider, "SVGYO")
    text = format_intraday_preview(result, "SVGYO")
    assert "GÜN İÇİ ÖN ANALİZ" in text
    assert "yatırım tavsiyesi değildir" in text


def test_format_multi_timeframe_smoke(mock_provider):
    result = analyze_multi_timeframe(mock_provider, "THYAO")
    text = format_multi_timeframe(result, "THYAO")
    assert "ÇOKLU ZAMAN DİLİMİ" in text
    assert "Uyum skoru" in text


def test_format_liquidity_smoke(mock_provider):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=200)
    df = mock_provider.get_ohlcv("ASELS", "1d", start, end)
    result = compute_liquidity(df)
    text = format_liquidity(result, "ASELS")
    assert "LİKİDİTE" in text
