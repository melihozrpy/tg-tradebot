from __future__ import annotations

"""Telegram için merkezi, güvenli ve kısa biçimlendirme yardımcıları.

Bu modül kullanıcıya ``None``, ``NaN``, ``inf``, iç enum/sınıf adları veya
provider exception metni sızmasını önler. Uzun ve kısa senaryo sunumları da
tek noktadan üretilir.
"""

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.utils.financial_formatter import (
    finite_float,
    format_multiple,
    format_percent,
    format_price,
)

TELEGRAM_SAFE_MESSAGE_LENGTH = 4000

_INTERNAL_ERROR_TOKENS = (
    "429",
    "rate limit",
    "too many request",
    "provider exception",
    "traceback",
    "http error",
    "circuit breaker",
    "yahoo_chart",
    "yfinance",
    "python exception",
)

_PUBLIC_LABELS = {
    "gunluk": "Günlük",
    "haftalik": "Haftalık",
    "aylik": "Aylık",
    "guclu_yukselis": "Güçlü yükseliş",
    "zayif_yukselis": "Yükseliş",
    "yatay": "Yatay",
    "dagitim": "Dağıtım riski",
    "zayif_dusus": "Düşüş",
    "guclu_dusus": "Güçlü düşüş",
    "asiri_volatil": "Aşırı oynak",
    "veri_yetersiz": "Veri yetersiz",
    "cok_yuksek": "Çok yüksek",
    "yuksek": "Yüksek",
    "orta": "Orta",
    "dusuk": "Düşük",
    "cok_dusuk": "Çok düşük",
}


def public_label(value: Any, *, missing: str = "Veri yetersiz") -> str:
    if value is None:
        return missing
    text = str(getattr(value, "value", value)).strip()
    if not text:
        return missing
    return _PUBLIC_LABELS.get(text.casefold(), text.replace("_", " ").capitalize())


def price_text(value: Any, *, missing: str = "Veri yetersiz") -> str:
    return format_price(value, missing=missing)


def percent_text(value: Any, *, signed: bool = True, missing: str = "Veri yetersiz") -> str:
    return format_percent(value, signed=signed, missing=missing)


def multiple_text(value: Any, *, missing: str = "Veri yetersiz") -> str:
    return format_multiple(value, missing=missing)


def score_text(value: Any, *, missing: str = "Veri yetersiz") -> str:
    number = finite_float(value)
    if number is None:
        return missing
    return f"{max(0.0, min(100.0, number)):.0f}/100"


def optional_text(value: Any, *, missing: str = "") -> str:
    if value is None:
        return missing
    if isinstance(value, float) and finite_float(value) is None:
        return missing
    if isinstance(value, (list, tuple, set, dict)) and not value:
        return missing
    text = str(value).strip()
    return text if text and text.casefold() not in {"none", "nan", "inf", "-inf"} else missing


@dataclass
class MissingDataCollector:
    """Tekrarlanan eksik-veri satırlarını mesaj sonunda tek bölümde toplar."""

    items: list[str] = field(default_factory=list)

    def add(self, label: str, value: Any = None) -> None:
        if value is None or not optional_text(value):
            clean = optional_text(label)
            if clean and clean not in self.items:
                self.items.append(clean)

    def extend(self, values: Optional[Iterable[str]]) -> None:
        for value in values or []:
            clean = optional_text(value)
            if clean and clean not in self.items:
                self.items.append(clean)

    def lines(self) -> list[str]:
        if not self.items:
            return []
        return ["", "Veri eksikleri: " + " · ".join(self.items)]


def sanitize_provider_error(error: Any, *, cache_used: bool = False) -> str:
    """İç provider/HTTP/Python ayrıntısını kullanıcıya göstermeyen hata metni."""

    text = str(error or "").casefold()
    if cache_used:
        return "⚠️ Canlı veri kaynağı geçici olarak sınırlı. Analiz son güvenilir veriyle üretildi."
    if any(token in text for token in ("429", "rate limit", "too many request", "circuit breaker")):
        return "⚠️ Canlı veri kaynağı geçici olarak sınırlı; güncel analiz tamamlanamadı."
    if any(token in text for token in _INTERNAL_ERROR_TOKENS) or text:
        return "⚠️ Güncel veri yetersiz olduğu için bazı bölümler hesaplanamadı."
    return "⚠️ Veri kaynağına şu anda ulaşılamıyor."


def short_scenario_lines(label: str, zone: Any, *, condition_label: str = "Koşul") -> list[str]:
    if zone is None:
        return []
    low = finite_float(getattr(zone, "low", None))
    high = finite_float(getattr(zone, "high", None))
    if low is None or high is None:
        return []
    condition = next(iter(getattr(zone, "activation_conditions", []) or []), "")
    lines = [f"- {label}: {price_text(low)}–{price_text(high)}"]
    if condition:
        lines.append(f"  {condition_label}: {condition}")
    return lines


def detailed_scenario_lines(label: str, zone: Any) -> list[str]:
    if zone is None:
        return []
    lines = [f"{label}: {price_text(zone.low)}–{price_text(zone.high)}"]
    lines.append(f"Kanıt gücü: {score_text(getattr(zone, 'evidence_strength', getattr(zone, 'confidence', None)))}")
    horizon = optional_text(getattr(zone, "time_horizon", None))
    if horizon:
        lines.append(f"Zaman ufku: {horizon}")
    evidence = list(getattr(zone, "evidence", []) or [])
    if evidence:
        lines.append("Kaynaklar: " + ", ".join(evidence[:6]))
    activation = list(getattr(zone, "activation_conditions", []) or [])
    invalidation = list(getattr(zone, "invalidation_conditions", []) or [])
    if activation:
        lines.append("Aktivasyon: " + activation[0])
    if invalidation:
        lines.append("Geçersizlik: " + invalidation[0])
    contributions = getattr(zone, "score_breakdown", None)
    if contributions is not None:
        lines.extend(
            [
                f"Teknik yapı: {contributions.technical_structure:+.0f}",
                f"Trend: {contributions.trend:+.0f}",
                f"Hacim/likidite: {contributions.volume_liquidity:+.0f}",
                f"Göreceli güç: {contributions.relative_strength:+.0f}",
                f"Temel değerleme: {contributions.fundamental_valuation:+.0f}",
                f"Veri kalitesi cezası: {contributions.data_quality_penalty:.0f}",
                f"Spekülasyon riski cezası: {contributions.speculation_risk_penalty:.0f}",
            ]
        )
    return lines


def enforce_message_limit(text: str, *, limit: int = TELEGRAM_SAFE_MESSAGE_LENGTH) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n\nDetayların bir bölümü mesaj sınırı nedeniyle kısaltıldı."
    return text[: max(0, limit - len(suffix))].rstrip() + suffix
