from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from app.fundamentals.base import FundamentalDataProvider, ProviderResponseError
from app.fundamentals.models import FundamentalSnapshot, SourceTrust
from app.fundamentals.normalizer import normalize_symbol, snapshot_from_payload


_STATEMENT_ROWS: dict[str, tuple[str, ...]] = {
    "revenue": ("Total Revenue", "Operating Revenue"),
    "net_income": ("Net Income", "Net Income Common Stockholders"),
    "gross_profit": ("Gross Profit",),
    "operating_income": ("Operating Income",),
    "ebitda": ("EBITDA", "Normalized EBITDA"),
    "operating_cash_flow": ("Operating Cash Flow", "Total Cash From Operating Activities"),
    "capital_expenditure": ("Capital Expenditure", "Capital Expenditures"),
    "free_cash_flow": ("Free Cash Flow",),
    "total_assets": ("Total Assets",),
    "total_liabilities": ("Total Liabilities Net Minority Interest", "Total Liab"),
    "total_equity": ("Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"),
    "total_debt": ("Total Debt",),
    "cash_and_equivalents": ("Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"),
    "current_assets": ("Current Assets", "Total Current Assets"),
    "current_liabilities": ("Current Liabilities", "Total Current Liabilities"),
}


def _statement(ticker: Any, *names: str) -> Any | None:
    for name in names:
        try:
            frame = getattr(ticker, name, None)
        except Exception:
            continue
        if frame is not None and not getattr(frame, "empty", True):
            return frame
    return None


def _mapping_attribute(ticker: Any, name: str) -> dict[str, Any]:
    try:
        value = getattr(ticker, name, None)
        return dict(value or {})
    except Exception:
        return {}


def _cell(frame: Any, column: Any, labels: tuple[str, ...]) -> Any | None:
    if frame is None or column not in frame.columns:
        return None
    for label in labels:
        if label in frame.index:
            value = frame.at[label, column]
            try:
                if value != value:  # NaN
                    continue
            except (TypeError, ValueError):
                pass
            return value
    return None


class YahooFundamentalProvider(FundamentalDataProvider):
    """Normalize Yahoo Finance as an explicitly secondary, delayed fallback."""

    name = "yahoo_finance"

    def __init__(self, ticker_factory=None) -> None:
        self.ticker_factory = ticker_factory

    def fetch(self, symbol: str) -> FundamentalSnapshot:
        normalized = normalize_symbol(symbol)
        if self.ticker_factory is None:
            import yfinance as yf

            ticker_factory = yf.Ticker
        else:
            ticker_factory = self.ticker_factory
        try:
            ticker = ticker_factory(f"{normalized}.IS")
        except Exception as exc:
            raise ProviderResponseError("Yahoo temel veri bağlantısı başarısız.") from exc

        # Yahoo'nun profil uç noktası zaman zaman tek başına hata verirken
        # finansal tabloları çalışmaya devam eder. Profil hatası tablo verisini
        # çöpe atmamalı; her yüzey bağımsız ve kontrollü okunur.
        info = _mapping_attribute(ticker, "info")
        fast_info = _mapping_attribute(ticker, "fast_info")

        income = _statement(ticker, "quarterly_income_stmt", "quarterly_financials")
        balance = _statement(ticker, "quarterly_balance_sheet")
        cashflow = _statement(ticker, "quarterly_cashflow")
        columns: set[Any] = set()
        for frame in (income, balance, cashflow):
            if frame is not None:
                columns.update(frame.columns)
        ordered_columns = sorted(columns, reverse=True)
        currency = str(info.get("financialCurrency") or info.get("currency") or "TRY").upper()
        quote_currency = str(info.get("currency") or fast_info.get("currency") or "TRY").upper()
        notes = ["Yahoo Finance gecikmeli/ikincil kaynaktır; KAP ile doğrulanmalıdır."]
        if not info.get("financialCurrency") and not info.get("currency") and not fast_info.get("currency"):
            notes.append("Para birimi BIST sembolü nedeniyle TRY varsayıldı.")
        if not info:
            notes.append("Şirket profil uç noktası yanıt vermedi; mevcut finansal tablolar kullanıldı.")
        market_values_compatible = currency == quote_currency
        if not market_values_compatible:
            notes.append(
                f"Finansal tablo para birimi {currency}, borsa fiyatı para birimi {quote_currency}; "
                "kur dönüşümü olmadan F/K, PD/DD ve FD/FAVÖK hesaplanmadı."
            )

        periods: list[dict[str, Any]] = []
        for index, column in enumerate(ordered_columns):
            values: dict[str, Any] = {}
            for canonical, labels in _STATEMENT_ROWS.items():
                for frame in (income, balance, cashflow):
                    value = _cell(frame, column, labels)
                    if value is not None:
                        values[canonical] = value
                        break
            if index == 0:
                market_fields = {
                    "market_cap": (
                        info.get("marketCap") or fast_info.get("market_cap")
                    ) if market_values_compatible else None,
                    "enterprise_value": info.get("enterpriseValue") if market_values_compatible else None,
                    "shares_outstanding": info.get("sharesOutstanding") or info.get("impliedSharesOutstanding"),
                    "last_price": (
                        info.get("currentPrice")
                        or info.get("regularMarketPrice")
                        or fast_info.get("last_price")
                    ) if market_values_compatible else None,
                    "revenue_ttm": info.get("totalRevenue"),
                    "net_income_ttm": info.get("netIncomeToCommon"),
                    "ebitda_ttm": info.get("ebitda"),
                }
                for key, value in market_fields.items():
                    if value is not None:
                        values[key] = value
                if "total_debt" not in values and info.get("totalDebt") is not None:
                    values["total_debt"] = info["totalDebt"]
                if "cash_and_equivalents" not in values and info.get("totalCash") is not None:
                    values["cash_and_equivalents"] = info["totalCash"]
            if values:
                period_date = column.to_pydatetime() if hasattr(column, "to_pydatetime") else column
                periods.append(
                    {
                        "period_end": period_date,
                        "period_type": "quarterly",
                        "revision": None,
                        "consolidated": None,
                        "currency": currency,
                        "duration_months": 3,
                        "flow_basis": "discrete",
                        "values": values,
                    }
                )

        if not periods:
            latest_raw = info.get("mostRecentQuarter") or info.get("lastFiscalYearEnd")
            if latest_raw:
                latest_date = datetime.fromtimestamp(float(latest_raw), tz=timezone.utc)
                values = {
                    key: value
                    for key, value in {
                        "revenue_ttm": info.get("totalRevenue"),
                        "net_income_ttm": info.get("netIncomeToCommon"),
                        "ebitda_ttm": info.get("ebitda"),
                        "total_debt": info.get("totalDebt"),
                        "cash_and_equivalents": info.get("totalCash"),
                    "market_cap": info.get("marketCap") if market_values_compatible else None,
                    "enterprise_value": info.get("enterpriseValue") if market_values_compatible else None,
                    }.items()
                    if value is not None
                }
                periods.append(
                    {
                        "period_end": latest_date,
                        "period_type": "ttm",
                        "revision": None,
                        "consolidated": None,
                        "currency": currency,
                        "values": values,
                    }
                )
        if not periods:
            raise ProviderResponseError("Yahoo finansal dönem/tablo verisi döndürmedi.")

        payload: Mapping[str, Any] = {
            "symbol": normalized,
            "company": {
                "name": info.get("longName") or info.get("shortName") or normalized,
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "summary": info.get("longBusinessSummary"),
            },
            "currency": currency,
            "periods": periods,
        }
        return snapshot_from_payload(
            payload,
            provider=self.name,
            trust=SourceTrust.SECONDARY,
            requested_symbol=normalized,
            default_source_url=f"https://finance.yahoo.com/quote/{normalized}.IS",
            notes=tuple(notes),
        )
