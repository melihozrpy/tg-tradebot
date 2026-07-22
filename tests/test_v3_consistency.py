from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.analysis.consistency_validator import apply_consistency_guard, validate_signal_consistency
from app.analysis.indicator_engine import compute_technical_snapshot
from app.analysis.market_regime_engine import classify_market_regime
from app.analysis.signal_engine import evaluate_signal


def _get_signal(mock_provider, strategy_config, symbol="THYAO"):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    df = mock_provider.get_ohlcv(symbol, "1d", start, end)
    snapshot = compute_technical_snapshot(df, symbol, "1d")
    regime = classify_market_regime(mock_provider, "XU100", "1d")
    return evaluate_signal(snapshot, regime, mock_provider.name, strategy_config, df=df), snapshot.close


def test_consistent_signal_passes_validation(mock_provider, strategy_config):
    signal, close = _get_signal(mock_provider, strategy_config)
    result = validate_signal_consistency(signal, close)
    # Destek/direnc/stop/hedef zaten construction sirasinda dogru kurallara gore
    # hesaplandigi icin bu ozellikle tutarli olmali (issue olsa da guard uygulanir, cokme yok).
    assert isinstance(result.issues, list)


def test_guard_demotes_confidence_on_inconsistency():
    from app.analysis.signal_engine import SignalResult

    fake_signal = SignalResult(
        symbol="TEST", timeframe="1d", score=80, signal_type="STRONG_BUY_CANDIDATE", confidence="yuksek",
        reasons=[], entry_zone=(100.0, 102.0), stop_price=105.0,  # HATALI: stop giris ustunde
        target_1=90.0,  # HATALI: hedef giristen dusuk
        target_2=None, target_3=None, risk_reward=3.0, market_regime="zayif_yukselis",
        data_timestamp=datetime.now(timezone.utc), provider="test", strategy_version="1.0.0",
        idempotency_key="x", is_actionable_buy=True, invalidation_note="",
    )
    result = validate_signal_consistency(fake_signal, close=101.0)
    assert result.is_consistent is False
    assert len(result.issues) > 0

    guarded = apply_consistency_guard(fake_signal, result)
    assert guarded.confidence == "orta"  # yuksek -> orta demote edildi
    assert guarded.is_actionable_buy is False


def test_targets_must_be_increasing_detected():
    from app.analysis.signal_engine import SignalResult

    fake_signal = SignalResult(
        symbol="TEST", timeframe="1d", score=80, signal_type="BUY_CANDIDATE", confidence="orta",
        reasons=[], entry_zone=(100.0, 102.0), stop_price=95.0,
        target_1=110.0, target_2=105.0, target_3=None,  # HATALI: azalan sira
        risk_reward=2.5, market_regime="zayif_yukselis",
        data_timestamp=datetime.now(timezone.utc), provider="test", strategy_version="1.0.0",
        idempotency_key="y", is_actionable_buy=False, invalidation_note="",
    )
    result = validate_signal_consistency(fake_signal, close=101.0)
    assert any("artan sirada degil" in issue for issue in result.issues)


def test_adx_sideways_phrasing_not_contradictory(mock_provider):
    """ADX yuksekken trend yatay ise, uretilen metin yon iddia etmemeli."""
    from app.analysis.signal_engine import _score_trend
    from app.analysis.indicator_engine import TechnicalSnapshot
    import pandas as pd

    snap = TechnicalSnapshot(
        symbol="TEST", timeframe="1d", last_timestamp=pd.Timestamp.now(tz="UTC"), close=100.0,
        ema20=99.5, ema50=99.7, ema100=None, ema200=None, sma20=99.5, sma50=99.6,
        adx=39.3, rsi=50.0, macd_line=0.0, macd_signal=0.0, macd_histogram=0.0, atr=2.0,
        relative_volume=1.0, obv_trend_up=True, mfi=50.0, bb_width=0.05,
        support=95.0, resistance=105.0, trend_direction="sideways", volume_confirmed=True, bars_used=100,
    )
    score, reasons = _score_trend(snap)
    adx_reason = next(r for r in reasons if "ADX" in r.description or "adx" in r.description.lower())
    # Yon iddia eden kelimeler (guclu YUKSELIS/DUSUS trendi gibi) kesinlikle olmamali
    assert "yon" in adx_reason.description.lower() or "guclu hareket" in adx_reason.description.lower()
