from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from app.fundamentals.base import ProviderResponseError
from app.fundamentals.models import (
    DataProvenance,
    FundamentalSnapshot,
    PeriodType,
    SourceTrust,
    StatementPeriod,
)
from app.fundamentals.ratios import calculate_ratios


_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,12}$")


def normalize_symbol(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper().removesuffix(".IS")
    if not _SYMBOL_RE.fullmatch(normalized):
        raise ProviderResponseError("Geçersiz BIST sembolü.")
    return normalized


def _key(value: Any) -> str:
    text = str(value or "").strip().lower().translate(
        str.maketrans({"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g"})
    )
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text)


_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": ("revenue", "total_revenue", "operating_revenue", "ciro", "hasilat", "satis_gelirleri"),
    "net_income": ("net_income", "net_income_common_stockholders", "net_kar", "donem_kari", "ana_ortaklik_net_kar"),
    "gross_profit": ("gross_profit", "brut_kar"),
    "operating_income": ("operating_income", "operating_profit", "faaliyet_kari", "esas_faaliyet_kari"),
    "ebitda": ("ebitda", "normalized_ebitda", "favok"),
    "operating_cash_flow": ("operating_cash_flow", "cash_from_operations", "faaliyetlerden_nakit_akisi", "faaliyet_nakdi"),
    "capital_expenditure": ("capital_expenditure", "capital_expenditures", "capex", "yatirim_harcamalari"),
    "free_cash_flow": ("free_cash_flow", "serbest_nakit_akisi"),
    "total_assets": ("total_assets", "toplam_varliklar"),
    "total_liabilities": ("total_liabilities", "total_liabilities_net_minority_interest", "toplam_yukumlulukler"),
    "total_equity": ("total_equity", "stockholders_equity", "common_stock_equity", "ozkaynaklar", "ana_ortakliga_ait_ozkaynaklar"),
    "total_debt": ("total_debt", "financial_debt", "toplam_borc", "finansal_borc"),
    "cash_and_equivalents": ("cash_and_equivalents", "cash_cash_equivalents_and_short_term_investments", "total_cash", "nakit_ve_nakit_benzerleri"),
    "current_assets": ("current_assets", "total_current_assets", "donen_varliklar"),
    "current_liabilities": ("current_liabilities", "total_current_liabilities", "kisa_vadeli_yukumlulukler"),
    "market_cap": ("market_cap", "market_capitalization", "piyasa_degeri"),
    "enterprise_value": ("enterprise_value", "firma_degeri"),
    "shares_outstanding": ("shares_outstanding", "implied_shares_outstanding", "dolasimdaki_pay"),
    "last_price": ("last_price", "current_price", "regular_market_price", "son_fiyat"),
    "revenue_ttm": ("revenue_ttm", "total_revenue_ttm", "ttm_revenue"),
    "net_income_ttm": ("net_income_ttm", "net_income_to_common", "ttm_net_income"),
    "gross_profit_ttm": ("gross_profit_ttm", "ttm_gross_profit"),
    "operating_income_ttm": ("operating_income_ttm", "ttm_operating_income"),
    "ebitda_ttm": ("ebitda_ttm", "ttm_ebitda"),
}
_ALIAS_TO_FIELD = {
    _key(alias): canonical
    for canonical, aliases in _FIELD_ALIASES.items()
    for alias in (canonical, *aliases)
}
_CORE_FINANCIAL_FIELDS = {
    "revenue", "net_income", "gross_profit", "operating_income", "ebitda",
    "operating_cash_flow", "total_assets", "total_liabilities", "total_equity",
    "revenue_ttm", "net_income_ttm",
}


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        cleaned = value.strip().replace(" ", "")
        if not cleaned or cleaned.lower() in {"none", "null", "nan", "n/a", "-"}:
            return None
        # API payloads must use JSON-style decimal points. Thousands separators
        # are accepted only in the unambiguous 1,234.56 form.
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")
        elif cleaned.count(",") == 1 and cleaned.count(".") == 0:
            cleaned = cleaned.replace(",", ".")
        value = cleaned
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _parse_period_end(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).date()
    text = str(value or "").strip()
    if not text:
        raise ProviderResponseError("Finansal dönem tarihi eksik.")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    quarter = re.fullmatch(r"(\d{4})[- /]?Q([1-4])", text, re.IGNORECASE)
    if quarter:
        year, number = int(quarter.group(1)), int(quarter.group(2))
        month = number * 3
        return date(year, month, calendar.monthrange(year, month)[1])
    month_match = re.fullmatch(r"(\d{4})[- /](\d{1,2})", text)
    if month_match:
        year, month = int(month_match.group(1)), int(month_match.group(2))
        return date(year, month, calendar.monthrange(year, month)[1])
    raise ProviderResponseError("Finansal dönem tarihi tanınmadı.")


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderResponseError("Finansal yayın zamanı tanınmadı.") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_period_type(value: Any) -> PeriodType:
    normalized = _key(value)
    if normalized in {"quarterly", "quarter", "ceyrek", "ceyreklik", "3aylik", "6aylik", "9aylik"}:
        return PeriodType.QUARTERLY
    if normalized in {"annual", "yearly", "year", "yillik", "12aylik"}:
        return PeriodType.ANNUAL
    if normalized in {"ttm", "trailing12months", "son12ay"}:
        return PeriodType.TTM
    return PeriodType.UNKNOWN


def _parse_consolidated(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = _key(value)
    if normalized in {"1", "true", "yes", "evet", "consolidated", "konsolide"}:
        return True
    if normalized in {"0", "false", "no", "hayir", "unconsolidated", "solo", "konsolideolmayan"}:
        return False
    return None


def _parse_duration_months(item: Mapping[str, Any], root_period_type: Any) -> int | None:
    raw = (
        item.get("duration_months")
        or item.get("period_length_months")
        or item.get("months")
    )
    if raw not in (None, ""):
        try:
            duration = int(raw)
        except (TypeError, ValueError) as exc:
            raise ProviderResponseError("Finansal dönem süresi geçersiz.") from exc
        if duration not in {3, 6, 9, 12}:
            raise ProviderResponseError("Finansal dönem süresi 3, 6, 9 veya 12 ay olmalıdır.")
        return duration
    label = _key(item.get("period_type") or item.get("frequency") or root_period_type)
    implicit = {"3aylik": 3, "6aylik": 6, "9aylik": 9, "12aylik": 12}
    if label in implicit:
        return implicit[label]
    if label in {"annual", "yearly", "year", "yillik", "ttm", "trailing12months", "son12ay"}:
        return 12
    return None


def _parse_flow_basis(item: Mapping[str, Any], period_type: PeriodType) -> str:
    raw = _key(item.get("flow_basis") or item.get("statement_basis"))
    mapping = {
        "discrete": "discrete",
        "quarter": "discrete",
        "standalone": "discrete",
        "cumulative": "cumulative_ytd",
        "cumulativeytd": "cumulative_ytd",
        "ytd": "cumulative_ytd",
        "annual": "annual",
        "ttm": "ttm",
    }
    if raw:
        if raw not in mapping:
            raise ProviderResponseError("Finansal akış dönemi baz türü tanınmadı.")
        return mapping[raw]
    if period_type == PeriodType.ANNUAL:
        return "annual"
    if period_type == PeriodType.TTM:
        return "ttm"
    return "unknown"


def _collect_values(container: Mapping[str, Any]) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}

    def visit(mapping: Mapping[str, Any], depth: int = 0) -> None:
        if depth > 2:
            return
        for raw_key, raw_value in mapping.items():
            canonical = _ALIAS_TO_FIELD.get(_key(raw_key))
            if canonical:
                parsed = decimal_or_none(raw_value)
                if parsed is not None:
                    result[canonical] = parsed
            elif isinstance(raw_value, Mapping) and _key(raw_key) in {
                "values", "metrics", "financials", "income", "incomestatement",
                "balancesheet", "cashflow", "marketdata", "valuation",
            }:
                visit(raw_value, depth + 1)

    visit(container)
    return result


def _period_items(payload: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    periods = payload.get("periods") or payload.get("statements")
    if isinstance(periods, list):
        return tuple(item for item in periods if isinstance(item, Mapping))
    if isinstance(periods, Mapping):
        items: list[Mapping[str, Any]] = []
        for period_key, item in periods.items():
            if isinstance(item, Mapping):
                items.append({"period_end": period_key, **dict(item)})
        return tuple(items)
    if any(key in payload for key in ("period_end", "period", "date", "as_of")):
        return (payload,)
    raise ProviderResponseError("Sağlayıcı yanıtında finansal dönem bulunamadı.")


def _unwrap(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    current = payload
    for _ in range(3):
        wrapper = next(
            (
                current[key]
                for key in ("data", "snapshot")
                if isinstance(current.get(key), Mapping)
            ),
            None,
        )
        if wrapper is None:
            return current
        current = wrapper
    return current


def snapshot_from_payload(
    payload: Mapping[str, Any],
    *,
    provider: str,
    trust: SourceTrust,
    requested_symbol: str,
    default_source_url: str | None,
    notes: tuple[str, ...] = (),
) -> FundamentalSnapshot:
    if not isinstance(payload, Mapping):
        raise ProviderResponseError("Sağlayıcı geçerli bir JSON nesnesi döndürmedi.")
    payload = _unwrap(payload)
    symbol = normalize_symbol(payload.get("symbol") or payload.get("ticker") or requested_symbol)
    if symbol != normalize_symbol(requested_symbol):
        raise ProviderResponseError("Sağlayıcı farklı bir şirket sembolü döndürdü.")

    company = payload.get("company") if isinstance(payload.get("company"), Mapping) else {}
    company_name = str(
        company.get("name") or payload.get("company_name") or payload.get("name") or symbol
    ).strip()
    sector = company.get("sector") or payload.get("sector")
    industry = company.get("industry") or payload.get("industry")
    summary = company.get("summary") or company.get("description") or payload.get("summary") or payload.get("description")
    root_currency = payload.get("currency") or payload.get("currency_code")
    root_revision = payload.get("revision") or payload.get("version")
    root_consolidated = payload.get("consolidated")
    root_period_type = payload.get("period_type") or payload.get("frequency")
    root_published_at = payload.get("published_at") or payload.get("filing_date")

    normalized_periods: list[StatementPeriod] = []
    for item in _period_items(payload):
        period_end = _parse_period_end(
            item.get("period_end") or item.get("period") or item.get("date") or item.get("as_of")
        )
        currency = str(item.get("currency") or item.get("currency_code") or root_currency or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ProviderResponseError("Finansal dönemin para birimi eksik veya geçersiz.")
        values_container = item.get("values") if isinstance(item.get("values"), Mapping) else item
        values = _collect_values(values_container)
        if not values or not (_CORE_FINANCIAL_FIELDS & values.keys()):
            raise ProviderResponseError("Finansal dönemde doğrulanabilir tablo kalemi bulunamadı.")
        revision_value = item.get("revision") if "revision" in item else root_revision
        consolidated_value = item.get("consolidated") if "consolidated" in item else root_consolidated
        published_value = item.get("published_at") or item.get("filing_date") or root_published_at
        parsed_period_type = _parse_period_type(
            item.get("period_type") or item.get("frequency") or root_period_type
        )
        normalized_periods.append(
            StatementPeriod(
                source=provider,
                period_end=period_end,
                period_type=parsed_period_type,
                revision=str(revision_value).strip() if revision_value not in (None, "") else None,
                consolidated=_parse_consolidated(consolidated_value),
                currency=currency,
                published_at=_parse_datetime(published_value),
                values=values,
                duration_months=_parse_duration_months(item, root_period_type),
                flow_basis=_parse_flow_basis(item, parsed_period_type),
            )
        )
    if not normalized_periods:
        raise ProviderResponseError("Sağlayıcı boş finansal tablo döndürdü.")
    periods_tuple = tuple(sorted(normalized_periods, key=lambda item: item.period_end, reverse=True))
    latest_values = periods_tuple[0].values
    for field in ("market_cap", "shares_outstanding", "last_price"):
        if field in latest_values and latest_values[field] <= 0:
            raise ProviderResponseError(f"{field} pozitif olmalıdır.")
    market_cap = latest_values.get("market_cap")
    shares = latest_values.get("shares_outstanding")
    price = latest_values.get("last_price")
    if market_cap is not None and shares is not None and price is not None:
        implied_cap = shares * price
        scale_ratio = market_cap / implied_cap
        if scale_ratio < Decimal("0.1") or scale_ratio > Decimal("10"):
            raise ProviderResponseError(
                "Piyasa değeri, pay sayısı ve son fiyat arasında birim/ölçek uyuşmazlığı var."
            )
    source_url = payload.get("source_url") or payload.get("url") or default_source_url
    return FundamentalSnapshot(
        symbol=symbol,
        company_name=company_name or symbol,
        sector=str(sector).strip() if sector else None,
        industry=str(industry).strip() if industry else None,
        summary=str(summary).strip() if summary else None,
        periods=periods_tuple,
        ratios=calculate_ratios(periods_tuple),
        provenance=DataProvenance(
            provider=provider,
            trust=trust,
            source_url=str(source_url) if source_url else None,
            notes=notes,
        ),
    )
