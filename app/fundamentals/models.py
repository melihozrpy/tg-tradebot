from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Mapping


class PeriodType(str, Enum):
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    TTM = "ttm"
    UNKNOWN = "unknown"


class SourceTrust(str, Enum):
    PRIMARY = "primary"
    LICENSED = "licensed"
    SECONDARY = "secondary"


@dataclass(frozen=True)
class DataProvenance:
    """Where a snapshot came from and when it was obtained.

    ``provider`` is assigned by the adapter, never trusted from a remote payload.
    This prevents a secondary source from presenting itself as KAP.
    """

    provider: str
    trust: SourceTrust
    source_url: str | None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StatementPeriod:
    source: str
    period_end: date
    period_type: PeriodType
    revision: str | None
    consolidated: bool | None
    currency: str
    published_at: datetime | None
    values: Mapping[str, Decimal]
    # Flow statements published by BIST issuers may be discrete 3-month
    # quarters or cumulative 3/6/9/12-month YTD values.  Keeping the duration
    # prevents an invalid 6M-vs-3M "growth" comparison.
    duration_months: int | None = None
    flow_basis: str = "unknown"

    def value(self, key: str) -> Decimal | None:
        return self.values.get(key)


@dataclass(frozen=True)
class FinancialRatios:
    revenue_growth: Decimal | None = None
    earnings_growth: Decimal | None = None
    profit_margin: Decimal | None = None
    gross_margin: Decimal | None = None
    operating_margin: Decimal | None = None
    debt_to_equity: Decimal | None = None
    current_ratio: Decimal | None = None
    free_cash_flow: Decimal | None = None
    return_on_equity: Decimal | None = None
    trailing_pe: Decimal | None = None
    price_to_book: Decimal | None = None
    enterprise_to_ebitda: Decimal | None = None
    net_debt: Decimal | None = None

    def as_dict(self) -> dict[str, Decimal | None]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


@dataclass(frozen=True)
class FundamentalSnapshot:
    symbol: str
    company_name: str
    sector: str | None
    industry: str | None
    summary: str | None
    periods: tuple[StatementPeriod, ...]
    ratios: FinancialRatios
    provenance: DataProvenance

    @property
    def latest_period(self) -> StatementPeriod:
        if not self.periods:
            raise ValueError("Fundamental snapshot has no financial period")
        return max(self.periods, key=lambda item: item.period_end)

    def period_on(self, period_end: date) -> StatementPeriod | None:
        return next((item for item in self.periods if item.period_end == period_end), None)


@dataclass(frozen=True)
class FieldComparison:
    field: str
    primary_value: Decimal
    secondary_value: Decimal
    relative_difference: Decimal
    matches: bool


@dataclass(frozen=True)
class CrossCheckReport:
    snapshot: FundamentalSnapshot
    secondary: FundamentalSnapshot | None
    comparisons: tuple[FieldComparison, ...]
    verified: bool
    used_fallback: bool
    warnings: tuple[str, ...] = ()
