from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any


STATE_LABELS = {
    "CREATED": ("⚪", "Plan oluşturuldu"),
    "WAITING_CONFIRMATION": ("🟡", "Kapanış teyidi bekleniyor"),
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
    return "Zayıf / pas geç"


def _next_target(signal: Any, state: str) -> tuple[str, Any]:
    if state in {"TARGET_2_HIT", "TP2_HIT"}:
        return "TP3", getattr(signal, "target_3", None)
    if state in {"TARGET_1_HIT", "TP1_HIT"}:
        return "TP2", getattr(signal, "target_2", None)
    return "TP1", getattr(signal, "target_1", None)


def _next_action(signal: Any, state: str) -> str:
    if state in {"CREATED", "WAITING_CONFIRMATION"}:
        return "Mum kapanışı ve yapı teyidi gelmeden işlem alma."
    if state in {"WAITING_TRIGGER", "SENT", "PENDING_ENTRY"}:
        trigger = getattr(signal, "entry_trigger", None)
        return f"{_money(trigger)} tetik seviyesi ve ardından retest/kapanış teyidini izle."
    if state in {"CONFIRMED", "ACTIVE"}:
        return "Stop disiplinini koru; TP görülmeden hedefi rastgele değiştirme."
    if state in {"TARGET_1_HIT", "TP1_HIT"}:
        return "TP1 sonrası plana göre kısmi kâr ve maliyet stopu değerlendir."
    if state in {"TARGET_2_HIT", "TP2_HIT"}:
        return "TP2 sonrası kalan pozisyonu güncel stop ile koru."
    if state == "SUSPENDED":
        return "Yeni veri doğrulanana kadar işlem/ekleme yapma."
    return "Sinyal detayındaki geçersizlik ve veri zamanını kontrol et."


def _evidence(signal: Any) -> tuple[list[str], list[str]]:
    positives: list[str] = []
    risks: list[str] = []
    for reason in list(getattr(signal, "reasons", None) or []):
        text = str(getattr(reason, "description", "") or "").strip()
        if not text:
            continue
        (risks if bool(getattr(reason, "is_risk", False)) else positives).append(text)
    return positives[:2], risks[:2]


def _timestamp(signal: Any) -> str:
    raw = getattr(signal, "data_timestamp", None) or getattr(signal, "created_at", None)
    if not isinstance(raw, datetime):
        return "veri zamanı yok"
    value = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    return value.strftime("%d.%m.%Y %H:%M") + " UTC"


def _card(signal: Any, *, detailed: bool = True) -> str:
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
    stop = (
        getattr(signal, "current_stop_price", None)
        or getattr(signal, "invalidation_price", None)
        or getattr(signal, "stop_price", None)
    )
    rr = getattr(signal, "risk_reward", None)
    rr_text = f"{float(rr):.2f}R" if rr is not None else "hesaplanamadı"
    positives, risks = _evidence(signal)
    lines = [
        f"{side_icon} #{getattr(signal, 'id', '-')} • {getattr(signal, 'symbol', '-')} • {side_label}",
        f"{state_icon} {state_label} • ⭐ {score:.0f}/100 ({_quality(score)})",
        f"📍 {entry_text} • Tetik {_money(getattr(signal, 'entry_trigger', None))}",
        f"🛡️ Stop {_money(stop)} TL • Teknik geçersizlik {_money(stop)} TL",
        f"🎯 TP1 {_money(getattr(signal, 'target_1', None))} • TP2 {_money(getattr(signal, 'target_2', None))} • TP3 {_money(getattr(signal, 'target_3', None))}",
        f"⚖️ Plan R/R {rr_text} • Sonraki {_next_target(signal, state)[0]} {_money(_next_target(signal, state)[1])} TL",
    ]
    if detailed:
        regime = getattr(signal, "market_regime", None)
        relative = getattr(signal, "relative_strength_score", None)
        lines.append(
            f"🌍 Rejim {regime or 'belirsiz'} • Göreceli güç "
            + (f"{float(relative):.0f}/100" if relative is not None else "veri yok")
        )
        if positives:
            lines.append("✅ Kanıt: " + " • ".join(positives))
        if risks:
            lines.append("⚠️ Risk: " + " • ".join(risks))
        lines.extend(
            [
                f"🧭 Şimdi: {_next_action(signal, state)}",
                f"🕒 {_timestamp(signal)} • Detay: /sinyal {getattr(signal, 'id', '-')}",
            ]
        )
    return "\n".join(lines)


def format_active_signals(signals: Sequence[Any], *, limit: int = 8) -> str:
    visible = list(signals[:limit])
    if not visible:
        return (
            "🎯 AKTİF SİNYALLER\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Şu anda açık veya tetik bekleyen sinyal yok.\n\n"
            "Yeni plan: /islemplani THYAO\n"
            "Son kayıtlar: /sinyaller"
        )

    directions = Counter(_side(signal)[1] for signal in signals)
    quality = Counter(_quality(float(getattr(signal, "score", 0) or 0)) for signal in signals)
    blocks = [
        f"🎯 AKTİF SİNYALLER • {len(signals)} kayıt",
        "━━━━━━━━━━━━━━━━━━",
        f"🟢 LONG {directions['LONG']} • 🔴 SHORT/RİSK {directions['SHORT/RİSK']}",
        f"⭐ A {quality['A kalite']} • B {quality['B kalite']} • C {quality['C kalite']} • Zayıf {quality['Zayıf / pas geç']}",
        "Puan olasılık değil; teknik kurulum kalitesidir.",
    ]
    for signal in visible:
        card = _card(signal)
        if len("\n\n".join([*blocks, card])) > 3800:
            break
        blocks.append(card)
    shown = len(blocks) - 5
    hidden = len(signals) - shown
    if hidden > 0:
        blocks.append(f"➕ {hidden} kayıt daha var. Sembol veya kimlik ile: /sinyal THYAO")
    blocks.append("🧭 PENDING/TETİK BEKLİYOR kayıtları açık pozisyon değildir.")
    return "\n\n".join(blocks)[:4096]


def format_recent_signals(signals: Sequence[Any], *, limit: int = 12) -> str:
    visible = list(signals[:limit])
    if not visible:
        return "📌 SON SİNYALLER\n\nHenüz kayıtlı sinyal yok."
    blocks = [
        f"📌 SON SİNYALLER • son {len(visible)} kayıt",
        "━━━━━━━━━━━━━━━━━━",
        "Her kayıt için yön, durum, plan ve veri zamanı aşağıdadır.",
    ]
    for signal in visible:
        card = _card(signal, detailed=False) + f"\n🕒 {_timestamp(signal)} • Detay /sinyal {getattr(signal, 'id', '-')}"
        if len("\n\n".join([*blocks, card])) > 3900:
            break
        blocks.append(card)
    return "\n\n".join(blocks)[:4096]
