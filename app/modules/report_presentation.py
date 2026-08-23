"""Compact, decision-oriented presentation helpers for daily BIST reports."""

from __future__ import annotations

from app.services.market_breadth_service import BreadthCandidate, MarketBreadthResult


def _price(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _candidate_lines(candidate: BreadthCandidate, *, bullish: bool, rank: int) -> list[str]:
    """Render a vivid but short next-session watch card."""

    reasons = " • ".join(candidate.reasons[:3]) or "Sayısal gerekçe yetersiz"
    icon = "🟢" if bullish else "🔴"
    direction = "YÜKSELİŞ SENARYOSU" if bullish else "ZAYIFLIK SENARYOSU"
    lines = [
        f"{rank}. {icon} {candidate.symbol} • {direction}",
        f"   ✨ 10 göstergede {candidate.confluence_count}/10 uyum • Kalite {candidate.score}/100",
        f"   📐 Formasyon: {candidate.pattern_name or 'doğrulanmış yapı'}",
        f"   🔎 Neden: {reasons}",
    ]
    if bullish and candidate.entry_low is not None and candidate.entry_high is not None:
        lines.append(f"   🟡 Retest bölgesi: {_price(candidate.entry_low)}–{_price(candidate.entry_high)}")
    if candidate.confirmation_level is not None and candidate.technical_target is not None:
        distance = ((candidate.technical_target / candidate.last_close) - 1.0) * 100.0
        if bullish:
            lines.append("   ✅ Tetik: bölgede 15dk hacimli yeşil kapanış")
            lines.append(f"   🎯 Formasyon hedefi: {_price(candidate.technical_target)} (%{distance:+.1f})")
        else:
            lines.append(f"   ⚠️ Tetik: {_price(candidate.confirmation_level)} altında günlük kapanış")
            lines.append(f"   🎯 İzlenen alt hedef: {_price(candidate.technical_target)} (%{distance:+.1f})")
    return lines


def format_breadth_panel(breadth: MarketBreadthResult, *, report_kind: str) -> list[str]:
    """Render broad market data as a readable panel, not a symbol wall.

    The levels are observation-based technical reference levels.  They are
    deliberately described as conditional targets, never as direct entries or
    return promises.
    """

    coverage = f"%{breadth.coverage_ratio:.1f}" if breadth.coverage_ratio is not None else "—"
    ema200 = f"%{breadth.above_ema200_ratio:.1f}" if breadth.above_ema200_ratio is not None else "veri yetersiz"
    scope = f"{breadth.scanned}/{breadth.universe_size} ({coverage})"
    phase = "AÇILIŞ ÖNCESİ" if report_kind == "morning" else "KAPANIŞ"
    mood_icon = "🟢" if (breadth.breadth_score or 0) >= 54 else "🔴" if (breadth.breadth_score or 0) <= 46 else "🟡"
    lines = [
        "",
        f"┏━━ 🌐 {breadth.universe_size} HİSSE • {phase} PUSULASI ━━┓",
        f"{mood_icon} Piyasa modu: {breadth.regime} • {breadth.breadth_score}/100",
        f"📈 Yükselen {breadth.advancers}  |  📉 Düşen {breadth.decliners}  |  Net {breadth.net_breadth:+d}",
        f"📊 EMA50 %{breadth.above_ema50_ratio:.1f}  •  EMA200 {ema200}  •  Hacim %{breadth.rising_volume_ratio:.1f}",
        f"🧭 Sonraki seans: {breadth.tomorrow_bias}  •  Veri kapsamı {scope}",
        "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛",
    ]

    if breadth.long_candidates:
        lines.extend(["", "🚀 YARIN İÇİN EN TEMİZ 2 YÜKSELİŞ SENARYOSU"])
        for rank, candidate in enumerate(breadth.long_candidates[:2], start=1):
            lines.extend(_candidate_lines(candidate, bullish=True, rank=rank))
    else:
        lines.extend(["", "🟡 Yükseliş listesi: 9/10 uyum + formasyon + likiditeyi birlikte geçen aday yok; isim zorlanmadı."])
    if breadth.short_candidates:
        lines.extend(["", "🛡️ 2 ZAYIFLIK / RİSK SENARYOSU • SPOTTA SHORT ÇAĞRISI DEĞİL"])
        for rank, candidate in enumerate(breadth.short_candidates[:2], start=1):
            lines.extend(_candidate_lines(candidate, bullish=False, rank=rank))
    else:
        lines.extend(["", "🟡 Zayıflık listesi: 9/10 uyum + formasyon + likiditeyi birlikte geçen aday yok."])
    return lines
