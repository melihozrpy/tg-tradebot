from __future__ import annotations

from collections.abc import Sequence
from typing import Any


STATE_LABELS = {
    "CREATED": ("⚪", "Plan oluşturuldu"),
    "WAITING_TRIGGER": ("🟡", "Tetik bekleniyor"),
    "CONFIRMED": ("🟢", "Tetik doğrulandı"),
    "SENT": ("🟡", "Giriş bekleniyor"),
    "PENDING_ENTRY": ("🟡", "Giriş bölgesi bekleniyor"),
    "ACTIVE": ("🟢", "Pozisyon aktif"),
    "TARGET_1_HIT": ("✅", "TP1 görüldü"),
    "TARGET_2_HIT": ("✅", "TP2 görüldü"),
    "TP1_HIT": ("✅", "TP1 görüldü"),
    "TP2_HIT": ("✅", "TP2 görüldü"),
    "EXIT_PENDING": ("🟠", "Çıkış teyidi bekleniyor"),
    "SUSPENDED": ("⏸️", "Veri/şirket işlemi nedeniyle askıda"),
}


def _value(value: Any) -> str:
    return str(getattr(value, "value", value) or "-")


def _money(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def _side(signal: Any) -> tuple[str, str]:
    side = _value(getattr(signal, "side", "")).upper()
    signal_type = _value(getattr(signal, "signal_type", "")).upper()
    if side in {"SELL", "SHORT"} or any(token in signal_type for token in ("RISK", "REDUCE")):
        return "🔴", "SHORT/RİSK"
    return "🟢", "LONG"


def _quality(score: float) -> str:
    if score >= 78:
        return "A kalite"
    if score >= 68:
        return "B kalite"
    if score >= 58:
        return "C kalite"
    return "zayıf"


def _next_target(signal: Any, state: str) -> tuple[str, Any]:
    if state in {"TARGET_2_HIT", "TP2_HIT"}:
        return "TP3", getattr(signal, "target_3", None)
    if state in {"TARGET_1_HIT", "TP1_HIT"}:
        return "TP2", getattr(signal, "target_2", None)
    return "TP1", getattr(signal, "target_1", None)


def format_active_signals(signals: Sequence[Any], *, limit: int = 12) -> str:
    """Render active signals as compact cards instead of a dense CSV-like list."""
    visible = list(signals[:limit])
    if not visible:
        return (
            "🎯 AKTİF SİNYALLER\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Şu anda açık veya tetik bekleyen sinyal yok.\n\n"
            "Yeni plan: /islemplani THYAO\n"
            "Son kayıtlar: /sinyaller"
        )

    blocks = [
        f"🎯 AKTİF SİNYALLER • {len(signals)} kayıt",
        "━━━━━━━━━━━━━━━━━━",
        "Puan, olasılık değil teknik kurulum kalitesidir.",
    ]
    for signal in visible:
        state = _value(getattr(signal, "state", ""))
        state_icon, state_label = STATE_LABELS.get(state, ("🔹", state.replace("_", " ").title()))
        side_icon, side_label = _side(signal)
        score = float(getattr(signal, "score", 0) or 0)
        entry_low = getattr(signal, "entry_zone_low", None)
        entry_high = getattr(signal, "entry_zone_high", None)
        planned = getattr(signal, "planned_entry_price", None) or getattr(signal, "entry_trigger", None)
        actual = getattr(signal, "actual_entry_price", None)
        if actual is not None:
            entry_text = f"Gerçek giriş {_money(actual)} TL"
        elif entry_low is not None and entry_high is not None:
            entry_text = f"Giriş bölgesi {_money(entry_low)}–{_money(entry_high)} TL"
        else:
            entry_text = f"Planlanan giriş {_money(planned)} TL"
        stop = getattr(signal, "current_stop_price", None) or getattr(signal, "stop_price", None)
        target_name, target = _next_target(signal, state)
        rr = getattr(signal, "risk_reward", None)
        rr_text = f"{float(rr):.2f}R" if rr is not None else "-"
        blocks.extend(
            [
                "",
                f"{side_icon} #{getattr(signal, 'id', '-')} • {getattr(signal, 'symbol', '-')} • {side_label}",
                f"{state_icon} {state_label}",
                f"⭐ {score:.0f}/100 • {_quality(score)}",
                f"📍 {entry_text}",
                f"🛡 Stop {_money(stop)} TL  •  🎯 {target_name} {_money(target)} TL",
                f"⚖️ Plan R/R: {rr_text}  •  Detay: /sinyal {getattr(signal, 'id', '-')}",
            ]
        )
    hidden = len(signals) - len(visible)
    if hidden > 0:
        blocks.extend(["", f"➕ {hidden} kayıt daha var. Sembol detayı: /sinyal SEMBOL"])
    blocks.extend(["", "🧭 Tetik gelmeden PENDING kayıtlar aktif işlem değildir."])
    return "\n".join(blocks)[:4096]
