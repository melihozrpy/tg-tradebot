from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.indicator_engine import compute_technical_snapshot
from app.analysis.market_regime_engine import classify_market_regime
from app.analysis.signal_engine import evaluate_signal
from app.analysis.support_resistance_engine import compute_support_resistance, round2


def _get_df_and_snapshot(mock_provider, symbol="THYAO", timeframe="1d"):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    df = mock_provider.get_ohlcv(symbol, timeframe, start, end)
    snapshot = compute_technical_snapshot(df, symbol, timeframe)
    return df, snapshot


def test_round2_never_shows_long_decimals():
    assert round2(140.2118261812036) == 140.21
    assert round2(None) is None


def test_support_levels_are_below_current_price(mock_provider):
    df, snapshot = _get_df_and_snapshot(mock_provider)
    sr = compute_support_resistance(df, snapshot.close, snapshot.ema20, snapshot.ema50, snapshot.atr)
    if sr.support_1 is not None:
        assert sr.support_1 < snapshot.close
    if sr.support_2 is not None:
        assert sr.support_2 < snapshot.close
    if sr.main_support is not None:
        assert sr.main_support < snapshot.close


def test_resistance_levels_are_above_current_price(mock_provider):
    df, snapshot = _get_df_and_snapshot(mock_provider)
    sr = compute_support_resistance(df, snapshot.close, snapshot.ema20, snapshot.ema50, snapshot.atr)
    if sr.resistance_1 is not None:
        assert sr.resistance_1 > snapshot.close
    if sr.resistance_2 is not None:
        assert sr.resistance_2 > snapshot.close
    if sr.main_resistance is not None:
        assert sr.main_resistance > snapshot.close


def test_insufficient_bars_marks_levels_unreliable(mock_provider):
    df, snapshot = _get_df_and_snapshot(mock_provider)
    tiny_df = df.tail(10)  # MIN_BARS_FOR_LEVELS (30) altinda
    sr = compute_support_resistance(tiny_df, snapshot.close, snapshot.ema20, snapshot.ema50, snapshot.atr)
    assert sr.support_reliable is False
    assert sr.resistance_reliable is False
    assert "Guvenilir seviye hesaplanamadi." in sr.support_note


def test_levels_never_fabricated_when_atr_zero(mock_provider):
    df, snapshot = _get_df_and_snapshot(mock_provider)
    sr = compute_support_resistance(df, snapshot.close, snapshot.ema20, snapshot.ema50, atr=0.0)
    assert sr.support_1 is None
    assert sr.resistance_1 is None
    assert sr.support_reliable is False


# ---------------------------------------------------------------------------
# Giris / stop / hedef / risk-getiri mantigi (evaluate_signal, df ile)
# ---------------------------------------------------------------------------


def test_evaluate_signal_with_df_produces_targets_from_resistance(mock_provider, strategy_config):
    df, snapshot = _get_df_and_snapshot(mock_provider)
    regime = classify_market_regime(mock_provider, "XU100", "1d")
    signal = evaluate_signal(snapshot, regime, mock_provider.name, strategy_config, df=df)

    assert signal.support_resistance is not None
    if signal.target_1 is not None:
        assert signal.target_1 > snapshot.close
    if signal.target_2 is not None and signal.target_1 is not None:
        assert signal.target_2 > signal.target_1
    if signal.target_3 is not None and signal.target_2 is not None:
        assert signal.target_3 > signal.target_2


def test_stop_never_absurdly_narrow_or_wide(mock_provider, strategy_config):
    df, snapshot = _get_df_and_snapshot(mock_provider)
    regime = classify_market_regime(mock_provider, "XU100", "1d")
    signal = evaluate_signal(snapshot, regime, mock_provider.name, strategy_config, df=df)

    if signal.stop_price is not None:
        distance_percent = (snapshot.close - signal.stop_price) / snapshot.close
        assert 0.005 <= distance_percent <= 0.15


def test_low_risk_reward_blocks_strong_buy(mock_provider, strategy_config):
    """Risk/getiri esigin altindaysa is_actionable_buy True olamaz."""
    df, snapshot = _get_df_and_snapshot(mock_provider)
    regime = classify_market_regime(mock_provider, "XU100", "1d")
    signal = evaluate_signal(snapshot, regime, mock_provider.name, strategy_config, df=df)

    min_rr = strategy_config["thresholds"]["minimum_risk_reward"]
    if signal.risk_reward is not None and signal.risk_reward < min_rr:
        assert signal.is_actionable_buy is False
        assert "Risk/getiri yetersiz." in signal.contextual_notes


def test_actionable_buy_always_has_valid_risk_reward(mock_provider, strategy_config):
    df, snapshot = _get_df_and_snapshot(mock_provider)
    regime = classify_market_regime(mock_provider, "XU100", "1d")
    signal = evaluate_signal(snapshot, regime, mock_provider.name, strategy_config, df=df)

    if signal.is_actionable_buy:
        assert signal.risk_reward is not None
        assert signal.risk_reward >= strategy_config["thresholds"]["minimum_risk_reward"]
        assert signal.stop_price is not None


def test_daily_change_percent_computed_when_df_present(mock_provider, strategy_config):
    df, snapshot = _get_df_and_snapshot(mock_provider)
    regime = classify_market_regime(mock_provider, "XU100", "1d")
    signal = evaluate_signal(snapshot, regime, mock_provider.name, strategy_config, df=df)
    assert signal.daily_change_percent is not None


def test_backward_compatible_without_df(mock_provider, strategy_config):
    """df verilmezse eski (basit ATR tabanli) davranis korunur; hicbir kirilma olmaz."""
    df, snapshot = _get_df_and_snapshot(mock_provider)
    regime = classify_market_regime(mock_provider, "XU100", "1d")
    signal = evaluate_signal(snapshot, regime, mock_provider.name, strategy_config)
    assert signal.support_resistance is None
    assert signal.target_3 is None


# ---------------------------------------------------------------------------
# V3.1 (bolum 7): guven skoru / temas sayisi / EMA100-200 / ankor VWAP
# ---------------------------------------------------------------------------


def test_confidence_scores_are_within_0_100(mock_provider):
    df, snapshot = _get_df_and_snapshot(mock_provider)
    sr = compute_support_resistance(
        df, snapshot.close, snapshot.ema20, snapshot.ema50, snapshot.atr,
        ema100=snapshot.ema100, ema200=snapshot.ema200,
    )
    if sr.support_1_confidence is not None:
        assert 0 <= sr.support_1_confidence <= 100
        assert sr.support_1_touches >= 1
    if sr.resistance_1_confidence is not None:
        assert 0 <= sr.resistance_1_confidence <= 100
        assert sr.resistance_1_touches >= 1


def test_ema100_ema200_are_optional_and_backward_compatible(mock_provider):
    """ema100/ema200 verilmeden cagrilirsa (eski imza) hata olusmamali."""
    df, snapshot = _get_df_and_snapshot(mock_provider)
    sr = compute_support_resistance(df, snapshot.close, snapshot.ema20, snapshot.ema50, snapshot.atr)
    assert sr is not None


def test_more_confirming_sources_increase_confidence():
    """Birden fazla bagimsiz yontemin ayni bolgede birlestigi bir seviye,
    tek yontemden gelen bir seviyeden daha yuksek guven skoruna sahip olmali."""
    from app.analysis.support_resistance_engine import LevelZone, _zone_confidence

    single_source_zone = LevelZone(price=10.0, votes=1, sources=["ema20"])
    multi_source_zone = LevelZone(price=10.0, votes=4, sources=["ema20", "ema50", "swing_low", "hacimli_bolge"])
    assert _zone_confidence(multi_source_zone) > _zone_confidence(single_source_zone)


def test_role_reversal_flags_are_booleans(mock_provider):
    df, snapshot = _get_df_and_snapshot(mock_provider)
    sr = compute_support_resistance(df, snapshot.close, snapshot.ema20, snapshot.ema50, snapshot.atr)
    assert isinstance(sr.support_role_reversal, bool)
    assert isinstance(sr.resistance_role_reversal, bool)
