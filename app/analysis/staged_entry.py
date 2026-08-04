from __future__ import annotations

"""Zone-based staged entries that never substitute the last close for entry.

The module is deliberately independent from Telegram and persistence.  Signal,
AI and chart consumers receive the same immutable plan and can persist fills in
their own transaction boundary.
"""

from dataclasses import dataclass, replace
from typing import Literal, Sequence

from app.analysis.indicator_engine import ConfluenceResult
from app.analysis.quality_zone_engine import QualityZoneScenario

EntryStatus = Literal["PENDING", "CONFIRMED", "INVALIDATED", "COMPLETED"]


@dataclass(frozen=True)
class StagedEntryLevel:
    order: int
    price: float
    allocation_percent: float
    label: str
    filled: bool = False


@dataclass(frozen=True)
class StagedEntryPlan:
    symbol: str
    direction: Literal["LONG", "SHORT"]
    zone_kind: str
    zone_low: float
    zone_high: float
    current_price: float
    status: EntryStatus
    levels: tuple[StagedEntryLevel, ...]
    invalidation: float
    target_1: float | None
    target_2: float | None
    structural_entry: float
    entry_reason: str
    structure_confirmed: bool
    rr_is_sufficient: bool
    confluence_count: int
    confluence_required: int
    confirmations: tuple[str, ...]
    cancelled_reason: str | None = None

    @property
    def filled_levels(self) -> tuple[StagedEntryLevel, ...]:
        return tuple(level for level in self.levels if level.filled)

    @property
    def average_entry(self) -> float | None:
        filled = self.filled_levels
        total_weight = sum(level.allocation_percent for level in filled)
        if total_weight <= 0:
            return None
        return sum(level.price * level.allocation_percent for level in filled) / total_weight

    @property
    def planned_average_entry(self) -> float:
        total_weight = sum(level.allocation_percent for level in self.levels)
        return sum(level.price * level.allocation_percent for level in self.levels) / total_weight


def _normalise_allocations(allocations: Sequence[float]) -> tuple[float, float, float]:
    if len(allocations) != 3:
        raise ValueError("Kademe dagilimi tam olarak uc oran icermelidir.")
    cleaned = tuple(float(value) for value in allocations)
    if any(value <= 0 for value in cleaned):
        raise ValueError("Kademe oranlari pozitif olmalidir.")
    total = sum(cleaned)
    return tuple(value / total * 100.0 for value in cleaned)  # type: ignore[return-value]


def build_staged_entry_plan(
    scenario: QualityZoneScenario,
    *,
    symbol: str,
    allocations: Sequence[float] = (40.0, 35.0, 25.0),
    confluence: ConfluenceResult | None = None,
) -> StagedEntryPlan:
    """Split the selected OB/FVG into first-touch, middle and deep entries."""

    weights = _normalise_allocations(allocations)
    low, high = sorted((float(scenario.zone_low), float(scenario.zone_high)))
    midpoint = (low + high) / 2.0
    prices = (high, midpoint, low) if scenario.direction == "LONG" else (low, midpoint, high)
    labels = ("Ilk dokunus", "Zone ortasi / sweep", "Derin retest / son guvenli nokta")
    levels = tuple(
        StagedEntryLevel(index, float(price), weights[index - 1], labels[index - 1])
        for index, price in enumerate(prices, 1)
    )
    confluence_count = len(confluence.confirmations) if confluence else 0
    confluence_required = confluence.minimum_required if confluence else 3
    inside = low <= float(scenario.current_price) <= high
    confirmed = (
        inside
        and scenario.structure_confirmed
        and scenario.rr_is_sufficient
        and confluence_count >= confluence_required
    )
    return StagedEntryPlan(
        symbol=symbol.upper().removesuffix(".IS"),
        direction=scenario.direction,  # type: ignore[arg-type]
        zone_kind=scenario.zone_kind,
        zone_low=low,
        zone_high=high,
        current_price=float(scenario.current_price),
        status="CONFIRMED" if confirmed else "PENDING",
        levels=levels,
        invalidation=float(scenario.invalidation),
        target_1=scenario.target_1,
        target_2=scenario.target_2,
        structural_entry=float(scenario.entry),
        entry_reason=scenario.entry_reason,
        structure_confirmed=scenario.structure_confirmed,
        rr_is_sufficient=scenario.rr_is_sufficient,
        confluence_count=confluence_count,
        confluence_required=confluence_required,
        confirmations=confluence.confirmations if confluence else (),
    )


def evaluate_staged_entry(
    plan: StagedEntryPlan,
    *,
    candle_low: float,
    candle_high: float,
    candle_close: float,
) -> StagedEntryPlan:
    """Apply one completed candle, fill touched stages and cancel on invalidation."""

    if plan.status in {"INVALIDATED", "COMPLETED"}:
        return plan
    invalidated = (
        candle_close < plan.invalidation
        if plan.direction == "LONG"
        else candle_close > plan.invalidation
    )
    if invalidated:
        return replace(
            plan,
            status="INVALIDATED",
            cancelled_reason=(
                f"{candle_close:.2f} kapanisi invalidation {plan.invalidation:.2f} "
                "otesinde; kalan kademeler iptal edildi."
            ),
        )

    updated: list[StagedEntryLevel] = []
    for level in plan.levels:
        touched = float(candle_low) <= level.price <= float(candle_high)
        updated.append(replace(level, filled=True) if touched and not level.filled else level)
    all_filled = all(level.filled for level in updated)
    return replace(plan, levels=tuple(updated), status="COMPLETED" if all_filled else plan.status)


def format_staged_entry_plan(plan: StagedEntryPlan, *, decimals: int = 2) -> str:
    state_icon = "✅" if plan.status == "CONFIRMED" else "⏳" if plan.status == "PENDING" else "⛔"
    lines = [
        f"🪜 {plan.symbol} — KADEMELİ {plan.direction} PLANI",
        f"🔷 Bölge: {plan.zone_kind} {plan.zone_low:.{decimals}f}-{plan.zone_high:.{decimals}f}",
        f"{state_icon} Durum: {plan.status}",
    ]
    if plan.status == "PENDING":
        lines.append(
            f"⚠️ Şu an entry YOK. Fiyat {plan.zone_low:.{decimals}f}-{plan.zone_high:.{decimals}f} "
            "bölgesine gelmeli; yapı ve en az 3 indikatör teyidi beklenmeli."
        )
    for level in plan.levels:
        fill_icon = "☑️" if level.filled else "▫️"
        lines.append(
            f"{fill_icon} Kademe {level.order}: %{level.allocation_percent:.0f} @ "
            f"{level.price:.{decimals}f} — {level.label}"
        )
    average = plan.average_entry
    if average is not None:
        lines.append(f"💰 Gerçekleşen ortalama maliyet: {average:.{decimals}f}")
    else:
        lines.append(f"🧮 Tüm kademeler dolarsa ortalama: {plan.planned_average_entry:.{decimals}f}")
    lines.extend(
        [
            f"🛑 Ortak SL / invalidation: {plan.invalidation:.{decimals}f}",
            f"🎯 TP1: {plan.target_1:.{decimals}f}" if plan.target_1 is not None else "🎯 TP1: doğrulanamadı",
            f"🎯 TP2: {plan.target_2:.{decimals}f}" if plan.target_2 is not None else "🎯 TP2: doğrulanamadı",
            f"🧩 Confluence: {plan.confluence_count}/{plan.confluence_required} minimum",
            f"💬 Yapısal entry nedeni: {plan.entry_reason}",
        ]
    )
    if plan.cancelled_reason:
        lines.append(f"🚫 {plan.cancelled_reason}")
    lines.append("ℹ️ Son kapanış fiyatı entry olarak kullanılmaz; plan yalnızca zone seviyelerinden oluşur.")
    return "\n".join(lines)
