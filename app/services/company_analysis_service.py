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
    quarterly_trends: tuple[str, ...]
    valuation_lines: tuple[str, ...]
    financial_period: str | None
    kap_url: str
    source: str = "Yahoo Finance (gecikmeli/ikincil kaynak)"


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _statement(ticker, *names: str):
    for name in names:
        value = getattr(ticker, name, None)
        if value is not None and not getattr(value, "empty", True):
            return value
    return None


def _row(frame, *labels: str):
    if frame is None:
        return None
    for label in labels:
        if label in frame.index:
            return frame.loc[label].dropna().sort_index()
    return None


def _trend_line(label: str, values) -> str | None:
    if values is None or len(values) < 2:
        return None
    latest, previous = float(values.iloc[-1]), float(values.iloc[-2])
    change = ((latest / abs(previous)) - 1) * 100 if previous else 0.0
    return f"{label}: {latest / 1_000_000:.1f} mn TL • çeyreklik %{change:+.1f}"


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
        "gross_margin": _number(info.get("grossMargins")),
        "operating_margin": _number(info.get("operatingMargins")),
        "enterprise_to_ebitda": _number(info.get("enterpriseToEbitda")),
        "price_to_book": _number(info.get("priceToBook")),
        "market_cap": _number(info.get("marketCap")),
        "total_debt": _number(info.get("totalDebt")),
        "total_cash": _number(info.get("totalCash")),
    }
    income = _statement(ticker, "quarterly_income_stmt", "quarterly_financials")
    balance = _statement(ticker, "quarterly_balance_sheet")
    cashflow = _statement(ticker, "quarterly_cashflow")
    revenue = _row(income, "Total Revenue", "Operating Revenue")
    net_income = _row(income, "Net Income", "Net Income Common Stockholders")
    ebitda = _row(income, "EBITDA", "Normalized EBITDA")
    operating_cash = _row(cashflow, "Operating Cash Flow", "Total Cash From Operating Activities")
    trends = tuple(filter(None, (
        _trend_line("Ciro", revenue), _trend_line("Net kâr", net_income),
        _trend_line("FAVÖK", ebitda), _trend_line("Faaliyet nakdi", operating_cash),
    )))
    periods = []
    for frame in (income, balance, cashflow):
        if frame is not None:
            periods.extend(frame.columns)
    financial_period = max(periods).strftime("%Y-%m-%d") if periods else None
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
    valuation_lines = []
    for label, key, suffix in (("F/K", "trailing_pe", "x"), ("PD/DD", "price_to_book", "x"),
                               ("FD/FAVÖK", "enterprise_to_ebitda", "x")):
        if metrics[key] is not None:
            valuation_lines.append(f"{label}: {metrics[key]:.2f}{suffix}")
    debt_value, cash_value = metrics["total_debt"], metrics["total_cash"]
    if debt_value is not None and cash_value is not None:
        valuation_lines.append(f"Net borç: {(debt_value - cash_value) / 1_000_000:.1f} mn TL")
    return CompanyAnalysis(
        symbol=normalized, name=info.get("longName") or normalized,
        sector=info.get("sector") or "Veri yok", industry=info.get("industry") or "Veri yok",
        summary=(info.get("longBusinessSummary") or "Şirket faaliyet özeti veri kaynağında bulunamadı.")[:700],
        status=status, score=score, positives=tuple(positives), risks=tuple(risks), metrics=metrics,
        quarterly_trends=trends, valuation_lines=tuple(valuation_lines), financial_period=financial_period,
        kap_url=f"https://www.kap.org.tr/tr/search/{quote(normalized)}/1",
    )


def format_company_analysis(result: CompanyAnalysis) -> str:
    positives = "\n".join(f"• {item}" for item in result.positives) or "• Doğrulanmış güçlü veri yok"
    risks = "\n".join(f"• {item}" for item in result.risks) or "• Belirgin finansal risk işaretlenmedi"
    trends = "\n".join(f"• {item}" for item in result.quarterly_trends) or "• Çeyreklik tablo verisi bulunamadı"
    valuations = "\n".join(f"• {item}" for item in result.valuation_lines) or "• Çarpan verisi bulunamadı"
    period = result.financial_period or "veri kaynağında yok"
    return (
        f"🏢 {result.name} ({result.symbol})\n"
        f"Sektör: {result.sector} / {result.industry}\n\n"
        f"📌 TEMEL GÖRÜNÜM: {result.status} ({result.score}/100)\n\n"
        f"Şirket ne yapıyor?\n{result.summary}\n\n"
        f"✅ Yükselişi destekleyebilecek veriler\n{positives}\n\n"
        f"⚠️ Baskı yaratabilecek riskler\n{risks}\n\n"
        f"📊 SON ÇEYREK DEĞİŞİMLERİ\n{trends}\n\n"
        f"💰 DEĞERLEME VE BORÇLULUK\n{valuations}\n\n"
        f"🗓️ Son finansal dönem: {period}\n"
        f"Kaynak: {result.source}\n\n"
        f"🔗 Resmî KAP araması: {result.kap_url}\n\n"
        "Not: Bu değerlendirme eksik/gecikmeli ikincil veriye dayanabilir; kesin al/sat kararı değildir."
    )
