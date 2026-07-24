from __future__ import annotations

from app.analysis.bist_trade_plan import BistTradePlan, DirectionPlan


def _targets(side: DirectionPlan) -> str:
    return "  •  ".join(
        f"TP{i} {price:.2f} ({rr:.1f}R)"
        for i, (price, rr) in enumerate(zip(side.targets, side.risk_multiples), 1)
    )


def format_bist_trade_plan(plan: BistTradePlan) -> str:
    """Telegram için kısa, düzgün UTF-8 ve kolay taranabilir işlem planı."""
    preferred = plan.long if plan.long.score >= plan.short.score else plan.short
    alternative = plan.short if preferred is plan.long else plan.long
    return (
        f"📊 {plan.symbol} İŞLEM PLANI\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 Fiyat: {plan.current_price:.2f} TL\n"
        f"🧭 Trend: {plan.trend}  |  RSI: {plan.rsi:.1f}  |  ATR: %{plan.atr_percent:.2f}\n\n"
        f"⭐ ÖNCELİKLİ: {preferred.direction} ({preferred.score}/100)\n"
        f"Giriş: {preferred.entry_low:.2f} – {preferred.entry_high:.2f} TL\n"
        f"Tetik: {preferred.trigger:.2f} TL kapanış + hacim teyidi\n"
        f"Standart stop: {preferred.stop_standard:.2f} TL\n"
        f"Hedefler: {_targets(preferred)}\n\n"
        "🛡️ STOP SEÇENEKLERİ\n"
        f"Agresif: {preferred.stop_aggressive:.2f}  |  "
        f"Standart: {preferred.stop_standard:.2f}  |  Geniş: {preferred.stop_conservative:.2f}\n\n"
        f"🔄 ALTERNATİF: {alternative.direction} ({alternative.score}/100)\n"
        f"Giriş: {alternative.entry_low:.2f} – {alternative.entry_high:.2f} TL\n"
        f"Tetik: {alternative.trigger:.2f} TL  |  Stop: {alternative.stop_standard:.2f} TL\n"
        f"Hedefler: {_targets(alternative)}\n\n"
        "⚠️ Tetik gelmeden plan aktif değildir. Short işlem için açığa satış/VİOP uygunluğu kontrol edilmelidir.\n\n"
        "ℹ️ Teknik senaryodur; yatırım tavsiyesi değildir."
    )
