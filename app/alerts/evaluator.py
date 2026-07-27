from __future__ import annotations

from decimal import Decimal

from app.alerts.enums import AlarmCondition, AlarmStatus
from app.alerts.schemas import EvaluationDecision, PriceObservation


def _d(value) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def evaluate_price_alarm(alert, observation: PriceObservation, *, stale_after_seconds: int = 180,
                         production: bool = False) -> EvaluationDecision:
    if observation.price <= 0 or not observation.price.is_finite():
        return EvaluationDecision(False, "INVALID_PRICE", rejection_reason="Geçersiz fiyat")
    if production and observation.provider.casefold() == "mock":
        return EvaluationDecision(False, "MOCK_REJECTED", rejection_reason="Üretimde mock veri reddedildi")
    if observation.fallback_used or not observation.is_live:
        return EvaluationDecision(False, "FALLBACK_REJECTED", rejection_reason="Canlı olmayan veri")
    if observation.freshness_seconds < 0 or observation.freshness_seconds > stale_after_seconds:
        return EvaluationDecision(False, "STALE_REJECTED", rejection_reason="Fiyat verisi eski")
    if observation.quality_state.upper() in {"INVALID", "UNUSABLE"}:
        return EvaluationDecision(False, "QUALITY_REJECTED", rejection_reason="Veri kalitesi yetersiz")

    condition = AlarmCondition(alert.condition_type)
    price, target = observation.price, _d(alert.target_price)
    previous = _d(alert.last_observed_price) or _d(alert.previous_valid_price)
    tolerance = _d(alert.near_tolerance) or max(target * Decimal("0.001"), Decimal("0.01"))
    base, percentage = _d(alert.base_price), _d(alert.percentage_value)
    triggered = False
    if condition == AlarmCondition.PRICE_GTE:
        triggered = price >= target
    elif condition == AlarmCondition.PRICE_LTE:
        triggered = price <= target
    elif condition == AlarmCondition.CROSS_UP:
        triggered = previous is not None and previous < target <= price
    elif condition == AlarmCondition.CROSS_DOWN:
        triggered = previous is not None and previous > target >= price
    elif condition == AlarmCondition.PRICE_NEAR:
        triggered = abs(price - target) <= tolerance
    elif condition == AlarmCondition.PERCENT_UP_FROM_BASE and base and percentage is not None:
        triggered = price >= base * (Decimal("1") + percentage / Decimal("100"))
    elif condition == AlarmCondition.PERCENT_DOWN_FROM_BASE and base and percentage is not None:
        triggered = price <= base * (Decimal("1") - percentage / Decimal("100"))

    reset_band = _d(alert.reset_band_value) or max(target * Decimal("0.005"), tolerance)
    should_rearm = False
    if alert.status in {AlarmStatus.ACKNOWLEDGED.value, AlarmStatus.COMPLETED.value}:
        if condition in {AlarmCondition.PRICE_GTE, AlarmCondition.CROSS_UP}:
            should_rearm = price <= target - reset_band
        elif condition in {AlarmCondition.PRICE_LTE, AlarmCondition.CROSS_DOWN}:
            should_rearm = price >= target + reset_band
        elif condition == AlarmCondition.PRICE_NEAR:
            should_rearm = abs(price - target) > tolerance + reset_band
    key = f"{condition.value}:{target}:{observation.data_timestamp.isoformat()}"
    return EvaluationDecision(triggered, key, should_rearm)
