"""Compact, decision-oriented presentation helpers for daily BIST reports."""

from __future__ import annotations

from app.services.market_breadth_service import BreadthCandidate, MarketBreadthResult


def _price(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _candidate_lines(candidate: BreadthCandidate, *, bullish: bool, rank: int) -> list[str]:
    """Keep the overnight watchlist compact and mechanically verifiable."""

    reasons = " • ".join(candidate.reasons[:3]) or "Teknik veri yetersiz"
    quality = f"Kalite {candidate.score}/100"
    if candidate.rsi14 is not None:
        quality += f" • RSI {candidate.rsi14:.0f}"
    lines = [f"{rank}) *{candidate.symbol}* • {quality}", f"   Neden: {reasons}"]
    if candidate.confirmation_level is not None and candidate.technical_target is not None:
        target_distance = ((candidate.technical_target / candidate.last_close) - 1.0) * 100.0
        if bullish:
            lines.append(
                f"   Tetik: {_price(candidate.confirmation_level)} üstü kapanış → "
                f"Direnç: {_price(candidate.technical_target)} (%{target_distance:+.1f})"
            )
        else:
            lines.append(
                f"   Tetik: {_price(candidate.confirmation_level)} altı kapanış → "
                f"Destek: {_price(candidate.technical_target)} (%{target_distance:+.1f})"
            )
    else:
        lines.append("   Seviye: doğrulanmış karşı swing yok; izleme dışı bırakılmalı.")
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
    lines = [
        "",
        f"🌐 {breadth.universe_size} HİSSE • PİYASA İÇ YAPISI ({phase})",
        f"• Kapsam {scope} • Piyasa puanı {breadth.breadth_score}/100 ({breadth.regime})",
        f"• Yükselen/Düşen: {breadth.advancers}/{breadth.decliners} • Net: {breadth.net_breadth:+d}",
        f"• EMA50/200 üstü: %{breadth.above_ema50_ratio:.1f} / {ema200} • Hacim: %{breadth.rising_volume_ratio:.1f}",
        f"🧭 Sonraki seans çerçevesi: {breadth.tomorrow_bias}",
    ]

    if breadth.long_candidates:
        lines.extend(["", "🟢 SONRAKİ SEANS İÇİN 3 TEMİZ YÜKSELİŞ ADAYI"])
        for rank, candidate in enumerate(breadth.long_candidates[:3], start=1):
            lines.extend(_candidate_lines(candidate, bullish=True, rank=rank))
    else:
        lines.extend(["", "🟢 Yükseliş adayı: aşırı uzamamış ve yeterli teyitli aday bulunamadı."])
    if breadth.short_candidates:
        lines.extend(["", "🔴 ZAYIFLAYABİLECEK 3 HİSSE • SPOTTA SHORT ÇAĞRISI DEĞİL"])
        for rank, candidate in enumerate(breadth.short_candidates[:3], start=1):
            lines.extend(_candidate_lines(candidate, bullish=False, rank=rank))
    else:
        lines.extend(["", "🔴 Zayıflık adayı: aşırı uzamamış ve yeterli teyitli aday bulunamadı."])
    return lines
