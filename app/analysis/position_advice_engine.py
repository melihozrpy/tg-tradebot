from __future__ import annotations

"""MERGEN QUANT - Asama 5c, Bolum 1: Pozisyonu olan/olmayan kullanici icin
ayri analiz ve yorum sistemi.

KURAL: Kararlar (TUT / KISMI KAR AL / POZISYON AZALT / STOP-TAM CIKIS)
kullanicinin MALIYETINE gore degil, TEKNIK YAPIYA (destek/direnc, hedefler,
stop, sinyal/karar motoru ciktisi) gore hesaplanir. Maliyet yalnizca
kar/zarar GORUNURLUGU icin kullanilir, karar mantigina girdi olarak
KULLANILMAZ.

Bu modul tamamen deterministik Python kodudur; Groq/LLM hicbir sekilde
fiyat, stop, hedef ya da TUT/SAT karari uretmez.
"""

from dataclasses import dataclass
from typing import Optional

DECISION_HOLD = "TUT"
DECISION_PARTIAL_PROFIT = "KISMI_KAR_AL"
DECISION_REDUCE = "POZISYON_AZALT"
DECISION_FULL_EXIT = "STOP_TAM_CIKIS"

_DECISION_LABELS = {
    DECISION_HOLD: "TUT",
    DECISION_PARTIAL_PROFIT: "KISMİ KÂR AL",
    DECISION_REDUCE: "POZİSYON AZALT",
    DECISION_FULL_EXIT: "STOP - TAM ÇIKIŞ",
}


def decision_label(decision: str) -> str:
    return _DECISION_LABELS.get(decision, decision)


@dataclass
class PositionAdviceResult:
    decision: str
    decision_label: str
    rationale: list[str]
    lot: float
    average_cost: float
    current_price: float
    pnl_amount: float
    pnl_percent: float
    portfolio_weight_pct: Optional[float]
    technical_stop: Optional[float]
    target_1: Optional[float]
    target_2: Optional[float]
    target_3: Optional[float]
    partial_profit_zone: Optional[tuple]
    reduce_zone: Optional[tuple]
    full_exit_level: Optional[float]
    estimated_loss_if_stopped: Optional[float]


def evaluate_position(
    *,
    lot: float,
    average_cost: float,
    current_price: float,
    technical_stop: Optional[float],
    target_1: Optional[float],
    target_2: Optional[float],
    target_3: Optional[float],
    main_resistance: Optional[float],
    decision_class: Optional[str],
    trend_direction: Optional[str],
    portfolio_weight_pct: Optional[float] = None,
) -> PositionAdviceResult:
    """Teknik yapiya gore pozisyon karari hesaplar.

    decision_class: DecisionEngine'in genel karar sinifi (orn. "guclu_al",
    "al", "notr", "sat", "guclu_sat", "kacin") - mevcut ise karar agirligini
    etkiler ama TEK BASINA belirleyici degildir; asil belirleyici stop/hedef
    yapisina gore fiyatin nerede oldugudur.
    """
    pnl_amount = round((current_price - average_cost) * lot, 2)
    cost_value = lot * average_cost
    pnl_percent = round((pnl_amount / cost_value) * 100, 2) if cost_value else 0.0

    estimated_loss_if_stopped = None
    if technical_stop is not None:
        estimated_loss_if_stopped = round((technical_stop - average_cost) * lot, 2)

    rationale: list[str] = []
    decision = DECISION_HOLD

    stop_hit = technical_stop is not None and current_price <= technical_stop
    if stop_hit:
        decision = DECISION_FULL_EXIT
        rationale.append(
            f"Fiyat ({current_price:.2f}) teknik stop seviyesinin ({technical_stop:.2f}) altında/eşiğinde; "
            "teknik yapı bozuldu."
        )
    else:
        reached_t3 = target_3 is not None and current_price >= target_3
        reached_t2 = target_2 is not None and current_price >= target_2
        reached_t1 = target_1 is not None and current_price >= target_1
        near_resistance = (
            main_resistance is not None
            and current_price > 0
            and abs(main_resistance - current_price) / current_price <= 0.02
        )

        if reached_t3:
            decision = DECISION_REDUCE
            rationale.append(f"Fiyat Hedef 3 seviyesine ({target_3:.2f}) ulaştı; kalan pozisyon için azaltma bölgesi.")
        elif reached_t2:
            decision = DECISION_PARTIAL_PROFIT
            rationale.append(f"Fiyat Hedef 2 seviyesine ({target_2:.2f}) ulaştı; kısmi kâr alma bölgesi.")
        elif reached_t1:
            decision = DECISION_PARTIAL_PROFIT
            rationale.append(f"Fiyat Hedef 1 seviyesine ({target_1:.2f}) ulaştı; kısmi kâr alma bölgesi.")
        elif near_resistance:
            decision = DECISION_PARTIAL_PROFIT
            rationale.append(
                f"Fiyat ana dirence ({main_resistance:.2f}) yakın; kısmi kâr alma değerlendirilebilir."
            )
        elif decision_class in ("SATIS_ADAYI", "GUCLU_SATIS_RISKI", "RISK_AZALT") and trend_direction == "down":
            decision = DECISION_REDUCE
            rationale.append("Karar motoru satış/risk yönlü ve trend aşağı; pozisyon azaltma değerlendirilebilir.")
        else:
            rationale.append("Teknik yapı (stop/hedef) bozulmadı; mevcut planla tutmaya devam edilebilir.")

    partial_profit_zone = None
    if target_1 is not None and target_2 is not None:
        partial_profit_zone = (round(min(target_1, target_2), 2), round(max(target_1, target_2), 2))
    reduce_zone = None
    if target_2 is not None and target_3 is not None:
        reduce_zone = (round(min(target_2, target_3), 2), round(max(target_2, target_3), 2))
    full_exit_level = technical_stop

    return PositionAdviceResult(
        decision=decision,
        decision_label=decision_label(decision),
        rationale=rationale,
        lot=lot,
        average_cost=average_cost,
        current_price=current_price,
        pnl_amount=pnl_amount,
        pnl_percent=pnl_percent,
        portfolio_weight_pct=portfolio_weight_pct,
        technical_stop=technical_stop,
        target_1=target_1,
        target_2=target_2,
        target_3=target_3,
        partial_profit_zone=partial_profit_zone,
        reduce_zone=reduce_zone,
        full_exit_level=full_exit_level,
        estimated_loss_if_stopped=estimated_loss_if_stopped,
    )
