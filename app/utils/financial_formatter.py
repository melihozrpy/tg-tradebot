from __future__ import annotations

"""Finansal değerler için tek ve güvenli biçimlendirme noktası."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from math import isfinite
from typing import Any, Optional


def finite_float(value: Any) -> Optional[float]:
    """None/NaN/inf ve sayısal olmayan değerleri kullanıcıya sızdırmaz."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def safe_decimal(value: Any) -> Optional[Decimal]:
    number = finite_float(value)
    if number is None:
        return None
    try:
        return Decimal(str(number))
    except InvalidOperation:
        return None


def round_money(value: Any) -> Optional[float]:
    number = safe_decimal(value)
    if number is None:
        return None
    return float(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def percent_change(new_value: Any, reference_value: Any) -> Optional[float]:
    current = safe_decimal(new_value)
    reference = safe_decimal(reference_value)
    if current is None or reference is None or reference <= 0:
        return None
    result = ((current - reference) / reference) * Decimal("100")
    return float(result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def price_multiple(target: Any, current: Any) -> Optional[float]:
    target_d = safe_decimal(target)
    current_d = safe_decimal(current)
    if target_d is None or current_d is None or target_d <= 0 or current_d <= 0:
        return None
    return float((target_d / current_d).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def format_price(value: Any, *, suffix: str = " TL", missing: str = "Veri bulunamadı") -> str:
    number = round_money(value)
    return missing if number is None else f"{number:,.2f}{suffix}".replace(",", "_").replace(".", ",").replace("_", ".")


def format_percent(value: Any, *, signed: bool = True, missing: str = "Veri bulunamadı") -> str:
    number = finite_float(value)
    if number is None:
        return missing
    sign = "+" if signed and number > 0 else ""
    return f"{sign}%{number:.2f}"


def format_multiple(value: Any, *, missing: str = "Veri bulunamadı") -> str:
    number = finite_float(value)
    return missing if number is None else f"{number:.2f}x"


def format_try_compact(value: Any, *, missing: str = "Veri bulunamadı") -> str:
    number = finite_float(value)
    if number is None:
        return missing
    absolute = abs(number)
    for divisor, label in ((1_000_000_000, "mr"), (1_000_000, "mn"), (1_000, "bin")):
        if absolute >= divisor:
            return f"{number / divisor:.2f} {label} TL"
    return format_price(number)
