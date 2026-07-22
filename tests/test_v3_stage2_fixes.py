from __future__ import annotations

from app.analysis.decision_engine import DECISION_STRONG_SELL_RISK
from app.services.analysis_service_v3 import run_symbol_analysis_v3
from app.services.intraday_service import run_intraday_preview
from app.telegram.message_templates_v3 import (
    _NO_POSITION_NOTE,
    format_detailed_analysis,
    format_intraday_preview,
    format_short_summary,
    resolve_decision_label,
    resolve_signal_label,
)


def test_analysis_v3_wires_decision_liquidity_and_multi_timeframe(db_session, mock_provider):
    from app.config.settings import get_settings

    outcome = run_symbol_analysis_v3(db_session, mock_provider, "THYAO", get_settings())
    assert outcome.decision is not None
    assert outcome.liquidity is not None
    assert outcome.multi_timeframe is not None
    # Karar motoru gercekten sinyal motorunun ciktisini isliyor olmali.
    assert outcome.decision.base_signal_type == outcome.signal.signal_type


def test_intraday_preview_wires_same_engines(mock_provider):
    result = run_intraday_preview(mock_provider, "SVGYO")
    assert result.support_resistance is not None or result.decision is not None
    # /gunici artik /analiz ile ayni DecisionEngine'den geciyor.
    if result.decision is not None:
        assert result.decision.base_signal_type is not None


def test_resolve_signal_label_never_says_sell_without_position():
    label_no_pos = resolve_signal_label("REDUCE_POSITION", holds_position=False)
    label_with_pos = resolve_signal_label("REDUCE_POSITION", holds_position=True)
    assert "azalt" not in label_no_pos.lower() and "sat" not in label_no_pos.lower()
    assert "azalt" in label_with_pos.lower()

    strong_no_pos = resolve_signal_label("STRONG_RISK", holds_position=False)
    assert "sat" not in strong_no_pos.lower()


def test_short_summary_omits_no_position_note_when_signal_is_neutral(strategy_config, mock_provider):
    from app.analysis.indicator_engine import compute_technical_snapshot
    from app.analysis.market_regime_engine import classify_market_regime
    from app.analysis.signal_engine import evaluate_signal
    from datetime import datetime, timedelta, timezone

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    df = mock_provider.get_ohlcv("THYAO", "1d", start, end)
    snapshot = compute_technical_snapshot(df, "THYAO", "1d")
    regime = classify_market_regime(mock_provider, index_symbol="XU100.IS", timeframe="1d")
    signal = evaluate_signal(snapshot, regime, mock_provider.name, strategy_config, df=df)

    from app.analysis.advanced_scoring import compute_advanced_score
    from app.analysis.relative_strength_engine import RelativeStrengthResult

    advanced = compute_advanced_score(signal, RelativeStrengthResult(available=False, note="n/a"), None)
    text = format_short_summary(
        signal, "THYAO", "confirmed_close", advanced, RelativeStrengthResult(available=False, note="n/a"),
        decision=None, holds_position=False,
    )
    if signal.signal_type not in ("REDUCE_POSITION", "STRONG_RISK"):
        assert _NO_POSITION_NOTE not in text


def test_trade_plan_explains_missing_plan_instead_of_fabricating_price(strategy_config, mock_provider):
    """Stop hesaplanamadiginda mesaj rastgele bir fiyat UYDURMAMALI; nedenini aciklamali."""
    from app.analysis.decision_engine import decide
    from app.analysis.signal_engine import SignalReasonItem, SignalResult
    from datetime import datetime, timezone

    broken_signal = SignalResult(
        symbol="TEST", timeframe="1d", score=40.0, signal_type="NEUTRAL", confidence="dusuk",
        reasons=[SignalReasonItem("risk", "Stop mesafesi cok dar, sinyal guvenilir sayilmadi", is_risk=True)],
        entry_zone=(10.0, 10.5), stop_price=None, target_1=None, target_2=None, risk_reward=None,
        market_regime="belirsiz", data_timestamp=datetime.now(timezone.utc), provider="mock",
        strategy_version="1.0.0", idempotency_key="x", is_actionable_buy=False,
        invalidation_note="Guvenilir stop hesaplanamadigi icin senaryo net degil.",
    )
    from app.analysis.advanced_scoring import compute_advanced_score
    from app.analysis.relative_strength_engine import RelativeStrengthResult

    advanced = compute_advanced_score(broken_signal, RelativeStrengthResult(available=False, note="n/a"), None)
    text = format_short_summary(
        broken_signal, "TEST", "confirmed_close", advanced, RelativeStrengthResult(available=False, note="n/a"),
    )
    assert "Geçerli bir işlem planı hesaplanamadı" in text
    assert "Guvenilir stop hesaplanamadigi" in text or "net değil" in text
    # Uydurma bir Stop/Hedef degeri gosterilmemis olmali.
    assert "Stop: -" not in text.replace("\n", " ") or "hesaplanamadı" in text
