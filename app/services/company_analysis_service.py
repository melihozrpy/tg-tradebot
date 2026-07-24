from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote


@dataclass(frozen=True)
class CompanyAnalysis:
    symbol: str
    name: str
    sector: str
    industry: str
    summary: str
    status: str
    score: int
    positives: tuple[str, ...]
    risks: tuple[str, ...]
    metrics: dict[str, float | None]
    kap_url: str
    source: str = "Yahoo Finance (gecikmeli/ikincil kaynak)"


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def analyze_company(symbol: str, ticker_factory=None) -> CompanyAnalysis:
    import yfinance as yf

    normalized = symbol.strip().upper().removesuffix(".IS")
    ticker = (ticker_factory or yf.Ticker)(f"{normalized}.IS")
    info = dict(getattr(ticker, "info", {}) or {})
    if not info:
        raise ValueError("Şirket temel verisi alınamadı.")
    metrics = {
        "revenue_growth": _number(info.get("revenueGrowth")),
        "earnings_growth": _number(info.get("earningsGrowth")),
        "profit_margin": _number(info.get("profitMargins")),
        "debt_to_equity": _number(info.get("debtToEquity")),
        "current_ratio": _number(info.get("currentRatio")),
        "free_cash_flow": _number(info.get("freeCashflow")),
        "trailing_pe": _number(info.get("trailingPE")),
        "return_on_equity": _number(info.get("returnOnEquity")),
    }
    positives: list[str] = []; risks: list[str] = []; score = 50
    growth = metrics["revenue_growth"]
    if growth is not None:
        (positives if growth > .10 else risks).append(f"Ciro büyümesi %{growth * 100:+.1f}")
        score += 12 if growth > .10 else -8 if growth < 0 else 0
    margin = metrics["profit_margin"]
    if margin is not None:
        (positives if margin > .08 else risks).append(f"Net kâr marjı %{margin * 100:.1f}")
        score += 10 if margin > .08 else -8 if margin < .02 else 0
    debt = metrics["debt_to_equity"]
    if debt is not None:
        (risks if debt > 150 else positives).append(f"Borç/özsermaye %{debt:.1f}")
        score += -15 if debt > 150 else 8 if debt < 60 else 0
    cash = metrics["free_cash_flow"]
    if cash is not None:
        (positives if cash > 0 else risks).append("Serbest nakit akışı pozitif" if cash > 0 else "Serbest nakit akışı negatif")
        score += 10 if cash > 0 else -12
    roe = metrics["return_on_equity"]
    if roe is not None:
        (positives if roe > .15 else risks).append(f"Özsermaye kârlılığı %{roe * 100:.1f}")
        score += 10 if roe > .15 else -5 if roe < .05 else 0
    score = max(0, min(100, score))
    status = "GÜÇLÜ" if score >= 70 else "DENGELİ" if score >= 50 else "RİSKLİ"
    return CompanyAnalysis(
        symbol=normalized, name=info.get("longName") or normalized,
        sector=info.get("sector") or "Veri yok", industry=info.get("industry") or "Veri yok",
        summary=(info.get("longBusinessSummary") or "Şirket faaliyet özeti veri kaynağında bulunamadı.")[:700],
        status=status, score=score, positives=tuple(positives), risks=tuple(risks), metrics=metrics,
        kap_url=f"https://www.kap.org.tr/tr/search/{quote(normalized)}/1",
    )


def format_company_analysis(result: CompanyAnalysis) -> str:
    positives = "\n".join(f"• {item}" for item in result.positives) or "• Doğrulanmış güçlü veri yok"
    risks = "\n".join(f"• {item}" for item in result.risks) or "• Belirgin finansal risk işaretlenmedi"
    return (
        f"🏢 {result.name} ({result.symbol})\n"
        f"Sektör: {result.sector} / {result.industry}\n\n"
        f"📌 TEMEL GÖRÜNÜM: {result.status} ({result.score}/100)\n\n"
        f"Şirket ne yapıyor?\n{result.summary}\n\n"
        f"✅ Yükselişi destekleyebilecek veriler\n{positives}\n\n"
        f"⚠️ Baskı yaratabilecek riskler\n{risks}\n\n"
        f"🔗 Resmî KAP araması: {result.kap_url}\n\n"
        "Not: Bu değerlendirme eksik/gecikmeli ikincil veriye dayanabilir; kesin al/sat kararı değildir."
    )
