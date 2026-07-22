from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from app.analysis.indicator_engine import TechnicalSnapshot
from app.analysis.market_regime_engine import (
    REGIME_STRONG_DOWN,
    REGIME_STRONG_UP,
    REGIME_VOLATILE,
    REGIME_WEAK_UP,
    MarketRegimeResult,
)
from app.analysis.support_resistance_engine import (
    SupportResistanceResult,
    compute_support_resistance,
    round2,
)

# Stop mesafesi guvenlik siniri: fiyatin bu oranindan daha dar ya da daha
# genis stop'lar "mantiksiz" sayilir ve guclu alim sinyaline izin verilmez.
MIN_STOP_DISTANCE_PERCENT = 0.005
MAX_STOP_DISTANCE_PERCENT = 0.15


@dataclass
class SignalReasonItem:
    category: str
    description: str
    is_risk: bool = False


@dataclass
class SignalResult:
    symbol: str
    timeframe: str
    score: float
    signal_type: str
    confidence: str
    reasons: list[SignalReasonItem]
    entry_zone: tuple[Optional[float], Optional[float]]
    stop_price: Optional[float]
    target_1: Optional[float]
    target_2: Optional[float]
    risk_reward: Optional[float]
    market_regime: str
    data_timestamp: datetime
    provider: str
    strategy_version: str
    idempotency_key: str
    is_actionable_buy: bool
    invalidation_note: str
    extras: dict = field(default_factory=dict)
    # Genisletilmis alanlar (df verildiginde doldurulur; aksi halde None/varsayilan kalir)
    target_3: Optional[float] = None
    entry_trigger: Optional[float] = None
    daily_change_percent: Optional[float] = None
    support_resistance: Optional[SupportResistanceResult] = None
    contextual_notes: list[str] = field(default_factory=list)


def _score_trend(snap: TechnicalSnapshot) -> tuple[float, list[SignalReasonItem]]:
    reasons: list[SignalReasonItem] = []
    score = 0.0
    max_score = 25.0

    if snap.trend_direction == "up":
        score += 12
        reasons.append(SignalReasonItem("trend", "EMA20 > EMA50 ve fiyat EMA20 uzerinde (yukselis yapisi)"))
    elif snap.trend_direction == "down":
        reasons.append(SignalReasonItem("trend", "EMA20 < EMA50 ve fiyat EMA20 altinda (dusus yapisi)", is_risk=True))
    else:
        score += 4
        reasons.append(SignalReasonItem("trend", "Fiyat yatay bir yapida, net trend yok"))

    if snap.adx >= 25:
        score += 8
        if snap.trend_direction == "sideways":
            reasons.append(
                SignalReasonItem(
                    "trend",
                    f"ADX {snap.adx:.1f}: Piyasada guclu hareket var; yon diger trend gostergeleriyle belirlenmistir",
                )
            )
        else:
            reasons.append(SignalReasonItem("trend", f"ADX guclu trend gosteriyor ({snap.adx:.1f})"))
    elif snap.adx >= 18:
        score += 4
        reasons.append(SignalReasonItem("trend", f"ADX orta seviye trend gucu ({snap.adx:.1f})"))
    else:
        reasons.append(SignalReasonItem("trend", f"ADX dusuk, trend zayif ({snap.adx:.1f})"))

    if snap.ema200 is not None:
        if snap.close > snap.ema200:
            score += 5
            reasons.append(SignalReasonItem("trend", "Fiyat EMA200 uzerinde (uzun vadeli yukselis)"))
        else:
            reasons.append(SignalReasonItem("trend", "Fiyat EMA200 altinda (uzun vadeli zayiflik)", is_risk=True))

    return min(score, max_score), reasons


def _score_volume(snap: TechnicalSnapshot) -> tuple[float, list[SignalReasonItem]]:
    reasons: list[SignalReasonItem] = []
    score = 0.0
    max_score = 20.0

    if snap.relative_volume >= 1.4:
        score += 10
        reasons.append(SignalReasonItem("volume", f"Goreceli hacim ortalamanin uzerinde ({snap.relative_volume:.2f}x)"))
    elif snap.relative_volume >= 1.0:
        score += 5
        reasons.append(SignalReasonItem("volume", f"Goreceli hacim normal seviyede ({snap.relative_volume:.2f}x)"))
    else:
        reasons.append(
            SignalReasonItem("volume", f"Hacim dusuk, hareket hacimle dogrulanmiyor ({snap.relative_volume:.2f}x)", is_risk=True)
        )

    if snap.obv_trend_up:
        score += 6
        reasons.append(SignalReasonItem("volume", "OBV yukselen egilimde (para girisi)"))
    else:
        reasons.append(SignalReasonItem("volume", "OBV yukselmiyor (para girisi zayif)", is_risk=True))

    if snap.mfi >= 50:
        score += 4
        reasons.append(SignalReasonItem("volume", f"Money Flow Index pozitif bolgede ({snap.mfi:.1f})"))
    else:
        reasons.append(SignalReasonItem("volume", f"Money Flow Index zayif ({snap.mfi:.1f})"))

    return min(score, max_score), reasons


def _score_momentum(snap: TechnicalSnapshot) -> tuple[float, list[SignalReasonItem]]:
    reasons: list[SignalReasonItem] = []
    score = 0.0
    max_score = 15.0

    if 45 <= snap.rsi <= 68:
        score += 7
        reasons.append(SignalReasonItem("momentum", f"RSI saglikli momentum bolgesinde ({snap.rsi:.1f})"))
    elif snap.rsi > 68:
        score += 2
        reasons.append(SignalReasonItem("momentum", f"RSI asiri alim bolgesine yakin ({snap.rsi:.1f})", is_risk=True))
    else:
        reasons.append(SignalReasonItem("momentum", f"RSI zayif bolgede ({snap.rsi:.1f})"))

    if snap.macd_histogram > 0 and snap.macd_line > snap.macd_signal:
        score += 8
        reasons.append(SignalReasonItem("momentum", "MACD pozitif kesisim, momentum yukari"))
    else:
        reasons.append(SignalReasonItem("momentum", "MACD momentum destegi yok"))

    return min(score, max_score), reasons


def _score_regime(regime_result: MarketRegimeResult) -> tuple[float, list[SignalReasonItem]]:
    reasons: list[SignalReasonItem] = []
    regime_score_map = {
        REGIME_STRONG_UP: 15.0,
        REGIME_WEAK_UP: 10.0,
        "yatay": 6.0,
        "dagitim": 2.0,
        "zayif_dusus": 1.0,
        REGIME_STRONG_DOWN: 0.0,
        REGIME_VOLATILE: 3.0,
        "veri_yetersiz": 0.0,
    }
    score = regime_score_map.get(regime_result.regime, 0.0)
    reasons.append(SignalReasonItem("regime", f"Piyasa rejimi: {regime_result.regime} ({regime_result.detail})"))
    if regime_result.regime in (REGIME_STRONG_DOWN,):
        reasons.append(SignalReasonItem("regime", "Guclu dusus rejiminde AL esigi yukseltilir", is_risk=True))
    if regime_result.regime == REGIME_VOLATILE:
        reasons.append(SignalReasonItem("regime", "Asiri volatil rejim: pozisyon buyuklugu azaltilmali", is_risk=True))
    return score, reasons


def _score_fundamental_kap(fundamental_status: str, kap_status: str) -> tuple[float, list[SignalReasonItem]]:
    """FAZ 1'de temel/KAP saglayicilari devre disi oldugu icin notr puan verir.

    Bu asla veri uydurmaz; sadece 'mevcut degil' bilgisini nötr (orta) bir
    puanla yansitir ki eksik veri sinyali haksiz yere cezalandirmasin ya da
    odullendirmesin.
    """
    reasons = [
        SignalReasonItem("fundamental", f"Temel veri durumu: {fundamental_status} (FAZ 1'de devre disi)"),
        SignalReasonItem("fundamental", f"KAP veri durumu: {kap_status} (FAZ 1'de devre disi)"),
    ]
    return 7.5, reasons  # 15 puanlik havuzun yarisi -> notr


def _score_liquidity_risk(snap: TechnicalSnapshot, atr_stop_multiplier: float) -> tuple[float, list[SignalReasonItem], Optional[float]]:
    reasons: list[SignalReasonItem] = []
    score = 0.0
    max_score = 10.0

    stop_distance = snap.atr * atr_stop_multiplier
    stop_price = round(snap.close - stop_distance, 2) if stop_distance > 0 else None

    if stop_price is not None and stop_price > 0 and stop_distance / snap.close > 0.005:
        score += 6
        reasons.append(SignalReasonItem("risk", f"ATR tabanli stop hesaplanabiliyor ({stop_price})"))
    else:
        reasons.append(SignalReasonItem("risk", "Stop mesafesi cok dar/anlamsiz, sinyal guvenilmez", is_risk=True))
        stop_price = None

    if snap.relative_volume >= 1.0:
        score += 4
        reasons.append(SignalReasonItem("risk", "Likidite yeterli gorunuyor (goreceli hacim >= 1.0)"))
    else:
        reasons.append(SignalReasonItem("risk", "Likidite dusuk olabilir", is_risk=True))

    return min(score, max_score), reasons, stop_price


def _compute_daily_change_percent(df: pd.DataFrame) -> Optional[float]:
    if df is None or len(df) < 2:
        return None
    prev_close = float(df.iloc[-2]["close"])
    last_close = float(df.iloc[-1]["close"])
    if prev_close <= 0:
        return None
    return round(((last_close - prev_close) / prev_close) * 100, 2)


def _compute_trade_plan(
    snapshot: TechnicalSnapshot,
    sr_result: SupportResistanceResult,
    risk_cfg: dict,
    thresholds: dict,
) -> dict:
    """Destek/direnc seviyelerine dayali giris, stop ve hedef planini hesaplar.

    Stop asla rastgele bir yuzde degildir: en yakin guvenilir destege, ATR'ye
    ve gunluk volatiliteye birlikte bakilir. Hedefler gercek direnc
    seviyelerinden turetilir; rastgele yuzdelik hedef uretilmez.
    """
    close = snapshot.close
    atr = snapshot.atr
    contextual_notes: list[str] = []
    extra_reasons: list[SignalReasonItem] = []

    # --- STOP HESABI -----------------------------------------------------
    atr_based_stop = close - (atr * risk_cfg["atr_stop_multiplier"])
    support_based_stop = None
    if sr_result.support_1 is not None and sr_result.support_1 < close:
        # Destegin biraz altina, stop-hunting'i azaltmak icin kucuk bir tampon.
        support_based_stop = sr_result.support_1 - (atr * 0.15)

    if support_based_stop is not None:
        stop_candidate = support_based_stop
    else:
        stop_candidate = atr_based_stop

    stop_price = round2(stop_candidate)
    stop_is_valid = False
    if stop_price is not None and stop_price > 0 and close > 0:
        distance_percent = (close - stop_price) / close
        if MIN_STOP_DISTANCE_PERCENT <= distance_percent <= MAX_STOP_DISTANCE_PERCENT:
            stop_is_valid = True
        elif distance_percent < MIN_STOP_DISTANCE_PERCENT:
            extra_reasons.append(
                SignalReasonItem("risk", "Stop mesafesi cok dar, sinyal guvenilir sayilmadi", is_risk=True)
            )
        else:
            extra_reasons.append(
                SignalReasonItem("risk", "Stop mesafesi cok genis/mantiksiz, sinyal guvenilir sayilmadi", is_risk=True)
            )

    if not stop_is_valid:
        stop_price = None

    # --- HEDEF HESABI (gercek direnc seviyelerinden) ----------------------
    target_1 = target_2 = target_3 = risk_reward = None
    entry_trigger = None

    if stop_price is not None:
        risk_per_share = close - stop_price
        if risk_per_share > 0:
            fallback_t1 = close + risk_per_share * 1.5
            fallback_t2 = close + risk_per_share * 2.5
            fallback_t3 = close + risk_per_share * 3.5

            target_1 = sr_result.resistance_1 if (sr_result.resistance_1 and sr_result.resistance_1 > close) else round2(fallback_t1)
            target_2_candidate = sr_result.resistance_2 if (sr_result.resistance_2 and sr_result.resistance_2 > target_1) else round2(fallback_t2)
            target_2 = target_2_candidate if target_2_candidate > target_1 else round2(target_1 + risk_per_share)
            target_3_candidate = (
                sr_result.main_resistance if (sr_result.main_resistance and sr_result.main_resistance > target_2) else round2(fallback_t3)
            )
            target_3 = target_3_candidate if target_3_candidate > target_2 else round2(target_2 + risk_per_share)

            risk_reward = round((target_1 - close) / risk_per_share, 2)

    if sr_result.price_below_main_resistance and sr_result.resistance_1:
        entry_trigger = sr_result.resistance_1
        contextual_notes.append("Direnc kirilimi bekleniyor.")
    else:
        entry_trigger = round2(close)

    if sr_result.price_reacting_off_support:
        contextual_notes.append("Destekten tepki ihtimali.")

    if sr_result.support_broken_with_volume:
        contextual_notes.append("Destek kirildi, risk artti.")
        extra_reasons.append(SignalReasonItem("risk", "Destek hacimli sekilde kirildi", is_risk=True))
        stop_is_valid = False
        stop_price = None
        target_1 = target_2 = target_3 = risk_reward = None

    if risk_reward is not None and risk_reward < thresholds["minimum_risk_reward"]:
        contextual_notes.append("Risk/getiri yetersiz.")
        extra_reasons.append(SignalReasonItem("risk", f"Risk/getiri orani yetersiz ({risk_reward})", is_risk=True))

    entry_low = round2(close * 0.995)
    entry_high = round2(close * 1.01)

    invalidation_note = (
        f"Fiyat {stop_price} altina kapanirsa senaryo gecersiz olur."
        if stop_price is not None
        else "Guvenilir stop hesaplanamadigi icin senaryo net degil."
    )

    return {
        "entry_zone": (entry_low, entry_high),
        "entry_trigger": entry_trigger,
        "stop_price": stop_price,
        "stop_is_valid": stop_is_valid,
        "target_1": round2(target_1) if target_1 is not None else None,
        "target_2": round2(target_2) if target_2 is not None else None,
        "target_3": round2(target_3) if target_3 is not None else None,
        "risk_reward": risk_reward,
        "invalidation_note": invalidation_note,
        "contextual_notes": contextual_notes,
        "extra_reasons": extra_reasons,
    }


# Disariya acik (public) surum: /gunici gibi baska servislerin ayni giris/stop/
# hedef mantigini (destek/direnc tabanli, fiyat uydurmayan) yeniden kullanabilmesi
# icin. Ic mantik degismez, sadece alt cizgisiz bir takma ad saglanir.
compute_trade_plan = _compute_trade_plan


def build_idempotency_key(symbol: str, timeframe: str, signal_type: str, data_timestamp: datetime) -> str:
    raw = f"{symbol}|{timeframe}|{signal_type}|{data_timestamp.isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def classify_signal_type(score: float, thresholds: dict) -> str:
    if score >= thresholds["strong_buy_score"]:
        return "STRONG_BUY_CANDIDATE"
    if score >= thresholds["buy_score"]:
        return "BUY_CANDIDATE"
    if score >= thresholds["watch_score"]:
        return "WATCH"
    if score >= 50:
        return "NEUTRAL"
    if score >= thresholds["risk_reduce_score"]:
        return "WEAK_RISK"
    if score >= thresholds["sell_risk_score"]:
        return "REDUCE_POSITION"
    return "STRONG_RISK"


def evaluate_signal(
    snapshot: TechnicalSnapshot,
    regime_result: MarketRegimeResult,
    provider_name: str,
    strategy_config: dict,
    fundamental_status: str = "unavailable",
    kap_status: str = "unavailable",
    df: Optional[pd.DataFrame] = None,
) -> SignalResult:
    thresholds = strategy_config["thresholds"]
    risk_cfg = strategy_config["risk"]
    strategy_version = strategy_config["strategy"]["version"]

    trend_score, trend_reasons = _score_trend(snapshot)
    volume_score, volume_reasons = _score_volume(snapshot)
    momentum_score, momentum_reasons = _score_momentum(snapshot)
    regime_score, regime_reasons = _score_regime(regime_result)
    fundamental_score, fundamental_reasons = _score_fundamental_kap(fundamental_status, kap_status)

    sr_result: Optional[SupportResistanceResult] = None
    contextual_notes: list[str] = []
    daily_change_percent: Optional[float] = None
    target_3: Optional[float] = None
    entry_trigger: Optional[float] = None
    extra_risk_reasons: list[SignalReasonItem] = []

    if df is not None:
        # Yeni yol: gercek gecmis veriden hesaplanan destek/direnc + gelismis
        # giris/stop/hedef plani (bolum 2 ve 3 spesifikasyonu).
        sr_result = compute_support_resistance(
            df=df,
            current_price=snapshot.close,
            ema20=snapshot.ema20,
            ema50=snapshot.ema50,
            atr=snapshot.atr,
        )
        daily_change_percent = _compute_daily_change_percent(df)

        liquidity_score = 0.0
        liquidity_reasons: list[SignalReasonItem] = []
        max_score = 10.0
        if snapshot.relative_volume >= 1.0:
            liquidity_score += 4
            liquidity_reasons.append(SignalReasonItem("risk", "Likidite yeterli gorunuyor (goreceli hacim >= 1.0)"))
        else:
            liquidity_reasons.append(SignalReasonItem("risk", "Likidite dusuk olabilir", is_risk=True))

        trade_plan = _compute_trade_plan(snapshot, sr_result, risk_cfg, thresholds)
        stop_price = trade_plan["stop_price"]
        if stop_price is not None:
            liquidity_score += 6
            liquidity_reasons.append(SignalReasonItem("risk", f"Destek/ATR tabanli stop hesaplanabiliyor ({stop_price})"))
        else:
            liquidity_reasons.append(SignalReasonItem("risk", "Stop mesafesi cok dar/genis/anlamsiz, sinyal guvenilmez", is_risk=True))
        liquidity_score = min(liquidity_score, max_score)

        if not sr_result.support_reliable:
            extra_risk_reasons.append(SignalReasonItem("risk", sr_result.support_note or "Destek seviyeleri guvenilir degil", is_risk=True))
        if not sr_result.resistance_reliable:
            extra_risk_reasons.append(SignalReasonItem("risk", sr_result.resistance_note or "Direnc seviyeleri guvenilir degil", is_risk=True))

        entry_zone = trade_plan["entry_zone"]
        entry_trigger = trade_plan["entry_trigger"]
        target_1 = trade_plan["target_1"]
        target_2 = trade_plan["target_2"]
        target_3 = trade_plan["target_3"]
        risk_reward = trade_plan["risk_reward"]
        invalidation_note = trade_plan["invalidation_note"]
        contextual_notes = trade_plan["contextual_notes"]
        liquidity_reasons = liquidity_reasons + trade_plan["extra_reasons"] + extra_risk_reasons
    else:
        # Eski yol (geriye donuk uyumluluk): sadece ATR tabanli basit stop/hedef.
        liquidity_score, liquidity_reasons, stop_price = _score_liquidity_risk(
            snapshot, risk_cfg["atr_stop_multiplier"]
        )
        entry_zone = (round(snapshot.close * 0.995, 2), round(snapshot.close * 1.01, 2))
        target_1 = target_2 = risk_reward = None
        if stop_price is not None:
            risk_per_share = snapshot.close - stop_price
            if risk_per_share > 0:
                target_1 = round(snapshot.close + risk_per_share * 1.5, 2)
                target_2 = round(snapshot.close + risk_per_share * 2.5, 2)
                risk_reward = round((target_1 - snapshot.close) / risk_per_share, 2)
        invalidation_note = (
            f"Fiyat {stop_price} altina kapanirsa senaryo geçersiz olur."
            if stop_price
            else "Stop hesaplanamadigi icin senaryo net degil."
        )

    total_score = (
        trend_score + volume_score + momentum_score + regime_score + fundamental_score + liquidity_score
    )
    total_score = round(min(max(total_score, 0), 100), 1)

    # Guclu dusus rejiminde AL esigini yukselt (kural 8/piyasa rejimi maddesi).
    effective_thresholds = dict(thresholds)
    if regime_result.regime == REGIME_STRONG_DOWN:
        effective_thresholds["strong_buy_score"] = min(100, thresholds["strong_buy_score"] + 7)
        effective_thresholds["buy_score"] = min(100, thresholds["buy_score"] + 7)

    signal_type = classify_signal_type(total_score, effective_thresholds)

    all_reasons = trend_reasons + volume_reasons + momentum_reasons + regime_reasons + fundamental_reasons + liquidity_reasons
    risk_reasons = [r for r in all_reasons if r.is_risk]

    is_actionable_buy = (
        signal_type in ("STRONG_BUY_CANDIDATE", "BUY_CANDIDATE")
        and stop_price is not None
        and risk_reward is not None
        and risk_reward >= thresholds["minimum_risk_reward"]
        and snapshot.relative_volume >= thresholds["minimum_relative_volume"]
        and regime_result.regime not in (REGIME_STRONG_DOWN, "veri_yetersiz")
    )

    confidence = "yuksek" if len(risk_reasons) <= 2 and total_score >= 70 else (
        "orta" if total_score >= 50 else "dusuk"
    )

    idem_key = build_idempotency_key(snapshot.symbol, snapshot.timeframe, signal_type, snapshot.last_timestamp.to_pydatetime())

    return SignalResult(
        symbol=snapshot.symbol,
        timeframe=snapshot.timeframe,
        score=total_score,
        signal_type=signal_type,
        confidence=confidence,
        reasons=all_reasons,
        entry_zone=entry_zone,
        stop_price=stop_price,
        target_1=target_1,
        target_2=target_2,
        risk_reward=risk_reward,
        market_regime=regime_result.regime,
        data_timestamp=snapshot.last_timestamp.to_pydatetime(),
        provider=provider_name,
        strategy_version=strategy_version,
        idempotency_key=idem_key,
        is_actionable_buy=is_actionable_buy,
        invalidation_note=invalidation_note,
        extras={
            "trend_score": trend_score,
            "volume_score": volume_score,
            "momentum_score": momentum_score,
            "regime_score": regime_score,
            "fundamental_score": fundamental_score,
            "liquidity_score": liquidity_score,
        },
        target_3=target_3,
        entry_trigger=entry_trigger,
        daily_change_percent=daily_change_percent,
        support_resistance=sr_result,
        contextual_notes=contextual_notes,
    )
