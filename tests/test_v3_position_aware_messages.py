from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.analysis.indicator_engine import compute_technical_snapshot
from app.analysis.market_regime_engine import classify_market_regime
from app.analysis.relative_strength_engine import RelativeStrengthResult
from app.analysis.advanced_scoring import compute_advanced_score
from app.analysis.signal_engine import evaluate_signal
from app.telegram.message_templates_v3 import format_short_summary


def _build_outcome(mock_provider, strategy_config, symbol="THYAO"):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    df = mock_provider.get_ohlcv(symbol, "1d", start, end)
    snapshot = compute_technical_snapshot(df, symbol, "1d")
    regime = classify_market_regime(mock_provider, "XU100", "1d")
    signal = evaluate_signal(snapshot, regime, mock_provider.name, strategy_config, df=df)
    signal.extras["close"] = snapshot.close
    signal.extras["trend_direction"] = snapshot.trend_direction
    xu100_rs = RelativeStrengthResult(available=False, note="test")
    advanced_score = compute_advanced_score(signal, xu100_rs, None)
    return signal, advanced_score, xu100_rs


def _fake_position(lot=100, average_cost=10.0, stop_price=None, target_1=None, target_2=None, target_3=None):
    return SimpleNamespace(
        lot=lot, average_cost=average_cost, stop_price=stop_price,
        target_1=target_1, target_2=target_2, target_3=target_3,
    )


def test_no_position_message_never_says_hold_or_exit(mock_provider, strategy_config):
    signal, advanced_score, xu100_rs = _build_outcome(mock_provider, strategy_config)
    text = format_short_summary(
        signal, "THYAO", "confirmed_close", advanced_score, xu100_rs,
        holds_position=False, position=None,
    )
    assert "Yeni giriş uygunluğu" in text
    assert "POZİSYON DURUMU" not in text
    for forbidden in ("KISMİ KÂR AL", "POZİSYON AZALT", "STOP - TAM ÇIKIŞ"):
        assert forbidden not in text


def test_position_message_shows_full_position_block(mock_provider, strategy_config):
    signal, advanced_score, xu100_rs = _build_outcome(mock_provider, strategy_config)
    close = signal.extras["close"]
    position = _fake_position(
        lot=50, average_cost=close * 0.8,
        stop_price=close * 0.9, target_1=close * 1.05, target_2=close * 1.1, target_3=close * 1.2,
    )
    text = format_short_summary(
        signal, "THYAO", "confirmed_close", advanced_score, xu100_rs,
        holds_position=True, position=position, portfolio_weight_pct=42.5,
    )
    assert "POZİSYON DURUMU" in text
    assert "Lot: 50" in text
    assert "Portföy ağırlığı: %42.5" in text
    assert "Karar:" in text
    assert "Yeni giriş uygunluğu" not in text
