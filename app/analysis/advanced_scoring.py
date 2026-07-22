from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.analysis.relative_strength_engine import RelativeStrengthResult
from app.analysis.signal_engine import SignalResult

# Bolum 17 spesifikasyonundaki puan havuzlari (toplam 100)
MAX_TREND = 20.0
MAX_MOMENTUM = 10.0
MAX_VOLUME = 15.0
MAX_SR = 15.0
MAX_XU100 = 15.0
MAX_SECTOR = 10.0
MAX_REGIME = 10.0
MAX_RISK_REWARD = 5.0

# V2 skor motorundaki eski havuz boyutlari (extras icindeki ham puanlarin
# olceklenmesi icin referans)
_OLD_MAX_TREND = 25.0
_OLD_MAX_MOMENTUM = 15.0
_OLD_MAX_VOLUME = 20.0
_OLD_MAX_REGIME = 15.0
_OLD_MAX_LIQUIDITY = 10.0


@dataclass
class AdvancedScoreBreakdown:
    trend: float
    momentum: float
    volume: float
    support_resistance: float
    xu100_strength: float
    xu100_strength_available: bool
    sector_strength: float
    sector_strength_available: bool
    regime: float
    risk_reward: float
    total: float
    # V3.2 (Asama 4, bolum 4): haber etkisinin toplam skora eklenen kismi.
    # Varsayilan 0.0'dir (haber yok/GDELT kapali); [-3, +3] araligiyla
    # sinirlidir ve `total` alanina ZATEN eklenmis olarak yansitilir.
    news_adjustment: float = 0.0


def _scale(value: float, old_max: float, new_max: float) -> float:
    if old_max <= 0:
        return 0.0
    return round(min(max(value, 0.0), old_max) / old_max * new_max, 2)


def _support_resistance_score(signal: SignalResult) -> float:
    """Destek/direnc konumuna gore puan (0-15).

    Fiyatin yapisal olarak saglam bir konumda olmasi (destek kirilmamis,
    direnc altinda hala yer var, destekten tepki ihtimali) puani artirir.
    """
    sr = signal.support_resistance
    if sr is None:
        return MAX_SR * 0.5  # S/R hesaplanmadan (df verilmeden) cagrilmissa notr puan

    score = 0.0
    if sr.support_reliable:
        score += 5
    if sr.resistance_reliable:
        score += 3
    if not sr.support_broken_with_volume:
        score += 4
    else:
        score -= 4
    if sr.price_reacting_off_support:
        score += 3
    if sr.price_below_main_resistance:
        score += 2  # direnc oncesi hala hareket alani var

    return round(min(max(score, 0.0), MAX_SR), 2)


def _relative_strength_score(rs: Optional[RelativeStrengthResult], max_points: float) -> tuple[float, bool]:
    if rs is None or not rs.available or rs.relative_score is None:
        return round(max_points * 0.5, 2), False  # veri yoksa NOTR puan; digerlerine aktarilmaz
    return round((rs.relative_score / 100.0) * max_points, 2), True


def compute_advanced_score(
    signal: SignalResult,
    xu100_relative_strength: Optional[RelativeStrengthResult] = None,
    sector_relative_strength: Optional[RelativeStrengthResult] = None,
) -> AdvancedScoreBreakdown:
    """V2'nin temel SignalResult'indan (extras icindeki ham bilesen puanlari)
    V3'un 8 kategorili 0-100 skorunu yeniden hesaplar.

    Sektor/XU100 verisi yoksa o kategori NOTR (yarim puan) sayilir; digerlerine
    puan aktarilmaz (spesifikasyon: 'kalan puanlari otomatik şişirme').
    """
    extras = signal.extras

    trend = _scale(extras.get("trend_score", 0.0), _OLD_MAX_TREND, MAX_TREND)
    momentum = _scale(extras.get("momentum_score", 0.0), _OLD_MAX_MOMENTUM, MAX_MOMENTUM)
    volume = _scale(extras.get("volume_score", 0.0), _OLD_MAX_VOLUME, MAX_VOLUME)
    regime = _scale(extras.get("regime_score", 0.0), _OLD_MAX_REGIME, MAX_REGIME)
    liquidity_component = _scale(extras.get("liquidity_score", 0.0), _OLD_MAX_LIQUIDITY, MAX_RISK_REWARD)

    sr_score = _support_resistance_score(signal)
    xu100_score, xu100_available = _relative_strength_score(xu100_relative_strength, MAX_XU100)
    sector_score, sector_available = _relative_strength_score(sector_relative_strength, MAX_SECTOR)

    total = round(
        trend + momentum + volume + sr_score + xu100_score + sector_score + regime + liquidity_component, 1
    )
    total = min(max(total, 0.0), 100.0)

    return AdvancedScoreBreakdown(
        trend=trend,
        momentum=momentum,
        volume=volume,
        support_resistance=sr_score,
        xu100_strength=xu100_score,
        xu100_strength_available=xu100_available,
        sector_strength=sector_score,
        sector_strength_available=sector_available,
        regime=regime,
        risk_reward=liquidity_component,
        total=total,
    )
