from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.fundamentals.models import FinancialRatios, PeriodType, StatementPeriod


ZERO = Decimal("0")
HUNDRED = Decimal("100")


def _divide(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator in (None, ZERO):
        return None
    try:
        return numerator / denominator
    except (InvalidOperation, ZeroDivisionError):
        return None


def _growth(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    if current is None or previous in (None, ZERO):
        return None
    return (current / abs(previous)) - Decimal("1")


def _profit_base(period: StatementPeriod, regular_key: str, ttm_key: str) -> Decimal | None:
    return period.value(ttm_key) or (
        period.value(regular_key)
        if period.period_type in {PeriodType.ANNUAL, PeriodType.TTM}
        else None
    )


def comparable_growth_period(
    latest: StatementPeriod,
    periods: tuple[StatementPeriod, ...] | list[StatementPeriod],
) -> StatementPeriod | None:
    """Return a financially comparable prior period, never an arbitrary row.

    Annual/TTM values can be compared with the previous same-basis record.
    Interim values require a known statement duration and the matching fiscal
    period from the prior year.  If a provider does not disclose whether its
    flow values are 3M, 6M or 9M, growth remains unavailable instead of treating
    cumulative YTD statements as standalone quarters.
    """

    compatible = [
        item
        for item in periods
        if item.period_end < latest.period_end
        and item.period_type == latest.period_type
        and item.currency == latest.currency
        and item.consolidated == latest.consolidated
    ]
    if latest.period_type in {PeriodType.ANNUAL, PeriodType.TTM}:
        return max(compatible, key=lambda item: item.period_end, default=None)
    if latest.period_type != PeriodType.QUARTERLY or latest.duration_months is None:
        return None
    prior_year = [
        item
        for item in compatible
        if item.duration_months == latest.duration_months
        and item.period_end.year == latest.period_end.year - 1
        and item.period_end.month == latest.period_end.month
    ]
    return max(prior_year, key=lambda item: item.period_end, default=None)


def calculate_ratios(periods: tuple[StatementPeriod, ...]) -> FinancialRatios:
    """Calculate ratios solely from normalized raw values.

    Quarterly profit is not silently annualized for P/E, EV/EBITDA or ROE. Those
    ratios stay unavailable unless TTM or annual data exists.
    """

    if not periods:
        return FinancialRatios()
    ordered = sorted(periods, key=lambda item: item.period_end, reverse=True)
    latest = ordered[0]
    previous = comparable_growth_period(latest, ordered[1:])
    current_revenue = latest.value("revenue")
    current_net_income = latest.value("net_income")
    previous_revenue = previous.value("revenue") if previous else None
    previous_net_income = previous.value("net_income") if previous else None

    revenue_for_margin = latest.value("revenue_ttm") or current_revenue
    income_for_margin = latest.value("net_income_ttm") or current_net_income
    gross_profit = latest.value("gross_profit_ttm") or latest.value("gross_profit")
    operating_income = latest.value("operating_income_ttm") or latest.value("operating_income")

    total_debt = latest.value("total_debt")
    cash = latest.value("cash_and_equivalents")
    equity = latest.value("total_equity")
    previous_equity = previous.value("total_equity") if previous else None
    average_equity = None
    if equity is not None:
        average_equity = (equity + previous_equity) / Decimal("2") if previous_equity is not None else equity

    operating_cash = latest.value("operating_cash_flow")
    capital_expenditure = latest.value("capital_expenditure")
    free_cash_flow = None
    if operating_cash is not None and capital_expenditure is not None:
        free_cash_flow = operating_cash - abs(capital_expenditure)
    elif latest.value("free_cash_flow") is not None:
        free_cash_flow = latest.value("free_cash_flow")

    market_cap = latest.value("market_cap")
    enterprise_value = latest.value("enterprise_value")
    net_debt = total_debt - cash if total_debt is not None and cash is not None else None
    if enterprise_value is None and market_cap is not None and net_debt is not None:
        enterprise_value = market_cap + net_debt

    annual_income = _profit_base(latest, "net_income", "net_income_ttm")
    annual_ebitda = _profit_base(latest, "ebitda", "ebitda_ttm")

    return FinancialRatios(
        revenue_growth=_growth(current_revenue, previous_revenue),
        earnings_growth=_growth(current_net_income, previous_net_income),
        profit_margin=_divide(income_for_margin, revenue_for_margin),
        gross_margin=_divide(gross_profit, revenue_for_margin),
        operating_margin=_divide(operating_income, revenue_for_margin),
        debt_to_equity=(
            _divide(total_debt, equity) * HUNDRED
            if _divide(total_debt, equity) is not None
            else None
        ),
        current_ratio=_divide(latest.value("current_assets"), latest.value("current_liabilities")),
        free_cash_flow=free_cash_flow,
        return_on_equity=_divide(annual_income, average_equity),
        trailing_pe=_divide(market_cap, annual_income),
        price_to_book=_divide(market_cap, equity),
        enterprise_to_ebitda=_divide(enterprise_value, annual_ebitda),
        net_debt=net_debt,
    )
