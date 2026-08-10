"""Compact, decision-oriented presentation helpers for daily BIST reports."""

from __future__ import annotations

from app.services.market_breadth_service import BreadthCandidate, MarketBreadthResult


def _price(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _candidate_lines(candidate: BreadthCandidate, *, bullish: bool) -> list[str]:
    reasons = " • ".join(candidate.reasons[:3]) or "Teknik teyitler izleniyor"
    target = _price(candidate.technical_target)
    if bullish:
        condition = f"{_price(candidate.confirmation_level)} üstünde günlük kapanış korunursa"
        title = "🟢"
    else:
        condition = f"{_price(candidate.confirmation_level)} altında günlük kapanış sürerse"
        title = "🔴"
    return [
        f"{title} {candidate.symbol}  •  Güç {candidate.score}/100  •  %{candidate.change_percent:+.1f}",
        f"   Neden: {reasons}",
        f"   Koşul: {condition}  →  Teknik hedef: {target} ({candidate.target_basis})",
    ]


def format_breadth_panel(breadth: MarketBreadthResult, *, report_kind: str) -> list[str]:
    """Render broad market data as a readable panel, not a symbol wall.

    The levels are observation-based technical reference levels.  They are
    deliberately described as conditional targets, never as direct entries or
    return promises.
    """

    scope = f"{breadth.scanned}/{breadth.universe_size} hisse"
    title = "AÇILIŞ ÖNCESİ GENİŞLİK" if report_kind == "morning" else "KAPANIŞ GENİŞLİĞİ"
    lines = [
        "",
        f"╔═ 🌐 BIST • {title} ═╗",
        f"║ Kapsam: {scope}  |  Piyasa gücü: {breadth.breadth_score}/100 • {breadth.regime}",
        f"║ Yükselen {breadth.advancers}  •  Düşen {breadth.decliners}  •  Yatay {breadth.unchanged}  |  Net {breadth.net_breadth:+d}",
        f"║ Trend: EMA20 %{breadth.above_ema20_ratio:.1f}  •  EMA50 %{breadth.above_ema50_ratio:.1f}  •  "
        + (f"EMA200 %{breadth.above_ema200_ratio:.1f}" if breadth.above_ema200_ratio is not None else "EMA200 veri yetersiz"),
        f"║ Yeni 20g zirve/dip: {breadth.new_20d_highs}/{breadth.new_20d_lows}  •  Hacim artışı: %{breadth.rising_volume_ratio:.1f}",
        f"║ Dağılım: {breadth.long_count} güçlü-yukarı  •  {breadth.short_count} zayıf-aşağı  •  {breadth.neutral_count} nötr",
        f"╚═ Sonraki seans çerçevesi: {breadth.tomorrow_bias} ═╝",
    ]

    if breadth.long_candidates:
        lines.extend(["", "🟢 GÜÇLÜ LONG İZLEME — neden / koşul / hedef"])
        for candidate in breadth.long_candidates[:3]:
            lines.extend(_candidate_lines(candidate, bullish=True))
    if breadth.short_candidates:
        lines.extend(["", "🔴 ZAYIFLIK / KORUMA İZLEME — neden / koşul / hedef"])
        for candidate in breadth.short_candidates[:3]:
            lines.extend(_candidate_lines(candidate, bullish=False))
    return lines
