from types import SimpleNamespace

from app.telegram.signal_list_formatter import format_active_signals


def _signal(**overrides):
    values = {
        "id": 17,
        "symbol": "THYAO",
        "side": "BUY",
        "signal_type": "BUY_CANDIDATE",
        "state": "PENDING_ENTRY",
        "score": 76,
        "entry_zone_low": 300.0,
        "entry_zone_high": 304.0,
        "planned_entry_price": 303.0,
        "entry_trigger": 305.0,
        "actual_entry_price": None,
        "current_stop_price": None,
        "stop_price": 294.0,
        "target_1": 315.0,
        "target_2": 324.0,
        "target_3": 333.0,
        "risk_reward": 1.5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_active_signal_cards_are_readable_and_explain_pending_state():
    message = format_active_signals([_signal()])
    assert "AKTİF SİNYALLER • 1 kayıt" in message
    assert "Giriş bölgesi bekleniyor" in message
    assert "Giriş bölgesi 300.00–304.00 TL" in message
    assert "Stop 294.00 TL" in message
    assert "Detay: /sinyal 17" in message
    assert "SignalStateEnum" not in message


def test_active_signal_cards_show_next_target_and_empty_state():
    message = format_active_signals([_signal(state="TP1_HIT", actual_entry_price=303, side="SELL")])
    assert "TP1 görüldü" in message
    assert "TP2 324.00 TL" in message
    assert "SHORT/RİSK" in message
    assert "açık veya tetik bekleyen sinyal yok" in format_active_signals([])
