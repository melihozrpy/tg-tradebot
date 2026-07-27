from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from app.fundamentals import FundamentalDataProvider, FundamentalSnapshot


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
    decision_summary: str = "Veri yeterliliği ve riskler birlikte değerlendirilmelidir."
    data_warnings: tuple[str, ...] = ()
    score_breakdown: tuple[str, ...] = ()
    data_coverage: int = 0
    currency: str = "TRY"


def _decision_summary(status: str, evidence_count: int) -> str:
    if evidence_count < 3:
        return (
            "Karar üretmek için doğrulanmış finansal kalem sayısı yetersiz. Yeni bilanço ve KAP bildirimleri "
            "gelmeden temel görünüm kesinleştirilemez."
        )
    if status == "GÜÇLÜ":
        return (
            "Büyüme, kârlılık, nakit ve borç göstergelerinin birlikte verdiği temel görünüm olumlu. "
            "Bu tek başına AL sinyali değildir; fiyatlama, sektör karşılaştırması, teknik teyit ve güncel KAP akışı gerekir."
        )
    if status == "RİSKLİ":
        return (
            "Borçluluk, marj, büyüme veya nakit üretimindeki zayıflıklar aşağı yönlü baskı riski oluşturuyor. "
            "Risk kalemleri düzelmeden yalnız düşük fiyat/çarpan gerekçesiyle alım sonucu çıkarılmamalı."
        )
    return (
        "Olumlu ve olumsuz finansal göstergeler dengede. Yön için yeni dönem sonuçları, sektör kıyası, "
        "değerleme ve güncel KAP haberleri birlikte izlenmeli."
    )


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _currency_label(currency: str) -> str:
    return "TL" if str(currency).upper() == "TRY" else str(currency).upper()


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


def _trend_line(label: str, values, currency: str = "TRY") -> str | None:
    if values is None or len(values) < 2:
        return None
    latest, previous = float(values.iloc[-1]), float(values.iloc[-2])
    change = ((latest / abs(previous)) - 1) * 100 if previous else 0.0
    return f"{label}: {latest / 1_000_000:.1f} mn {_currency_label(currency)} • çeyreklik %{change:+.1f}"


def _trend_line_pair(label: str, latest: Any, previous: Any, currency: str = "TRY") -> str | None:
    latest_value, previous_value = _number(latest), _number(previous)
    if latest_value is None or previous_value is None:
        return None
    change = ((latest_value / abs(previous_value)) - 1) * 100 if previous_value else 0.0
    return f"{label}: {latest_value / 1_000_000:.1f} mn {_currency_label(currency)} • dönemsel %{change:+.1f}"


def _analysis_from_snapshot(snapshot: "FundamentalSnapshot") -> CompanyAnalysis:
    from app.fundamentals.ratios import comparable_growth_period

    latest = snapshot.latest_period
    previous = comparable_growth_period(latest, snapshot.periods)
    calculated = snapshot.ratios.as_dict()
    metrics: dict[str, float | None] = {
        key: _number(value)
        for key, value in calculated.items()
    }
    metrics.update(
        {
            "market_cap": _number(latest.value("market_cap")),
            "total_debt": _number(latest.value("total_debt")),
            "total_cash": _number(latest.value("cash_and_equivalents")),
        }
    )
    trends = tuple(
        filter(
            None,
            (
                _trend_line_pair("Ciro", latest.value("revenue"), previous.value("revenue") if previous else None, latest.currency),
                _trend_line_pair("Net kâr", latest.value("net_income"), previous.value("net_income") if previous else None, latest.currency),
                _trend_line_pair("FAVÖK", latest.value("ebitda"), previous.value("ebitda") if previous else None, latest.currency),
                _trend_line_pair(
                    "Faaliyet nakdi",
                    latest.value("operating_cash_flow"),
                    previous.value("operating_cash_flow") if previous else None,
                    latest.currency,
                ),
            ),
        )
    )

    positives: list[str] = []
    risks: list[str] = []
    score_breakdown: list[str] = ["Başlangıç: 50 puan"]
    score = 50
    growth = metrics.get("revenue_growth")
    if growth is not None:
        (positives if growth > .10 else risks).append(f"Ciro büyümesi %{growth * 100:+.1f}")
        contribution = 12 if growth > .10 else -8 if growth < 0 else 0
        score += contribution
        score_breakdown.append(f"Ciro büyümesi: {contribution:+d}")
    margin = metrics.get("profit_margin")
    if margin is not None:
        (positives if margin > .08 else risks).append(f"Net kâr marjı %{margin * 100:.1f}")
        contribution = 10 if margin > .08 else -8 if margin < .02 else 0
        score += contribution
        score_breakdown.append(f"Net kâr marjı: {contribution:+d}")
    sector_text = f"{snapshot.sector or ''} {snapshot.industry or ''}".casefold()
    is_bank = any(token in sector_text for token in ("banka", "banking", "bank"))
    data_warnings: list[str] = []
    data_warnings.extend(snapshot.provenance.notes)
    debt = metrics.get("debt_to_equity")
    if debt is not None:
        if is_bank:
            data_warnings.append(
                "Banka bilançosunda sanayi şirketi Borç/Özsermaye eşiği kullanılmadı; bankacılık rasyoları ayrıca gerekir."
            )
            score_breakdown.append("Borç/özsermaye: banka için puanlanmadı")
        else:
            (risks if debt > 150 else positives).append(f"Borç/özsermaye %{debt:.1f}")
            contribution = -15 if debt > 150 else 8 if debt < 60 else 0
            score += contribution
            score_breakdown.append(f"Borç/özsermaye: {contribution:+d}")
    cash = metrics.get("free_cash_flow")
    if cash is not None:
        (positives if cash > 0 else risks).append(
            "Serbest nakit akışı pozitif" if cash > 0 else "Serbest nakit akışı negatif"
        )
        contribution = 10 if cash > 0 else -12
        score += contribution
        score_breakdown.append(f"Serbest nakit akışı: {contribution:+d}")
    roe = metrics.get("return_on_equity")
    if roe is not None:
        (positives if roe > .15 else risks).append(f"Özsermaye kârlılığı %{roe * 100:.1f}")
        contribution = 10 if roe > .15 else -5 if roe < .05 else 0
        score += contribution
        score_breakdown.append(f"Özsermaye kârlılığı: {contribution:+d}")
    evidence_count = sum(
        metrics.get(key) is not None
        for key in ("revenue_growth", "profit_margin", "debt_to_equity", "free_cash_flow", "return_on_equity")
    )
    score = max(0, min(100, score))
    status = (
        "VERİ YETERSİZ"
        if evidence_count < 3
        else "GÜÇLÜ" if score >= 70 else "DENGELİ" if score >= 50 else "RİSKLİ"
    )
    period_age = (date.today() - latest.period_end).days
    stale_limit = 550 if latest.period_type.value == "annual" else 220
    if period_age > stale_limit:
        data_warnings.append(f"Son finansal dönem {period_age} günlük; güncellik eşiğini aşıyor.")
    if not snapshot.provenance.source_url:
        data_warnings.append("Kaynak belge bağlantısı sağlanmadı.")

    valuation_lines: list[str] = []
    for label, key in (("F/K", "trailing_pe"), ("PD/DD", "price_to_book"), ("FD/FAVÖK", "enterprise_to_ebitda")):
        if metrics.get(key) is not None:
            valuation_lines.append(f"{label}: {metrics[key]:.2f}x")
    if metrics.get("net_debt") is not None:
        valuation_lines.append(f"Net borç: {metrics['net_debt'] / 1_000_000:.1f} mn {_currency_label(latest.currency)}")

    consolidation = "konsolide" if latest.consolidated is True else "solo" if latest.consolidated is False else "türü belirsiz"
    revision = latest.revision or "revizyon bilgisi yok"
    source = (
        f"{snapshot.provenance.provider} • {snapshot.provenance.trust.value} • "
        f"{latest.period_end.isoformat()} • {revision} • {consolidation} • {latest.currency}"
    )
    return CompanyAnalysis(
        symbol=snapshot.symbol,
        name=snapshot.company_name,
        sector=snapshot.sector or "Veri yok",
        industry=snapshot.industry or "Veri yok",
        summary=(snapshot.summary or "Şirket faaliyet özeti veri kaynağında bulunamadı.")[:700],
        status=status,
        score=score,
        positives=tuple(positives),
        risks=tuple(risks),
        metrics=metrics,
        quarterly_trends=trends,
        valuation_lines=tuple(valuation_lines),
        financial_period=latest.period_end.isoformat(),
        kap_url=f"https://www.kap.org.tr/tr/search/{quote(snapshot.symbol)}/1",
        source=source,
        decision_summary=_decision_summary(status, evidence_count),
        data_warnings=tuple(data_warnings),
        score_breakdown=tuple(score_breakdown),
        data_coverage=round(evidence_count / 5 * 100),
        currency=latest.currency,
    )


def analyze_company(
    symbol: str,
    ticker_factory=None,
    fundamental_provider: "FundamentalDataProvider | None" = None,
) -> CompanyAnalysis:
    import yfinance as yf

    normalized = symbol.strip().upper().removesuffix(".IS")
    if fundamental_provider is not None:
        return _analysis_from_snapshot(fundamental_provider.fetch(normalized))
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
        _trend_line("Ciro", revenue, str(info.get("financialCurrency") or "TRY")),
        _trend_line("Net kâr", net_income, str(info.get("financialCurrency") or "TRY")),
        _trend_line("FAVÖK", ebitda, str(info.get("financialCurrency") or "TRY")),
        _trend_line("Faaliyet nakdi", operating_cash, str(info.get("financialCurrency") or "TRY")),
    )))
    periods = []
    for frame in (income, balance, cashflow):
        if frame is not None:
            periods.extend(frame.columns)
    financial_period = max(periods).strftime("%Y-%m-%d") if periods else None
    positives: list[str] = []; risks: list[str] = []; score = 50
    score_breakdown: list[str] = ["Başlangıç: 50 puan"]
    growth = metrics["revenue_growth"]
    if growth is not None:
        (positives if growth > .10 else risks).append(f"Ciro büyümesi %{growth * 100:+.1f}")
        contribution = 12 if growth > .10 else -8 if growth < 0 else 0
        score += contribution
        score_breakdown.append(f"Ciro büyümesi: {contribution:+d}")
    margin = metrics["profit_margin"]
    if margin is not None:
        (positives if margin > .08 else risks).append(f"Net kâr marjı %{margin * 100:.1f}")
        contribution = 10 if margin > .08 else -8 if margin < .02 else 0
        score += contribution
        score_breakdown.append(f"Net kâr marjı: {contribution:+d}")
    debt = metrics["debt_to_equity"]
    if debt is not None:
        (risks if debt > 150 else positives).append(f"Borç/özsermaye %{debt:.1f}")
        contribution = -15 if debt > 150 else 8 if debt < 60 else 0
        score += contribution
        score_breakdown.append(f"Borç/özsermaye: {contribution:+d}")
    cash = metrics["free_cash_flow"]
    if cash is not None:
        (positives if cash > 0 else risks).append("Serbest nakit akışı pozitif" if cash > 0 else "Serbest nakit akışı negatif")
        contribution = 10 if cash > 0 else -12
        score += contribution
        score_breakdown.append(f"Serbest nakit akışı: {contribution:+d}")
    roe = metrics["return_on_equity"]
    if roe is not None:
        (positives if roe > .15 else risks).append(f"Özsermaye kârlılığı %{roe * 100:.1f}")
        contribution = 10 if roe > .15 else -5 if roe < .05 else 0
        score += contribution
        score_breakdown.append(f"Özsermaye kârlılığı: {contribution:+d}")
    evidence_count = sum(
        metrics.get(key) is not None
        for key in ("revenue_growth", "profit_margin", "debt_to_equity", "free_cash_flow", "return_on_equity")
    )
    score = max(0, min(100, score))
    status = (
        "VERİ YETERSİZ"
        if evidence_count < 3
        else "GÜÇLÜ" if score >= 70 else "DENGELİ" if score >= 50 else "RİSKLİ"
    )
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
        decision_summary=_decision_summary(status, evidence_count),
        data_warnings=("Yahoo verisi gecikmeli/ikincil kaynaktır; KAP veya lisanslı kaynakla doğrulanmalıdır.",),
        score_breakdown=tuple(score_breakdown),
        data_coverage=round(evidence_count / 5 * 100),
        currency=str(info.get("financialCurrency") or info.get("currency") or "TRY").upper(),
    )


def format_company_analysis(result: CompanyAnalysis) -> str:
    positives = "\n".join(f"• {item}" for item in result.positives) or "• Doğrulanmış güçlü veri yok"
    risks = "\n".join(f"• {item}" for item in result.risks) or "• Belirgin finansal risk işaretlenmedi"
    trends = "\n".join(f"• {item}" for item in result.quarterly_trends) or "• Çeyreklik tablo verisi bulunamadı"
    valuations = "\n".join(f"• {item}" for item in result.valuation_lines) or "• Çarpan verisi bulunamadı"
    period = result.financial_period or "veri kaynağında yok"
    warnings = "\n".join(f"• {item}" for item in result.data_warnings) or "• Ek veri kalite uyarısı yok"
    score_lines = "\n".join(f"• {item}" for item in result.score_breakdown) or "• Puan bileşeni üretilemedi"
    return (
        f"🏢 {result.name} ({result.symbol})\n"
        f"Sektör: {result.sector} / {result.industry}\n\n"
        f"📌 TEMEL GÖRÜNÜM: {result.status} ({result.score}/100)\n"
        f"Veri kapsamı: %{result.data_coverage}\n"
        "Bu puan AL olasılığı değil, açıklanabilir finansal kalite puanıdır.\n\n"
        f"Şirket ne yapıyor?\n{result.summary}\n\n"
        f"✅ Yükselişi destekleyebilecek veriler\n{positives}\n\n"
        f"⚠️ Baskı yaratabilecek riskler\n{risks}\n\n"
        f"📊 SON ÇEYREK DEĞİŞİMLERİ\n{trends}\n\n"
        f"💰 DEĞERLEME VE BORÇLULUK\n{valuations}\n\n"
        f"🧮 PUAN NEREDEN GELDİ?\n{score_lines}\n\n"
        f"🧭 NE ANLAMA GELİYOR?\n{result.decision_summary}\n\n"
        f"🔎 VERİ KALİTESİ\n{warnings}\n\n"
        f"🗓️ Son finansal dönem: {period}\n"
        f"Kaynak: {result.source}\n\n"
        f"🔗 Resmî KAP araması: {result.kap_url}\n\n"
        "Not: Kaynak ne kadar güçlü olursa olsun bu değerlendirme kesin al/sat kararı veya fiyat yönü garantisi değildir."
    )
