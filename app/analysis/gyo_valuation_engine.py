from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

from app.utils.financial_formatter import finite_float, round_money


@dataclass
class GYOValuationResult:
    symbol: str
    applicable: bool
    classification: str
    current_market_cap: Optional[float] = None
    shares_outstanding: Optional[float] = None
    book_value_per_share: Optional[float] = None
    net_asset_value: Optional[float] = None
    nav_per_share: Optional[float] = None
    market_cap_to_nav: Optional[float] = None
    nav_discount_premium_percent: Optional[float] = None
    total_assets: Optional[float] = None
    total_debt: Optional[float] = None
    net_debt: Optional[float] = None
    financing_expenses: Optional[float] = None
    rental_income: Optional[float] = None
    property_portfolio_value: Optional[float] = None
    latest_profit_loss: Optional[float] = None
    equity_change_percent: Optional[float] = None
    financial_period_date: Optional[date] = None
    data_is_stale: bool = False
    warnings: list[str] = field(default_factory=list)
    source_fields: list[str] = field(default_factory=list)


def is_gyo_company(symbol: str, *, sector_name: Optional[str] = None, company_type: Optional[str] = None) -> bool:
    text = " ".join(filter(None, (sector_name, company_type))).casefold()
    normalized = text.replace("ı", "i").replace("ğ", "g").replace("ö", "o").replace("ü", "u").replace("ş", "s").replace("ç", "c")
    return "gayrimenkul yatirim ortakligi" in normalized or normalized.strip() == "gyo" or symbol.upper().endswith("GYO")


def _pick(data: dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = finite_float(data.get(key))
        if value is not None:
            return value
    return None


def _period_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        try:
            return value.date()
        except AttributeError:
            return None


def evaluate_gyo_valuation(
    symbol: str,
    current_price: float,
    fundamentals: Optional[dict[str, Any]],
    *,
    sector_name: Optional[str] = None,
    company_type: Optional[str] = None,
    as_of: Optional[datetime] = None,
) -> GYOValuationResult:
    symbol = symbol.upper()
    if not is_gyo_company(symbol, sector_name=sector_name, company_type=company_type):
        return GYOValuationResult(
            symbol, False, "Uygulanamaz",
            warnings=["GYO olmayan şirkete GYO değerleme formülü uygulanmadı."],
        )
    data = fundamentals or {}
    shares = _pick(data, "shares_outstanding", "total_shares", "issued_capital")
    market_cap = _pick(data, "market_cap", "current_market_cap")
    if market_cap is None and shares and current_price > 0:
        market_cap = float(current_price) * shares
    equity = _pick(data, "total_equity", "equity", "shareholders_equity")
    portfolio = _pick(data, "property_portfolio_value", "real_estate_portfolio_value")
    net_debt = _pick(data, "net_debt")
    total_debt = _pick(data, "total_debt", "total_liabilities")
    cash = _pick(data, "cash_and_equivalents", "cash")
    if net_debt is None and total_debt is not None and cash is not None:
        net_debt = total_debt - cash
    nav = _pick(data, "net_asset_value", "nav")
    if nav is None and portfolio is not None and net_debt is not None:
        nav = portfolio - net_debt
    book_per_share = equity / shares if equity is not None and shares and shares > 0 else None
    nav_per_share = nav / shares if nav is not None and shares and shares > 0 else None
    ratio = market_cap / nav if market_cap is not None and nav is not None and nav > 0 else None
    discount = (1 - ratio) * 100 if ratio is not None else None
    if ratio is None:
        classification = "Veri yetersiz"
    elif ratio <= 0.50:
        classification = "Çok iskontolu"
    elif ratio <= 0.80:
        classification = "İskontolu"
    elif ratio <= 1.20:
        classification = "Makul"
    elif ratio <= 1.50:
        classification = "Primli"
    else:
        classification = "Aşırı primli"

    period = _period_date(data.get("financial_period_date") or data.get("period_date") or data.get("as_of"))
    now = (as_of or datetime.now(timezone.utc)).date()
    stale = bool(period and (now - period).days > 180)
    warnings: list[str] = []
    if not data or data.get("status") == "unavailable":
        warnings.append("Veri bulunamadı; temel değerleme yapılamadı.")
    if ratio is None:
        warnings.append("Net aktif değer veya piyasa değeri eksik; sınıflandırma üretilemedi.")
    if stale:
        warnings.append("Veri eski; son finansal dönem 180 günden daha eski.")
    previous_equity = _pick(data, "previous_equity")
    equity_change = ((equity - previous_equity) / abs(previous_equity) * 100) if equity is not None and previous_equity not in (None, 0) else None
    return GYOValuationResult(
        symbol=symbol, applicable=True, classification=classification,
        current_market_cap=round_money(market_cap), shares_outstanding=shares,
        book_value_per_share=round_money(book_per_share), net_asset_value=round_money(nav),
        nav_per_share=round_money(nav_per_share),
        market_cap_to_nav=round(ratio, 4) if ratio is not None else None,
        nav_discount_premium_percent=round(discount, 2) if discount is not None else None,
        total_assets=_pick(data, "total_assets"), total_debt=total_debt, net_debt=net_debt,
        financing_expenses=_pick(data, "financing_expenses", "finance_costs"),
        rental_income=_pick(data, "rental_income"), property_portfolio_value=portfolio,
        latest_profit_loss=_pick(data, "net_income", "latest_profit_loss"),
        equity_change_percent=round(equity_change, 2) if equity_change is not None else None,
        financial_period_date=period, data_is_stale=stale, warnings=warnings,
        source_fields=[key for key, value in data.items() if value is not None and key != "status"],
    )


def collect_fundamental_payload(provider, symbol: str) -> dict[str, Any]:
    """Mevcut provider alanlarını birleştirir; değer uydurmaz."""
    merged: dict[str, Any] = {}
    for method_name in ("get_balance_sheet", "get_income_statement", "get_cash_flow", "get_ratios", "get_quarterly_growth"):
        method = getattr(provider, method_name, None)
        if not callable(method):
            continue
        try:
            payload = method(symbol) or {}
            if payload.get("status") != "unavailable":
                merged.update(payload)
        except Exception:  # noqa: BLE001 - kısmi temel veri desteklenir
            continue
    return merged or {"status": "unavailable", "symbol": symbol.upper()}
