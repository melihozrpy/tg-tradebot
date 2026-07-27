from __future__ import annotations

from app.analysis.bist_trade_plan import BistTradePlan, DirectionPlan


def _targets(side: DirectionPlan) -> str:
    return "\n".join(
        f"  🎯 TP{i}: {price:.2f} TL  •  {rr:.1f}R"
        for i, (price, rr) in enumerate(zip(side.targets, side.risk_multiples), 1)
    )


def format_bist_trade_plan(plan: BistTradePlan) -> str:
    """Telegram için açıklanabilir, sade ve koşullu işlem planı."""
    preferred = plan.long if plan.long.score >= plan.short.score else plan.short
    alternative = plan.short if preferred is plan.long else plan.long
    decision_icon = "🟢" if plan.preferred_direction == "LONG" else "🔴" if plan.preferred_direction == "SHORT" else "🟡"
    confirmations = "\n".join(f"  ✅ {item}" for item in preferred.confirmations[:5]) or "  • Ek teyit yok"
    risks = "\n".join(f"  ⚠️ {item}" for item in preferred.risks[:4]) or "  • Belirgin ek risk işaretlenmedi"
    breakdown = "\n".join(f"  • {item}" for item in preferred.score_breakdown)
    warnings = "\n".join(f"  • {item}" for item in plan.warnings)
    return (
        f"📊 {plan.symbol} • KALİTELİ İŞLEM PLANI\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{decision_icon} KARAR: {plan.decision}\n\n"
        f"💰 Son fiyat: {plan.current_price:.2f} TL\n"
        f"🧭 Trend: {plan.trend}  •  RSI {plan.rsi:.1f}  •  ADX {plan.adx:.1f}\n"
        f"🌊 ATR %{plan.atr_percent:.2f}  •  Göreli hacim {plan.relative_volume:.2f}x\n\n"
        f"⭐ ÖNCELİKLİ PUAN: {preferred.direction} • {preferred.score}/100\n"
        f"Kalite: {preferred.status}\n"
        f"Yöntem: {preferred.entry_method}\n\n"
        f"📍 Giriş bölgesi: {preferred.entry_low:.2f} – {preferred.entry_high:.2f} TL\n"
        f"⚡ Tetik: {preferred.trigger:.2f} TL tamamlanmış kapanış + hacim\n"
        f"❌ Geçersizlik: {preferred.invalidation}\n\n"
        "🛡️ STOP SEÇENEKLERİ\n"
        f"  Agresif {preferred.stop_aggressive:.2f}  •  Standart {preferred.stop_standard:.2f}  •  Geniş {preferred.stop_conservative:.2f}\n\n"
        f"🎯 HEDEFLER\n{_targets(preferred)}\n\n"
        f"🧮 PUAN NEREDEN GELDİ?\n{breakdown}\n\n"
        f"🔎 TEYİTLER\n{confirmations}\n\n"
        f"🚧 RİSKLER\n{risks}\n\n"
        f"🔄 ALTERNATİF YÖN: {alternative.direction} • {alternative.score}/100 • {alternative.status}\n"
        f"   Bölge {alternative.entry_low:.2f}–{alternative.entry_high:.2f} • Tetik {alternative.trigger:.2f}\n\n"
        f"ℹ️ KONTROL NOTLARI\n{warnings}\n\n"
        "Tetik oluşmadan işlem aktif sayılmaz. Bu çıktı yatırım tavsiyesi değildir."
    )
