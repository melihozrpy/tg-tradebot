"""Compact, decision-oriented presentation helpers for daily BIST reports."""

from __future__ import annotations

from app.services.market_breadth_service import BreadthCandidate, MarketBreadthResult


def _price(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _candidate_lines(candidate: BreadthCandidate, *, bullish: bool) -> list[str]:
    reasons = " • ".join(candidate.reasons[:3])
    target_line: str | None = None
    if candidate.technical_target is not None and candidate.technical_target > 0:
        distance = ((candidate.technical_target / candidate.last_close) - 1.0) * 100.0
        target_label = "Potansiyel hedef" if bullish else "Düşüş potansiyeli"
        target_line = (
            f"   {target_label}: {_price(candidate.technical_target)} "
            f"({candidate.target_basis}; %{distance:+.1f})"
        )
    if bullish:
        reason_label = "Neden güçlü"
        risk = "Genel piyasa çerçevesi değişirse bireysel güç de zayıflayabilir."
    else:
        reason_label = "Neden zayıf"
        risk = "Spot BIST'te bu bir açığa satış çağrısı değil; hacim teyitsiz işlem alınmamalı."
    lines = [
        f"▸ {candidate.symbol}  (Skor: {candidate.score}/100 • Günlük: %{candidate.change_percent:+.1f})",
        f"   {reason_label}: {reasons or 'Yeterli sayısal teknik gerekçe üretilemedi.'}",
    ]
    if target_line:
        lines.append(target_line)
    if candidate.confirmation_level is not None and candidate.confirmation_level > 0:
        if bullish:
            lines.append(f"   Teyit seviyesi: {_price(candidate.confirmation_level)} üstünde günlük kapanış")
        else:
            lines.append(f"   Düşüş teyidi: {_price(candidate.confirmation_level)} altında günlük kapanış sürerse")
    lines.append(f"   Risk notu: {risk}")
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
        f"• Kapsam: {scope}",
        f"• Puan: {breadth.breadth_score}/100 • {breadth.regime}",
        f"• Yükselen/Düşen/Yatay: {breadth.advancers}/{breadth.decliners}/{breadth.unchanged} • Net genişlik: {breadth.net_breadth:+d}",
        f"• EMA20/50/200 üstü: %{breadth.above_ema20_ratio:.1f} / %{breadth.above_ema50_ratio:.1f} / {ema200}",
        f"• Yeni 20g zirve/dip: {breadth.new_20d_highs}/{breadth.new_20d_lows} • Hacim artışı: %{breadth.rising_volume_ratio:.1f}",
        f"• Long {breadth.long_count} • Short/Risk {breadth.short_count} • Nötr {breadth.neutral_count}",
        f"🔮 Açılış çerçevesi: {breadth.tomorrow_bias}",
    ]

    if breadth.long_candidates:
        lines.extend(["", "🟢 GÜÇLÜ LONG İZLEME"])
        for candidate in breadth.long_candidates[:6]:
            lines.extend(_candidate_lines(candidate, bullish=True))
    if breadth.short_candidates:
        lines.extend(["", "🔴 ZAYIF / SHORT-RİSK İZLEME"])
        for candidate in breadth.short_candidates[:6]:
            lines.extend(_candidate_lines(candidate, bullish=False))
    return lines
