from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.analysis.target_realism_engine import TargetRealismResult, evaluate_target_realism
from app.analysis.target_roadmap_engine import TargetRoadmapResult, build_target_roadmap


@dataclass
class UserTargetEvaluation:
    symbol: str
    current_price: float
    user_target: float
    realism: TargetRealismResult
    roadmap: TargetRoadmapResult
    technical_class: str
    fundamental_valuation_class: str
    liquidity_suitability: str
    market_support: str
    required_close_conditions: list[str] = field(default_factory=list)
    invalidation_conditions: list[str] = field(default_factory=list)
    estimated_horizon: str = "Veri yetersiz"
    risk_class: str = "Veri yetersiz"
    evidence_strength: Optional[float] = None


def evaluate_user_target(
    symbol: str,
    current_price: float,
    user_target: float,
    *,
    intermediate_levels: Optional[Iterable] = None,
    support_levels: Optional[Iterable] = None,
    valuation_class: Optional[str] = None,
    market_support: Optional[str] = None,
    **realism_inputs,
) -> UserTargetEvaluation:
    realism = evaluate_target_realism(current_price, user_target, **realism_inputs)
    roadmap = build_target_roadmap(
        current_price, user_target, intermediate_levels=intermediate_levels,
        support_levels=support_levels,
        volume_available=realism_inputs.get("liquidity_score") is not None,
        target_source="kullanıcı hedefi",
    )
    multiple = realism.required_price_multiple or 0
    if multiple <= 1.25:
        horizon = "Kısa vade"
    elif multiple <= 2:
        horizon = "Orta vade"
    else:
        horizon = "Uzun vade" if roadmap.steps else "Belirsiz"
    risk = "Çok yüksek" if realism.speculation_risk == "Yüksek" else (
        "Yüksek" if realism.liquidity_risk in {"Yüksek", "Çok yüksek", "Veri yetersiz"} else "Orta"
    )
    resistances = [step.mid for step in roadmap.steps]
    return UserTargetEvaluation(
        symbol=symbol.upper(), current_price=current_price, user_target=user_target,
        realism=realism, roadmap=roadmap,
        technical_class=realism.technical_probability_class,
        fundamental_valuation_class=valuation_class or realism.fundamental_support_class,
        liquidity_suitability=("Uygun değil" if realism.liquidity_risk in {"Yüksek", "Çok yüksek"} else "Veri yetersiz" if realism.liquidity_risk == "Veri yetersiz" else "Kısmen uygun"),
        market_support=market_support or "XU100 ve sektör verisi yetersiz",
        required_close_conditions=[
            f"Ara bölgelerin sırayla aşılması: {' → '.join(f'{x:.2f}' for x in resistances)}" if resistances else "Teknik yol doğrulanmalı",
            "Kırılımlar kapanış ve hacimle teyit edilmeli",
        ],
        invalidation_conditions=[
            "İlk yol haritası adımının geçersizlik seviyesinin altında kapanış" if roadmap.steps else "Teknik geçersizlik verisi yetersiz",
            "XU100/sektör yönünün kalıcı biçimde tersine dönmesi",
        ],
        estimated_horizon=horizon,
        risk_class=risk,
        evidence_strength=realism.realism_score,
    )
