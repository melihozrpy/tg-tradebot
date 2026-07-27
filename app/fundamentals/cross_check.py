from __future__ import annotations

from decimal import Decimal

from app.fundamentals.base import (
    CrossCheckMismatchError,
    FundamentalDataError,
    FundamentalDataProvider,
    ProviderUnavailableError,
)
from app.fundamentals.models import CrossCheckReport, FieldComparison, FundamentalSnapshot


DEFAULT_COMPARE_FIELDS = (
    "revenue",
    "net_income",
    "gross_profit",
    "operating_income",
    "ebitda",
    "operating_cash_flow",
    "total_assets",
    "total_liabilities",
    "total_equity",
    "total_debt",
    "cash_and_equivalents",
)


class FundamentalCrossCheckService(FundamentalDataProvider):
    """Resolve primary data and compare it with a separately licensed source.

    Fallback is disabled by default. Enabling it never marks the result verified,
    so a caller cannot accidentally present Yahoo data as official KAP data.
    """

    name = "cross_checked_fundamentals"

    def __init__(
        self,
        primary: FundamentalDataProvider,
        secondary: FundamentalDataProvider | None = None,
        *,
        relative_tolerance: Decimal | str = Decimal("0.03"),
        absolute_tolerance: Decimal | str = Decimal("1"),
        compare_fields: tuple[str, ...] = DEFAULT_COMPARE_FIELDS,
        strict: bool = True,
        allow_secondary_fallback: bool = False,
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.relative_tolerance = Decimal(str(relative_tolerance))
        self.absolute_tolerance = Decimal(str(absolute_tolerance))
        self.compare_fields = compare_fields
        self.strict = strict
        self.allow_secondary_fallback = allow_secondary_fallback
        if self.relative_tolerance < 0 or self.absolute_tolerance < 0:
            raise ValueError("Cross-check tolerances cannot be negative")

    @staticmethod
    def _safe_fetch(provider: FundamentalDataProvider, symbol: str) -> FundamentalSnapshot:
        try:
            return provider.fetch(symbol)
        except FundamentalDataError:
            raise
        except Exception as exc:
            raise ProviderUnavailableError(f"{provider.name} temel veri sağlayıcısı başarısız.") from exc

    def _compare(
        self,
        primary: FundamentalSnapshot,
        secondary: FundamentalSnapshot,
    ) -> tuple[tuple[FieldComparison, ...], tuple[str, ...]]:
        warnings: list[str] = []
        latest = primary.latest_period
        other = secondary.period_on(latest.period_end)
        if other is None:
            return (), ("İkincil kaynakta aynı finansal dönem bulunamadı.",)
        if latest.currency != other.currency:
            return (), ("Kaynakların finansal para birimleri farklı.",)
        if (
            latest.consolidated is not None
            and other.consolidated is not None
            and latest.consolidated != other.consolidated
        ):
            return (), ("Konsolide/solo tablo türleri farklı.",)

        comparisons: list[FieldComparison] = []
        for field in self.compare_fields:
            primary_value = latest.value(field)
            secondary_value = other.value(field)
            if primary_value is None or secondary_value is None:
                continue
            denominator = max(abs(primary_value), abs(secondary_value))
            absolute_difference = abs(primary_value - secondary_value)
            relative_difference = absolute_difference / denominator if denominator else Decimal("0")
            limit = max(self.absolute_tolerance, denominator * self.relative_tolerance)
            comparisons.append(
                FieldComparison(
                    field=field,
                    primary_value=primary_value,
                    secondary_value=secondary_value,
                    relative_difference=relative_difference,
                    matches=absolute_difference <= limit,
                )
            )
        if not comparisons:
            warnings.append("Kaynaklar arasında karşılaştırılabilir ortak tablo kalemi bulunamadı.")
        return tuple(comparisons), tuple(warnings)

    def resolve(self, symbol: str) -> CrossCheckReport:
        try:
            primary = self._safe_fetch(self.primary, symbol)
        except FundamentalDataError:
            if not self.allow_secondary_fallback or self.secondary is None:
                raise
            fallback = self._safe_fetch(self.secondary, symbol)
            return CrossCheckReport(
                snapshot=fallback,
                secondary=None,
                comparisons=(),
                verified=False,
                used_fallback=True,
                warnings=("Birincil kaynak kullanılamadı; ikincil veri açıkça fallback olarak kullanıldı.",),
            )

        if self.secondary is None:
            return CrossCheckReport(
                snapshot=primary,
                secondary=None,
                comparisons=(),
                verified=primary.provenance.trust.value == "primary",
                used_fallback=False,
                warnings=("İkinci kaynakla sayısal çapraz kontrol yapılmadı.",),
            )
        try:
            secondary = self._safe_fetch(self.secondary, symbol)
        except FundamentalDataError:
            return CrossCheckReport(
                snapshot=primary,
                secondary=None,
                comparisons=(),
                verified=False,
                used_fallback=False,
                warnings=("İkincil kaynak kullanılamadığı için çapraz kontrol tamamlanamadı.",),
            )

        comparisons, warnings = self._compare(primary, secondary)
        mismatches = tuple(item.field for item in comparisons if not item.matches)
        metadata_problem = bool(warnings)
        if self.strict and (mismatches or metadata_problem):
            detail = ", ".join(mismatches) if mismatches else warnings[0]
            raise CrossCheckMismatchError(f"Temel veri çapraz kontrolü başarısız: {detail}")
        verified = bool(comparisons) and not mismatches and not warnings
        return CrossCheckReport(
            snapshot=primary,
            secondary=secondary,
            comparisons=comparisons,
            verified=verified,
            used_fallback=False,
            warnings=warnings + ((f"Uyuşmayan alanlar: {', '.join(mismatches)}",) if mismatches else ()),
        )

    def fetch(self, symbol: str) -> FundamentalSnapshot:
        return self.resolve(symbol).snapshot
