from __future__ import annotations

from dataclasses import dataclass, field

from app.analysis.market_regime_engine import REGIME_STRONG_DOWN
from app.analysis.signal_engine import SignalResult

CONFIDENCE_DEMOTION = {"yuksek": "orta", "orta": "dusuk", "dusuk": "dusuk"}


@dataclass
class ConsistencyResult:
    is_consistent: bool
    issues: list[str] = field(default_factory=list)


def validate_signal_consistency(signal: SignalResult, close: float) -> ConsistencyResult:
    """Sinyal ciktisindaki olasi ic celiskileri kontrol eder (bkz. spesifikasyon
    bolum 18). Sorun bulunursa listeyi doner; cagiran taraf (analysis_service)
    bu listeye gore guveni dusurur ve/veya analiz durumunu 'guvenli' hale getirir.

    Bu fonksiyon sinyali degistirmez, yalnizca DENETLER; asil karar
    analysis_service'te (guveni dusurme / notr'a cekme) uygulanir.
    """
    issues: list[str] = []

    sr = signal.support_resistance
    if sr is not None:
        if sr.support_1 is not None and sr.support_1 >= close:
            issues.append("Destek 1 son fiyatin altinda degil.")
        if sr.support_2 is not None and sr.support_2 >= close:
            issues.append("Destek 2 son fiyatin altinda degil.")
        if sr.resistance_1 is not None and sr.resistance_1 <= close:
            issues.append("Direnc 1 son fiyatin ustunde degil.")
        if sr.resistance_2 is not None and sr.resistance_2 <= close:
            issues.append("Direnc 2 son fiyatin ustunde degil.")

    entry_low, entry_high = signal.entry_zone
    if signal.stop_price is not None and entry_low is not None and signal.stop_price >= entry_low:
        issues.append("Stop, giris bolgesinin altinda degil.")

    targets = [t for t in (signal.target_1, signal.target_2, signal.target_3) if t is not None]
    if targets and entry_high is not None and targets[0] <= entry_high:
        issues.append("Hedef 1, giris bolgesinin ustunde degil.")
    for i in range(1, len(targets)):
        if targets[i] <= targets[i - 1]:
            issues.append("Hedefler artan sirada degil.")
            break

    if signal.is_actionable_buy:
        min_rr = 2.0
        if signal.risk_reward is None or signal.risk_reward < min_rr:
            issues.append("Guclu alim sinyali risk/getiri ile uyumsuz.")
        if signal.market_regime == REGIME_STRONG_DOWN:
            issues.append("Guclu alim sinyali piyasa rejimiyle (guclu dusus) uyumsuz.")

    risk_reason_count = sum(1 for r in signal.reasons if r.is_risk)
    positive_reason_count = len(signal.reasons) - risk_reason_count
    if signal.signal_type in ("STRONG_BUY_CANDIDATE", "BUY_CANDIDATE") and risk_reason_count > positive_reason_count:
        issues.append("Sinyal turu ile ana nedenlerin cogunlugu uyumsuz (riskler baskin).")

    return ConsistencyResult(is_consistent=len(issues) == 0, issues=issues)


def apply_consistency_guard(signal: SignalResult, result: ConsistencyResult) -> SignalResult:
    """Tutarsizlik bulunursa guveni dusurur ve guclu alim bayragini kaldirir.
    Skor/sinyal turu GERIYE DONUK degistirilmez; yalnizca 'aksiyona gecilebilir'
    bayragi ve guven seviyesi guvenli tarafa cekilir.
    """
    if result.is_consistent:
        return signal

    signal.confidence = CONFIDENCE_DEMOTION.get(signal.confidence, "dusuk")
    if signal.is_actionable_buy:
        signal.is_actionable_buy = False
    signal.contextual_notes = signal.contextual_notes + [
        f"Tutarlilik kontrolu uyari verdi ({len(result.issues)} bulgu); guven seviyesi dusuruldu."
    ]
    return signal
