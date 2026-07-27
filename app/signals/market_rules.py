"""Versioned Borsa Istanbul Pay Piyasasi price/tick rules.

The default bands mirror the Pay Piyasasi table published by Borsa Istanbul
for equities and pre-emptive rights.  They are data, rather than conditionals,
so a future exchange revision can be introduced as a new version without
changing execution code.

Official reference (accessed 2026-07-25):
https://www.borsaistanbul.com/piyasalar/pay-piyasasi/piyasa-isleyisi
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Iterable

from app.signals.enums import PricePurpose, RoundingDirection


DecimalLike = Decimal | int | float | str


class MarketRuleError(ValueError):
    """Raised when a price or a market-rule definition is invalid."""


def as_decimal(value: DecimalLike, *, field_name: str = "value") -> Decimal:
    if isinstance(value, bool):
        raise MarketRuleError(f"{field_name} sayisal olmalidir.")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MarketRuleError(f"{field_name} gecerli bir sayi olmalidir.") from exc
    if not number.is_finite():
        raise MarketRuleError(f"{field_name} sonlu bir sayi olmalidir.")
    return number


@dataclass(frozen=True, slots=True)
class TickBand:
    lower: Decimal
    upper_exclusive: Decimal | None
    tick: Decimal

    def contains(self, price: Decimal) -> bool:
        return price >= self.lower and (self.upper_exclusive is None or price < self.upper_exclusive)


@dataclass(frozen=True, slots=True)
class RoundedOrderPrice:
    raw_calculated_price: Decimal
    rounded_order_price: Decimal
    tick_size: Decimal
    purpose: PricePurpose
    direction: RoundingDirection
    market_rule_version: str


@dataclass(frozen=True, slots=True)
class DailyPriceLimits:
    base_price: Decimal
    raw_lower_limit: Decimal
    lower_limit: Decimal
    raw_upper_limit: Decimal
    upper_limit: Decimal
    market_rule_version: str


DEFAULT_EQUITY_TICK_BANDS: tuple[TickBand, ...] = (
    TickBand(Decimal("0.010"), Decimal("20.000"), Decimal("0.010")),
    TickBand(Decimal("20.000"), Decimal("50.000"), Decimal("0.020")),
    TickBand(Decimal("50.000"), Decimal("100.000"), Decimal("0.050")),
    TickBand(Decimal("100.000"), Decimal("250.000"), Decimal("0.100")),
    TickBand(Decimal("250.000"), Decimal("500.000"), Decimal("0.250")),
    TickBand(Decimal("500.000"), Decimal("1000.000"), Decimal("0.500")),
    TickBand(Decimal("1000.000"), Decimal("2500.000"), Decimal("1.000")),
    TickBand(Decimal("2500.000"), None, Decimal("2.500")),
)


_PURPOSE_DIRECTIONS: dict[PricePurpose, RoundingDirection] = {
    # A buy limit must never silently exceed the user's maximum price.
    PricePurpose.BUY_LIMIT: RoundingDirection.FLOOR,
    PricePurpose.ENTRY_ZONE_LOW: RoundingDirection.CEILING,
    PricePurpose.ENTRY_ZONE_HIGH: RoundingDirection.FLOOR,
    # A breakout trigger must remain above, not below, the calculated level.
    PricePurpose.BREAKOUT_TRIGGER: RoundingDirection.CEILING,
    # Raising a long protective stop reduces risk; lowering it increases risk.
    PricePurpose.PROTECTIVE_STOP_LONG: RoundingDirection.CEILING,
    # Rounding a target toward the position is conservative for fill modelling.
    PricePurpose.TARGET_LONG: RoundingDirection.FLOOR,
    PricePurpose.MANUAL_EXIT: RoundingDirection.NEAREST,
    PricePurpose.REFERENCE_PRICE: RoundingDirection.NEAREST,
}


@dataclass(frozen=True, slots=True)
class BistMarketRules:
    version: str = "BIST_PAY_2026_01"
    tick_bands: tuple[TickBand, ...] = DEFAULT_EQUITY_TICK_BANDS
    # Borsa Istanbul states that price margins can differ by market,
    # instrument and trading method.  Therefore the generic rule set must not
    # silently assume a universal 10% limit.  A licensed quote/reference-data
    # provider may supply an instrument-specific default, or callers can pass
    # the percentage for the calculation explicitly.
    daily_price_limit_percent: Decimal | None = None
    lot_size: int = 1

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise MarketRuleError("Piyasa kural surumu bos olamaz.")
        if not self.tick_bands:
            raise MarketRuleError("En az bir fiyat adimi bandi gereklidir.")
        previous_upper: Decimal | None = None
        for index, band in enumerate(self.tick_bands):
            if band.lower <= 0 or band.tick <= 0:
                raise MarketRuleError("Fiyat bandi ve adimi pozitif olmalidir.")
            if band.upper_exclusive is not None and band.upper_exclusive <= band.lower:
                raise MarketRuleError("Fiyat bandinin ust siniri alt sinirindan buyuk olmalidir.")
            if index and previous_upper != band.lower:
                raise MarketRuleError("Fiyat adimi bantlari bosluksuz ve sirali olmalidir.")
            previous_upper = band.upper_exclusive
        if self.tick_bands[-1].upper_exclusive is not None:
            raise MarketRuleError("Son fiyat adimi bandi acik uclu olmalidir.")
        if self.daily_price_limit_percent is not None and (
            self.daily_price_limit_percent <= 0 or self.daily_price_limit_percent >= 100
        ):
            raise MarketRuleError("Gunluk fiyat limiti yuzdesi 0 ile 100 arasinda olmalidir.")
        if self.lot_size < 1:
            raise MarketRuleError("Lot buyuklugu en az 1 olmalidir.")

    @classmethod
    def from_bands(
        cls,
        version: str,
        bands: Iterable[TickBand],
        *,
        daily_price_limit_percent: DecimalLike | None = None,
        lot_size: int = 1,
    ) -> "BistMarketRules":
        return cls(
            version=version,
            tick_bands=tuple(bands),
            daily_price_limit_percent=(
                as_decimal(daily_price_limit_percent, field_name="daily_price_limit_percent")
                if daily_price_limit_percent is not None
                else None
            ),
            lot_size=lot_size,
        )

    def tick_size_for(self, price: DecimalLike) -> Decimal:
        number = as_decimal(price, field_name="price")
        if number < self.tick_bands[0].lower:
            raise MarketRuleError(f"Fiyat en az {self.tick_bands[0].lower} TL olmalidir.")
        for band in self.tick_bands:
            if band.contains(number):
                return band.tick
        raise MarketRuleError(f"Fiyat icin adim bulunamadi: {number}")

    def is_valid_price(self, price: DecimalLike) -> bool:
        try:
            number = as_decimal(price, field_name="price")
            tick = self.tick_size_for(number)
        except MarketRuleError:
            return False
        return number % tick == 0

    @staticmethod
    def _decimal_places(tick: Decimal) -> int:
        return max(0, -tick.normalize().as_tuple().exponent)

    def round_to_tick(self, price: DecimalLike, direction: RoundingDirection) -> tuple[Decimal, Decimal]:
        raw = as_decimal(price, field_name="price")
        if raw <= 0:
            raise MarketRuleError("Fiyat sifirdan buyuk olmalidir.")
        tick = self.tick_size_for(raw)
        rounding = {
            RoundingDirection.FLOOR: ROUND_FLOOR,
            RoundingDirection.CEILING: ROUND_CEILING,
            RoundingDirection.NEAREST: ROUND_HALF_UP,
        }[direction]
        rounded = (raw / tick).to_integral_value(rounding=rounding) * tick

        # A rounding operation can cross a band boundary. Re-evaluate the tick
        # at the resulting price until it is valid under the destination band.
        for _ in range(3):
            destination_tick = self.tick_size_for(rounded)
            if rounded % destination_tick == 0:
                tick = destination_tick
                break
            rounded = (rounded / destination_tick).to_integral_value(rounding=rounding) * destination_tick
            tick = destination_tick
        if not self.is_valid_price(rounded):
            raise MarketRuleError(f"Fiyat gecerli bir BIST adimina yuvarlanamadi: {raw}")
        quantum = Decimal(1).scaleb(-self._decimal_places(tick))
        return rounded.quantize(quantum), tick

    def round_price(self, price: DecimalLike, purpose: PricePurpose) -> RoundedOrderPrice:
        raw = as_decimal(price, field_name="price")
        direction = _PURPOSE_DIRECTIONS[purpose]
        rounded, tick = self.round_to_tick(raw, direction)
        return RoundedOrderPrice(raw, rounded, tick, purpose, direction, self.version)

    def daily_price_limits(
        self,
        base_price: DecimalLike,
        *,
        limit_percent: DecimalLike | None = None,
    ) -> DailyPriceLimits:
        base = self.round_price(base_price, PricePurpose.REFERENCE_PRICE).rounded_order_price
        configured_percent = (
            as_decimal(limit_percent, field_name="limit_percent")
            if limit_percent is not None
            else self.daily_price_limit_percent
        )
        if configured_percent is None:
            raise MarketRuleError(
                "Gunluk fiyat limiti enstruman/pazar bazinda veri saglayicidan alinmalidir."
            )
        if configured_percent <= 0 or configured_percent >= 100:
            raise MarketRuleError("Gunluk fiyat limiti yuzdesi 0 ile 100 arasinda olmalidir.")
        ratio = configured_percent / Decimal("100")
        raw_lower = base * (Decimal("1") - ratio)
        raw_upper = base * (Decimal("1") + ratio)
        # Both executable limits remain within the configured percentage band.
        lower, _ = self.round_to_tick(raw_lower, RoundingDirection.CEILING)
        upper, _ = self.round_to_tick(raw_upper, RoundingDirection.FLOOR)
        return DailyPriceLimits(base, raw_lower, lower, raw_upper, upper, self.version)

    def validate_long_stop_move(self, old_stop: DecimalLike, new_stop: DecimalLike) -> Decimal:
        old = as_decimal(old_stop, field_name="old_stop")
        rounded = self.round_price(new_stop, PricePurpose.PROTECTIVE_STOP_LONG).rounded_order_price
        if rounded < old:
            raise MarketRuleError("Long pozisyonda stop asagi tasinarak risk artirilamaz.")
        return rounded


DEFAULT_BIST_MARKET_RULES = BistMarketRules()
