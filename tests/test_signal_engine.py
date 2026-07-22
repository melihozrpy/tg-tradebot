from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.analysis.indicator_engine import compute_technical_snapshot
from app.analysis.market_regime_engine import classify_market_regime
from app.analysis.signal_engine import build_idempotency_key, evaluate_signal


def _get_snapshot(mock_provider, symbol="THYAO", timeframe="1d"):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)
    df = mock_provider.get_ohlcv(symbol, timeframe, start, end)
    return compute_technical_snapshot(df, symbol, timeframe)


def test_signal_score_within_bounds(mock_provider, strategy_config):
    snapshot = _get_snapshot(mock_provider)
    regime = classify_market_regime(mock_provider, "XU100", "1d")
    result = evaluate_signal(snapshot, regime, mock_provider.name, strategy_config)
    assert 0 <= result.score <= 100
    assert result.signal_type in (
        "STRONG_BUY_CANDIDATE",
        "BUY_CANDIDATE",
        "WATCH",
        "NEUTRAL",
        "WEAK_RISK",
        "REDUCE_POSITION",
        "STRONG_RISK",
    )


def test_idempotency_key_stable():
    ts = datetime(2026, 7, 10, tzinfo=timezone.utc)
    k1 = build_idempotency_key("THYAO", "1d", "BUY_CANDIDATE", ts)
    k2 = build_idempotency_key("THYAO", "1d", "BUY_CANDIDATE", ts)
    k3 = build_idempotency_key("THYAO", "1d", "WATCH", ts)
    assert k1 == k2
    assert k1 != k3


def test_buy_signal_requires_stop_and_risk_reward(mock_provider, strategy_config):
    snapshot = _get_snapshot(mock_provider)
    regime = classify_market_regime(mock_provider, "XU100", "1d")
    result = evaluate_signal(snapshot, regime, mock_provider.name, strategy_config)
    if result.is_actionable_buy:
        assert result.stop_price is not None
        assert result.risk_reward is not None
        assert result.risk_reward >= strategy_config["thresholds"]["minimum_risk_reward"]


def test_reasons_never_empty(mock_provider, strategy_config):
    snapshot = _get_snapshot(mock_provider)
    regime = classify_market_regime(mock_provider, "XU100", "1d")
    result = evaluate_signal(snapshot, regime, mock_provider.name, strategy_config)
    assert len(result.reasons) > 0
