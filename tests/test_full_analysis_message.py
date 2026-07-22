from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from app.analysis.indicator_engine import compute_technical_snapshot
from app.analysis.market_regime_engine import classify_market_regime
from app.analysis.signal_engine import evaluate_signal
from app.telegram.message_templates import format_full_analysis_message, format_price


def _build_signal(mock_provider, strategy_config, symbol="SVGYO"):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    df = mock_provider.get_ohlcv(symbol, "1d", start, end)
    snapshot = compute_technical_snapshot(df, symbol, "1d")
    regime = classify_market_regime(mock_provider, "XU100", "1d")
    signal = evaluate_signal(snapshot, regime, mock_provider.name, strategy_config, df=df)
    signal.extras["close"] = snapshot.close
    signal.extras["trend_direction"] = snapshot.trend_direction
    return signal


def test_format_price_never_exceeds_two_decimals():
    assert format_price(140.2118261812036) == "140.21"
    assert format_price(9.5) == "9.50"
    assert format_price(None) == "-"


def test_full_analysis_message_contains_all_required_fields(mock_provider, strategy_config):
    signal = _build_signal(mock_provider, strategy_config)
    text = format_full_analysis_message(signal, display_symbol="SVGYO")

    required_labels = [
        "SVGYO ANALIZI",
        "Son kapanis:",
        "Gunluk degisim:",
        "Veri tarihi:",
        "Trend:",
        "Analiz skoru:",
        "Sinyal:",
        "Piyasa yapisi:",
        "Destekler:",
        "Destek 1:",
        "Destek 2:",
        "Ana destek:",
        "Direncler:",
        "Direnc 1:",
        "Direnc 2:",
        "Ana direnc:",
        "Olasi giris bolgesi:",
        "Tetik seviyesi:",
        "Stop:",
        "Hedef 1:",
        "Hedef 2:",
        "Hedef 3:",
        "Risk/Getiri:",
        "Ana nedenler:",
        "Riskler:",
        "Senaryo su fiyat altinda gecersiz:",
        "Veri saglayicisi:",
        "Son islem gunu:",
        "yatirim tavsiyesi degildir",
    ]
    for label in required_labels:
        assert label in text, f"Eksik alan: {label}"


def test_full_analysis_message_prices_are_two_decimals(mock_provider, strategy_config):
    signal = _build_signal(mock_provider, strategy_config)
    text = format_full_analysis_message(signal, display_symbol="SVGYO")

    # Metindeki tum ondalikli sayilarin en fazla 2 basamak oldugunu dogrula.
    decimal_numbers = re.findall(r"\d+\.\d+", text)
    for num in decimal_numbers:
        decimals = num.split(".")[1]
        assert len(decimals) <= 2, f"Uzun ondalik bulundu: {num}"


def test_full_analysis_message_no_long_float_leak(mock_provider, strategy_config):
    signal = _build_signal(mock_provider, strategy_config)
    text = format_full_analysis_message(signal, display_symbol="SVGYO")
    assert "2118261812036" not in text
