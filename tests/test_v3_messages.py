from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.analysis.advanced_scoring import compute_advanced_score
from app.analysis.indicator_engine import compute_technical_snapshot
from app.analysis.market_regime_engine import classify_market_regime
from app.analysis.relative_strength_engine import compute_relative_strength
from app.analysis.signal_engine import evaluate_signal
from app.telegram.message_templates_v3 import (
    format_detailed_analysis,
    format_short_summary,
    split_long_message,
)


def _build(mock_provider, strategy_config, symbol="THYAO"):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    df = mock_provider.get_ohlcv(symbol, "1d", start, end)
    index_df = mock_provider.get_ohlcv("XU100", "1d", start, end)
    snapshot = compute_technical_snapshot(df, symbol, "1d")
    regime = classify_market_regime(mock_provider, "XU100", "1d")
    signal = evaluate_signal(snapshot, regime, mock_provider.name, strategy_config, df=df)
    signal.extras["close"] = snapshot.close
    signal.extras["trend_direction"] = snapshot.trend_direction
    xu100_rs = compute_relative_strength(df, index_df)
    advanced = compute_advanced_score(signal, xu100_rs, None)
    return signal, advanced, xu100_rs


def test_short_summary_contains_required_fields(mock_provider, strategy_config):
    signal, advanced, xu100_rs = _build(mock_provider, strategy_config)
    text = format_short_summary(signal, "THYAO", "confirmed_close", advanced, xu100_rs)

    for label in ["Son kapanış:", "Sinyal:", "Skor:", "Trend:", "Piyasa rejimi:", "XU100", "Destek:", "Direnç:", "Tetik:", "Stop:", "Hedef 1:", "Risk/Getiri:"]:
        assert label in text, f"Eksik alan: {label}"


def test_short_summary_marks_intraday_mode(mock_provider, strategy_config):
    signal, advanced, xu100_rs = _build(mock_provider, strategy_config)
    text = format_short_summary(signal, "THYAO", "intraday_preview", advanced, xu100_rs)
    assert "GÜN İÇİ ÖN ANALİZ" in text
    assert "kesin sinyal değildir" in text


def test_detailed_analysis_contains_score_breakdown(mock_provider, strategy_config):
    from app.services.sector_service import get_sector_info

    signal, advanced, xu100_rs = _build(mock_provider, strategy_config)
    text = format_detailed_analysis(
        signal, "THYAO", "confirmed_close", advanced, xu100_rs, None, "Eslesmemis", None, []
    )
    for label in ["Trend:", "Momentum:", "Hacim:", "Destek/Direnç:", "XU100 gücü:", "Sektör gücü:", "Piyasa rejimi:", "Risk/Getiri:", "Toplam:"]:
        assert label in text


def test_split_long_message_respects_limit():
    long_text = "\n".join(f"Satir {i}" for i in range(500))
    parts = split_long_message(long_text, max_length=100)
    assert len(parts) > 1
    for part in parts:
        assert len(part) <= 100


def test_split_long_message_no_split_when_short():
    text = "Kisa bir mesaj"
    parts = split_long_message(text)
    assert parts == [text]
