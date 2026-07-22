from __future__ import annotations

"""Gerçek teknik seviyelerden hedef yol haritası üretimi.

Motor ara fiyat uydurmaz. Bölge genişliği, retest ve geçersizlik değerleri
girdideki gerçek teknik alanlardan gelir; hedefin sabit yüzdeleri kullanılmaz.
"""

from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.utils.financial_formatter import finite_float, percent_change, round_money


ROADMAP_STATUSES = {
    "Bekleniyor",
    "Test ediliyor",
    "Hacimli kırıldı",
    "Retest bekleniyor",
    "Başarılı retest",
    "Başarısız kırılım",
    "Gerçekleşti",
    "Geçersiz",
}


@dataclass
class TargetRoadmapStep:
    sequence: int
    price_low: float
    price_high: float
    mid: float
    level_type: str
    distance_percent: float
    importance: str
    evidence_strength: float
    breakout_condition: str
    volume_condition: str
    next_target: Optional[float]
    retest_zone: Optional[tuple[float, float]]
    invalidation_level: Optional[float]
    estimated_duration: str
    status: str = "Bekleniyor"
    evidence: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        """Eski kayıt katmanı için geriye uyumlu salt-okunur takma ad."""

        return self.evidence_strength

    @property
    def correction_zone(self) -> Optional[tuple[float, float]]:
        """Eski DB alan adı; Aşama 5f'te gerçek retest bölgesini ifade eder."""

        return self.retest_zone


@dataclass
class TargetRoadmapResult:
    current_price: float
    target_price: float
    steps: list[TargetRoadmapStep]
    reliable: bool
    note: str = ""


@dataclass
class _RoadmapCandidate:
    low: float
    high: float
    mid: float
    kind: str
    evidence_strength: float
    evidence: list[str]
    invalidation: Optional[float] = None


def _number(value) -> Optional[float]:
    result = finite_float(value)
    return result if result is not None and result > 0 else None


def _candidate_from_item(item) -> Optional[_RoadmapCandidate]:
    if isinstance(item, (int, float)):
        price = _number(item)
        if price is None:
            return None
        return _RoadmapCandidate(
            price,
            price,
            price,
            "Teknik seviye",
            55.0,
            ["sağlanan teknik seviye"],
        )

    mid = _number(getattr(item, "mid", getattr(item, "price", None)))
    if mid is None:
        return None
    low = _number(getattr(item, "low", getattr(item, "price_low", mid))) or mid
    high = _number(getattr(item, "high", getattr(item, "price_high", mid))) or mid
    kind = str(
        getattr(
            item,
            "technical_role",
            getattr(item, "scenario_class", getattr(item, "level_type", "Teknik bölge")),
        )
    )
    strength = _number(
        getattr(item, "evidence_strength", getattr(item, "confidence", 55.0))
    ) or 55.0
    evidence = [str(value) for value in getattr(item, "evidence", []) or []]
    if not evidence:
        evidence = [str(value) for value in getattr(item, "sources", []) or []]
    invalidation = _number(getattr(item, "invalidation_level", None))
    return _RoadmapCandidate(
        min(low, high),
        max(low, high),
        mid,
        kind,
        max(0.0, min(100.0, strength)),
        list(dict.fromkeys(evidence)),
        invalidation,
    )


def _deduplicate(candidates: list[_RoadmapCandidate], current: float) -> list[_RoadmapCandidate]:
    candidates.sort(key=lambda item: item.mid)
    tolerance = max(current * 0.015, 0.01)
    unique: list[_RoadmapCandidate] = []
    for candidate in candidates:
        if unique and (
            candidate.low <= unique[-1].high
            or abs(candidate.mid - unique[-1].mid) <= tolerance
        ):
            previous = unique[-1]
            keep = candidate if candidate.evidence_strength > previous.evidence_strength else previous
            keep.low = min(previous.low, candidate.low)
            keep.high = max(previous.high, candidate.high)
            keep.evidence = list(dict.fromkeys(previous.evidence + candidate.evidence))
            keep.evidence_strength = min(
                97.0,
                max(previous.evidence_strength, candidate.evidence_strength)
                + min(8.0, max(0, len(keep.evidence) - 1) * 1.5),
            )
            unique[-1] = keep
        else:
            unique.append(candidate)
    return unique


def _limit_steps(candidates: list[_RoadmapCandidate], maximum: int = 7) -> list[_RoadmapCandidate]:
    if len(candidates) <= maximum:
        return candidates
    target = candidates[-1]
    # En güçlü gerçek teknik seviyeleri tut; arada fiyat uydurulmaz.
    selected = sorted(candidates[:-1], key=lambda item: (-item.evidence_strength, item.mid))[: maximum - 1]
    return sorted(selected + [target], key=lambda item: item.mid)


def _horizon(candidate: _RoadmapCandidate, current: float, target: float) -> str:
    distance = percent_change(candidate.mid, current)
    if distance is None:
        return "Belirsiz"
    if not candidate.evidence and candidate.mid != target:
        return "Belirsiz"
    if distance <= 25:
        return "Kısa vade"
    if distance <= 100:
        return "Orta vade"
    return "Uzun vade" if candidate.evidence else "Belirsiz"


def build_target_roadmap(
    current_price: float,
    target_price: float,
    *,
    intermediate_levels: Optional[Iterable] = None,
    support_levels: Optional[Iterable] = None,
    volume_available: bool = True,
    target_source: str = "kullanıcı hedefi",
) -> TargetRoadmapResult:
    current = round_money(current_price)
    target = round_money(target_price)
    if current is None or target is None or current <= 0 or target <= current:
        return TargetRoadmapResult(
            current or 0.0,
            target or 0.0,
            [],
            False,
            "Yükseliş hedef yolu için geçerli fiyat/hedef bulunamadı.",
        )

    candidates: list[_RoadmapCandidate] = []
    for item in intermediate_levels or []:
        candidate = _candidate_from_item(item)
        if candidate is not None and current < candidate.mid < target:
            candidates.append(candidate)

    # Kullanıcı hedefi teknik bir motor hedefi gibi etiketlenmez ve bantla
    # genişletilmez; kullanıcının girdiği gerçek değer aynen korunur.
    candidates.append(
        _RoadmapCandidate(target, target, target, "Kullanıcı hedefi" if target_source == "kullanıcı hedefi" else "Motor ana hedefi", 50.0, [target_source])
    )
    candidates = _limit_steps(_deduplicate(candidates, current))

    supports: list[_RoadmapCandidate] = []
    for item in support_levels or []:
        candidate = _candidate_from_item(item)
        if candidate is not None and candidate.mid <= current:
            supports.append(candidate)
    supports.sort(key=lambda item: item.mid)
    anchor = supports[-1] if supports else _RoadmapCandidate(
        current,
        current,
        current,
        "Yol başlangıç fiyatı",
        0.0,
        ["güncel piyasa fiyatı"],
    )

    steps: list[TargetRoadmapStep] = []
    previous = anchor
    for index, candidate in enumerate(candidates):
        next_target = candidates[index + 1].mid if index + 1 < len(candidates) else None
        status = "Test ediliyor" if candidate.low <= current <= candidate.high else "Bekleniyor"
        invalidation = candidate.invalidation or previous.low
        importance = "Çok yüksek" if candidate.mid == target else (
            "Yüksek" if candidate.evidence_strength >= 65 else "Orta"
        )
        evidence = list(dict.fromkeys(candidate.evidence))
        steps.append(
            TargetRoadmapStep(
                sequence=index + 1,
                price_low=round(candidate.low, 2),
                price_high=round(candidate.high, 2),
                mid=round(candidate.mid, 2),
                level_type=candidate.kind,
                distance_percent=percent_change(candidate.mid, current) or 0.0,
                importance=importance,
                evidence_strength=round(candidate.evidence_strength, 1),
                breakout_condition=f"{candidate.high:.2f} TL üzerinde tamamlanmış kapanış",
                volume_condition=("Kırılımda olağan hacmin üzerinde teyit" if volume_available else ""),
                next_target=round(next_target, 2) if next_target is not None else None,
                retest_zone=(round(previous.low, 2), round(previous.high, 2)),
                invalidation_level=round(invalidation, 2) if invalidation is not None else None,
                estimated_duration=_horizon(candidate, current, target),
                status=status,
                evidence=evidence,
            )
        )
        previous = candidate
    return TargetRoadmapResult(current, target, steps, True)


def update_roadmap_status(
    step: TargetRoadmapStep,
    *,
    current_price: float,
    close_price: Optional[float] = None,
    relative_volume: Optional[float] = None,
) -> str:
    price = float(current_price)
    close = float(close_price if close_price is not None else current_price)
    if step.invalidation_level is not None and close < step.invalidation_level:
        return "Geçersiz"
    if step.price_low <= price <= step.price_high and close <= step.price_high:
        return "Test ediliyor"
    if close >= step.price_high:
        return "Hacimli kırıldı" if relative_volume is not None and relative_volume >= 1.2 else "Gerçekleşti"
    if step.retest_zone and step.retest_zone[0] <= price <= step.retest_zone[1]:
        return "Retest bekleniyor"
    return "Bekleniyor"
