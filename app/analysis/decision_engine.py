from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.analysis.liquidity_engine import LIQUIDITY_LOW, LIQUIDITY_VERY_LOW, LiquidityResult
from app.analysis.multi_timeframe_engine import MultiTimeframeResult
from app.analysis.signal_engine import SignalResult

# Bolum 6: nihai sinyal siniflari (Turkce, kullaniciya gosterilen kodlar).
DECISION_STRONG_BUY = "GUCLU_ALIM_ADAYI"
DECISION_BUY = "ALIM_ADAYI"
DECISION_WAIT_TRIGGER = "TETIK_BEKLENIYOR"
DECISION_HOLD = "TUT"
DECISION_NEUTRAL = "NOTR_ISLEM_YOK"
DECISION_REDUCE_RISK = "RISK_AZALT"
DECISION_SELL = "SATIS_ADAYI"
DECISION_STRONG_SELL_RISK = "GUCLU_SATIS_RISKI"
DECISION_INSUFFICIENT = "VERI_YETERSIZ"

DECISION_LABELS_TR = {
    DECISION_STRONG_BUY: "GÜÇLÜ ALIM ADAYI",
    DECISION_BUY: "ALIM ADAYI",
    DECISION_WAIT_TRIGGER: "TETİK BEKLENİYOR",
    DECISION_HOLD: "TUT",
    DECISION_NEUTRAL: "NÖTR / İŞLEM YOK",
    DECISION_REDUCE_RISK: "RİSK AZALT",
    DECISION_SELL: "SATIŞ ADAYI",
    DECISION_STRONG_SELL_RISK: "GÜÇLÜ SATIŞ RİSKİ",
    DECISION_INSUFFICIENT: "VERİ YETERSİZ",
}

# Mevcut (V3) sinyal motorunun ic kodlarindan yeni Turkce karar siniflarina eslestirme.
# NOT: bu, mevcut SignalResult.signal_type degerlerini DEGISTIRMEZ; sadece yeni,
# ek bir karar katmani (decision layer) uretir. Mevcut /analiz komutu ve
# veritabani semasi bundan etkilenmez.
_BASE_SIGNAL_MAP = {
    "STRONG_BUY_CANDIDATE": DECISION_STRONG_BUY,
    "BUY_CANDIDATE": DECISION_BUY,
    "WATCH": DECISION_WAIT_TRIGGER,
    "NEUTRAL": DECISION_NEUTRAL,
    "WEAK_RISK": DECISION_REDUCE_RISK,
    "REDUCE_POSITION": DECISION_SELL,
    "STRONG_RISK": DECISION_STRONG_SELL_RISK,
}

_CONFIDENCE_DEMOTION = {"yuksek": "orta", "orta": "dusuk", "dusuk": "dusuk"}


@dataclass
class DecisionResult:
    decision_class: str
    decision_label_tr: str
    base_signal_type: str
    base_score: float
    confidence: str
    is_actionable_buy: bool
    liquidity: Optional[LiquidityResult] = None
    multi_timeframe: Optional[MultiTimeframeResult] = None
    news_score: Optional[float] = None
    anomaly_score: Optional[float] = None
    gating_reasons: list[str] = field(default_factory=list)


def decide(
    signal: SignalResult,
    liquidity: Optional[LiquidityResult] = None,
    multi_timeframe: Optional[MultiTimeframeResult] = None,
    news_score: Optional[float] = None,
    anomaly_score: Optional[float] = None,
) -> DecisionResult:
    """Bolum 6 spesifikasyonuna gore nihai AL/SAT karar sinifini uretir.

    Bu fonksiyon mevcut sinyal motorunu (evaluate_signal) DEGISTIRMEZ; onun
    ciktisini girdi olarak alip, coklu zaman dilimi uyumu ve likidite
    filtresiyle birlikte degerlendirerek NIHAI, daha temkinli bir karar
    sinifi uretir (gerekirse guclu AL'i engeller, guveni dusurur).

    news_score / anomaly_score FAZ 1'de mevcut degildir (GDELT/anomali
    motorlari sonraki asamada eklenecek); None gecilirse notr kabul edilir
    ve karara ETKI ETMEZ (haber/anomali eksikligi asla haksiz ceza vermez).
    """
    gating_reasons: list[str] = []
    decision_class = _BASE_SIGNAL_MAP.get(signal.signal_type, DECISION_NEUTRAL)
    is_actionable = signal.is_actionable_buy
    confidence = signal.confidence

    # --- Likidite kapisi (bolum 5: dusuk likidite guclu AL'i engeller) ---
    if liquidity is not None:
        if not liquidity.available:
            gating_reasons.append("Likidite hesaplanamadi; guclu sinyal onerilmiyor.")
            if decision_class == DECISION_STRONG_BUY:
                decision_class = DECISION_BUY
        else:
            if not liquidity.allow_strong_signal and decision_class == DECISION_STRONG_BUY:
                decision_class = DECISION_BUY
                gating_reasons.append(
                    f"Likidite skoru yetersiz ({liquidity.score}/100); guclu AL sinifi engellendi."
                )
            if liquidity.liquidity_class in (LIQUIDITY_LOW, LIQUIDITY_VERY_LOW):
                gating_reasons.append(liquidity.risk_note or "Likidite dusuk, ani hareket riski var.")
                is_actionable = False
            if liquidity.manipulation_risk:
                gating_reasons.append("Manipulasyon riski gostergeleri mevcut; pozisyon buyuklugu azaltilmali.")
                is_actionable = False

    # --- Coklu zaman dilimi kapisi (bolum 4) ---
    if multi_timeframe is not None:
        if multi_timeframe.counter_trend_warning:
            gating_reasons.append(multi_timeframe.scenario_note)
            if decision_class in (DECISION_STRONG_BUY, DECISION_BUY):
                decision_class = DECISION_WAIT_TRIGGER
            is_actionable = False
        elif multi_timeframe.conflict:
            gating_reasons.append("Zaman dilimleri arasinda celiski var; guven bir kademe dusuruldu.")
            confidence = _CONFIDENCE_DEMOTION.get(confidence, confidence)
            if decision_class == DECISION_STRONG_BUY:
                decision_class = DECISION_BUY

    # Haber/anomali skorlari (Faz 1'de yer tutucu; sadece BILGI amacli tasinir,
    # karari degistirmez -- eksik veri asla ceza/odul olarak kullanilmaz).
    if news_score is not None and news_score <= -60 and decision_class in (DECISION_STRONG_BUY, DECISION_BUY):
        gating_reasons.append("Guclu negatif haber etkisi; sinyal temkinli degerlendirilmeli.")
        is_actionable = False
    if anomaly_score is not None and anomaly_score >= 85 and decision_class == DECISION_STRONG_BUY:
        gating_reasons.append("Kritik anormal hareket; guclu AL yerine dogrulama beklenmeli.")
        decision_class = DECISION_BUY

    if decision_class == DECISION_STRONG_BUY and not is_actionable:
        decision_class = DECISION_BUY

    label = DECISION_LABELS_TR.get(decision_class, decision_class)

    return DecisionResult(
        decision_class=decision_class,
        decision_label_tr=label,
        base_signal_type=signal.signal_type,
        base_score=signal.score,
        confidence=confidence,
        is_actionable_buy=is_actionable and decision_class in (DECISION_STRONG_BUY, DECISION_BUY),
        liquidity=liquidity,
        multi_timeframe=multi_timeframe,
        news_score=news_score,
        anomaly_score=anomaly_score,
        gating_reasons=gating_reasons,
    )
