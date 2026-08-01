from __future__ import annotations

from app.analysis.bist_trade_plan import BistTradePlan, DirectionPlan
from app.analysis.quality_zone_engine import format_quality_zone_scenario


def _targets(side: DirectionPlan) -> str:
    return "\n".join(
        f"  🎯 TP{i}: {price:.2f} TL  •  {rr:.1f}R"
        for i, (price, rr) in enumerate(zip(side.targets, side.risk_multiples), 1)
    )


def format_bist_trade_plan(plan: BistTradePlan) -> str:
    """Telegram için açıklanabilir, sade ve koşullu işlem planı."""
    preferred = (
        plan.long
        if plan.quality_zone is not None and plan.quality_zone.direction == "LONG"
        else plan.short
        if plan.quality_zone is not None
        else plan.long
        if plan.long.score >= plan.short.score
        else plan.short
    )
    decision_icon = "🟢" if preferred.direction == "LONG" else "🔴"
    confirmations = "\n".join(f"  ✅ {item}" for item in preferred.confirmations[:5]) or "  • Ek teyit yok"
    risks = "\n".join(f"  ⚠️ {item}" for item in preferred.risks[:4]) or "  • Belirgin ek risk işaretlenmedi"
    breakdown = "\n".join(f"  • {item}" for item in preferred.score_breakdown)
    warnings = "\n".join(f"  • {item}" for item in plan.warnings)
    zone_block = (
        format_quality_zone_scenario(plan.quality_zone)
        if plan.quality_zone is not None
        else "🎯 EN YAKIN KALİTELİ BÖLGE: Doğrulanmış OB/FVG bulunamadı; işlem zorlanmamalı."
    )
    return (
        f"📊 {plan.symbol} • KALİTELİ İŞLEM PLANI\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{decision_icon} KARAR: Tetik bekle • güncel fiyattan doğrudan giriş yok\n\n"
        f"💰 Son fiyat: {plan.current_price:.2f} TL\n"
        f"🧭 Trend: {plan.trend}  •  RSI {plan.rsi:.1f}  •  ADX {plan.adx:.1f}\n"
        f"🌊 ATR %{plan.atr_percent:.2f}  •  Göreli hacim {plan.relative_volume:.2f}x\n\n"
        f"{zone_block}\n\n"
        f"⭐ ÖNCELİKLİ PUAN: {preferred.direction} • {preferred.score}/100\n"
        f"Kalite: {preferred.status}\n"
        f"🧮 PUAN NEREDEN GELDİ?\n{breakdown}\n\n"
        f"🔎 TEYİTLER\n{confirmations}\n\n"
        f"🚧 RİSKLER\n{risks}\n\n"
        f"ℹ️ KONTROL NOTLARI\n{warnings}\n\n"
        "Tetik oluşmadan işlem aktif sayılmaz. Bu çıktı yatırım tavsiyesi değildir."
    )
