from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.analysis.indicator_engine import compute_technical_snapshot
from app.analysis.market_regime_engine import classify_market_regime
from app.analysis.signal_engine import evaluate_signal
from app.telegram.message_templates import format_buy_candidate_message, format_risk_warning_message


def test_buy_message_contains_required_risk_fields(mock_provider, strategy_config):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)
    df = mock_provider.get_ohlcv("THYAO", "1d", start, end)
    snapshot = compute_technical_snapshot(df, "THYAO", "1d")
    regime = classify_market_regime(mock_provider, "XU100", "1d")
    signal = evaluate_signal(snapshot, regime, mock_provider.name, strategy_config)
    signal.extras["close"] = snapshot.close

    text = format_buy_candidate_message(signal)
    assert "Stop" in text
    assert "Risk/Getiri" in text
    assert "Veri zamani" in text
    assert "Veri saglayicisi" in text
    assert "yatirim tavsiyesi degil" in text


def test_risk_message_contains_action(mock_provider, strategy_config):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)
    df = mock_provider.get_ohlcv("THYAO", "1d", start, end)
    snapshot = compute_technical_snapshot(df, "THYAO", "1d")
    regime = classify_market_regime(mock_provider, "XU100", "1d")
    signal = evaluate_signal(snapshot, regime, mock_provider.name, strategy_config)
    signal.extras["close"] = snapshot.close

    text = format_risk_warning_message(signal)
    assert "Onerilen aksiyon turu" in text
