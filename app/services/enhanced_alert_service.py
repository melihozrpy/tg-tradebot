from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional

import pandas as pd
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analysis.data_quality import DataQualityEngine, DataQualityResult, DataQualityStatus
from app.analysis.indicator_engine import atr as atr_series
from app.analysis.indicator_engine import bollinger_bands, ema, macd, relative_volume as relative_volume_series, rsi
from app.analysis.timeframe_levels_engine import (
    TIMEFRAME_DAILY,
    TIMEFRAME_MONTHLY,
    TIMEFRAME_WEEKLY,
    LevelDetail,
    MultiTimeframeLevelsResult,
)
from app.models.database import (
    EnhancedAlertEvent,
    EnhancedAlarmRule,
    EnhancedAlarmTriggerEvent,
    NewsImpactSnapshot,
    User,
)

logger = logging.getLogger("mergen_quant.enhanced_alerts")

SUPPORT_RESISTANCE_TYPES = {
    "gunluk_destek",
    "haftalik_destek",
    "aylik_destek",
    "ortak_destek",
    "destek_tepki",
    "destek_kirilimi",
    "gunluk_direnc",
    "haftalik_direnc",
    "aylik_direnc",
    "ortak_direnc",
    "direnc_reddi",
    "direnc_kirilimi",
    "direnc_destek_retest",
    "destek_direnc_retest",
    "sahte_kirilim_riski",
}

TRADE_PLAN_TYPES = {
    "alim_bolgesi",
    "tetik",
    "stop",
    "hedef_1",
    "hedef_2",
    "hedef_3",
    "risk_getiri",
    "ana_dip_senaryosu",
    "guclu_yukselis_senaryosu",
}

TECHNICAL_TYPES = {
    "hacim_patlamasi",
    "relative_volume",
    "volatilite_patlamasi",
    "atr_artisi",
    "rsi_asiri_alim",
    "rsi_asiri_satim",
    "rsi_pozitif_uyumsuzluk",
    "rsi_negatif_uyumsuzluk",
    "macd_yukari_kesisim",
    "macd_asagi_kesisim",
    "ema20_ema50_kesisimi",
    "golden_cross",
    "death_cross",
    "vwap_yukari",
    "vwap_asagi",
    "bollinger_ust",
    "bollinger_alt",
    "gap_yukari",
    "gap_asagi",
    "anormal_hareket",
}

MARKET_TYPES = {
    "haber_etkisi",
    "negatif_haber_etkisi",
    "xu100_guc",
    "xu100_guc_alt",
    "sektor_guc",
    "piyasa_rejimi",
    "sektor_lideri",
    "xu100_ayrisma",
}

VALID_ENHANCED_ALERT_TYPES = SUPPORT_RESISTANCE_TYPES | TRADE_PLAN_TYPES | TECHNICAL_TYPES | MARKET_TYPES

ALIASES = {
    "gunluk_destek_bolgesi": "gunluk_destek",
    "haftalik_destek_bolgesi": "haftalik_destek",
    "aylik_destek_bolgesi": "aylik_destek",
    "guclu_destek": "ortak_destek",
    "cakisan_destek": "ortak_destek",
    "guclu_direnc": "ortak_direnc",
    "cakisan_direnc": "ortak_direnc",
    "gunluk_direnc_bolgesi": "gunluk_direnc",
    "haftalik_direnc_bolgesi": "haftalik_direnc",
    "aylik_direnc_bolgesi": "aylik_direnc",
    "haftalik_direnc_kirilimi": "direnc_kirilimi",
    "aylik_direnc_kirilimi": "direnc_kirilimi",
    "gunluk_direnc_kirilimi": "direnc_kirilimi",
    "haftalik_destek_kirilimi": "destek_kirilimi",
    "aylik_destek_kirilimi": "destek_kirilimi",
    "gunluk_destek_kirilimi": "destek_kirilimi",
    "hacim": "hacim_patlamasi",
    "rvol": "relative_volume",
    "atr_patlamasi": "atr_artisi",
    "vwap_yukari_kirilim": "vwap_yukari",
    "vwap_asagi_kirilim": "vwap_asagi",
    "bollinger_ust_kirilim": "bollinger_ust",
    "bollinger_alt_kirilim": "bollinger_alt",
    "hedef1": "hedef_1",
    "hedef2": "hedef_2",
    "hedef3": "hedef_3",
}

ALERT_LABELS = {
    "gunluk_destek": "Günlük destek bölgesine giriş",
    "haftalik_destek": "Haftalık destek bölgesine giriş",
    "aylik_destek": "Aylık destek bölgesine giriş",
    "ortak_destek": "Güçlü çakışan destek bölgesine giriş",
    "destek_tepki": "Destekten tepki alımı",
    "destek_kirilimi": "Desteğin kapanışla kırılması",
    "gunluk_direnc": "Günlük direnç bölgesine giriş",
    "haftalik_direnc": "Haftalık direnç bölgesine giriş",
    "aylik_direnc": "Aylık direnç bölgesine giriş",
    "ortak_direnc": "Güçlü çakışan direnç bölgesine giriş",
    "direnc_reddi": "Dirençten reddedilme",
    "direnc_kirilimi": "Direncin kapanışla kırılması",
    "direnc_destek_retest": "Kırılan direncin desteğe dönüşmesi",
    "destek_direnc_retest": "Kırılan desteğin dirence dönüşmesi",
    "sahte_kirilim_riski": "Sahte kırılım riski",
    "alim_bolgesi": "Alım bölgesine giriş",
    "tetik": "Tetik seviyesinin aşılması",
    "stop": "Stop seviyesinin çalışması",
    "hedef_1": "Hedef 1 gerçekleşmesi",
    "hedef_2": "Hedef 2 gerçekleşmesi",
    "hedef_3": "Hedef 3 gerçekleşmesi",
    "risk_getiri": "Risk/getiri oranının uygun hâle gelmesi",
    "ana_dip_senaryosu": "Ana dip senaryosunun aktifleşmesi",
    "guclu_yukselis_senaryosu": "Güçlü yükseliş senaryosunun aktifleşmesi",
    "hacim_patlamasi": "Hacim patlaması",
    "relative_volume": "Relative Volume artışı",
    "volatilite_patlamasi": "Volatilite patlaması",
    "atr_artisi": "ATR olağan dışı artış",
    "rsi_asiri_alim": "RSI aşırı alım",
    "rsi_asiri_satim": "RSI aşırı satım",
    "rsi_pozitif_uyumsuzluk": "RSI pozitif uyumsuzluk",
    "rsi_negatif_uyumsuzluk": "RSI negatif uyumsuzluk",
    "macd_yukari_kesisim": "MACD yukarı kesişim",
    "macd_asagi_kesisim": "MACD aşağı kesişim",
    "ema20_ema50_kesisimi": "EMA20/EMA50 kesişimi",
    "golden_cross": "EMA50/EMA200 golden cross",
    "death_cross": "EMA50/EMA200 death cross",
    "vwap_yukari": "VWAP yukarı kırılım",
    "vwap_asagi": "VWAP aşağı kırılım",
    "bollinger_ust": "Bollinger üst bant kırılımı",
    "bollinger_alt": "Bollinger alt bant kırılımı",
    "gap_yukari": "Gap yukarı",
    "gap_asagi": "Gap aşağı",
    "anormal_hareket": "Anormal fiyat veya hacim hareketi",
    "haber_etkisi": "Haber etkisi eşiği aştı",
    "negatif_haber_etkisi": "Negatif haber etkisi eşiği aştı",
    "xu100_guc": "XU100 göreceli güç eşiği aştı",
    "xu100_guc_alt": "XU100 göreceli güç eşiğin altına indi",
    "sektor_guc": "Sektör göreceli güç eşiği aştı",
    "piyasa_rejimi": "Piyasa rejimi değişti",
    "sektor_lideri": "Hisse sektör lideri oldu",
    "xu100_ayrisma": "Hisse XU100'den belirgin ayrıştı",
}


class InvalidEnhancedAlertError(ValueError):
    pass


@dataclass
class AlarmEvaluationContext:
    symbol: str
    timeframe: str
    df: pd.DataFrame
    levels: Optional[MultiTimeframeLevelsResult] = None
    confluence_supports: list = field(default_factory=list)
    confluence_resistances: list = field(default_factory=list)
    signal: Optional[object] = None
    price_scenario: Optional[object] = None
    liquidity_score: Optional[float] = None
    news_score: Optional[float] = None
    xu100_strength: Optional[float] = None
    sector_strength: Optional[float] = None
    stock_strength: Optional[float] = None
    market_regime: Optional[str] = None
    previous_market_regime: Optional[str] = None
    active_scenario: Optional[str] = None
    data_quality: Optional[DataQualityResult] = None
    provider: str = "unknown"
    fallback_used: bool = False
    current_price: Optional[float] = None
    current_price_timestamp: Optional[datetime] = None


@dataclass
class AlarmTrigger:
    alert_type: str
    label: str
    current_price: float
    candle_key: str
    state_key: str
    timeframe: str
    level_low: Optional[float] = None
    level_high: Optional[float] = None
    close_confirmed: bool = True
    volume_ratio: Optional[float] = None
    level_confidence: Optional[float] = None
    strong_confirmation: bool = False
    main_risk: str = ""
    active_scenario: str = "-"
    message: str = ""


def normalize_enhanced_alert_type(raw_type: str, extra_args: Optional[list[str]] = None) -> tuple[str, Optional[str], Optional[int]]:
    raw = raw_type.strip().lower().replace("-", "_")
    canonical = ALIASES.get(raw, raw)
    timeframe: Optional[str] = None
    if raw.startswith("haftalik_"):
        timeframe = TIMEFRAME_WEEKLY
    elif raw.startswith("aylik_"):
        timeframe = TIMEFRAME_MONTHLY
    elif raw.startswith("gunluk_"):
        timeframe = TIMEFRAME_DAILY

    target_index = None
    if canonical == "hedef" or raw == "hedef":
        if not extra_args:
            raise InvalidEnhancedAlertError("Hedef alarmı için 1, 2 veya 3 belirtilmeli.")
        try:
            target_index = int(extra_args[0])
        except ValueError as exc:
            raise InvalidEnhancedAlertError("Hedef numarası 1, 2 veya 3 olmalı.") from exc
        if target_index not in {1, 2, 3}:
            raise InvalidEnhancedAlertError("Hedef numarası 1, 2 veya 3 olmalı.")
        canonical = f"hedef_{target_index}"
    elif canonical.startswith("hedef_"):
        target_index = int(canonical[-1])

    if canonical not in VALID_ENHANCED_ALERT_TYPES:
        raise InvalidEnhancedAlertError(f"Gelişmiş alarm türü desteklenmiyor: {raw_type}")
    return canonical, timeframe, target_index


def create_enhanced_alarm_rule(
    db: Session,
    user: User,
    symbol: str,
    raw_type: str,
    args: Optional[list[str]] = None,
    cooldown_minutes: int = 120,
) -> EnhancedAlarmRule:
    args = list(args or [])
    canonical, timeframe, target_index = normalize_enhanced_alert_type(raw_type, args)
    threshold_value = None
    threshold_text = None
    consumed_target = raw_type.strip().lower() == "hedef"
    remaining = args[1:] if consumed_target and args else args
    if canonical in {"haber_etkisi", "negatif_haber_etkisi", "xu100_guc", "xu100_guc_alt", "sektor_guc", "xu100_ayrisma", "relative_volume", "risk_getiri"}:
        if not remaining:
            defaults = {"haber_etkisi": 60.0, "negatif_haber_etkisi": -60.0, "xu100_guc": 75.0, "xu100_guc_alt": 35.0, "sektor_guc": 75.0, "xu100_ayrisma": 20.0, "relative_volume": 2.0, "risk_getiri": 2.0}
            threshold_value = defaults[canonical]
        else:
            try:
                threshold_value = float(remaining[0])
            except ValueError as exc:
                raise InvalidEnhancedAlertError("Alarm eşiği sayısal olmalı.") from exc
    elif canonical == "piyasa_rejimi" and remaining:
        threshold_text = remaining[0]
    if canonical == "negatif_haber_etkisi" and threshold_value is not None:
        threshold_value = -abs(threshold_value)

    rule = EnhancedAlarmRule(
        user_id=user.id,
        symbol=symbol.strip().upper().removesuffix(".IS"),
        alert_type=canonical,
        timeframe=timeframe,
        threshold_value=threshold_value,
        threshold_text=threshold_text,
        target_index=target_index,
        cooldown_minutes=max(1, int(cooldown_minutes)),
        is_active=True,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def list_enhanced_alarm_rules(db: Session, user: User, active_only: bool = False) -> list[EnhancedAlarmRule]:
    query = db.query(EnhancedAlarmRule).filter(EnhancedAlarmRule.user_id == user.id)
    if active_only:
        query = query.filter(EnhancedAlarmRule.is_active.is_(True))
    return query.order_by(EnhancedAlarmRule.id).all()


def get_enhanced_alarm_rule(db: Session, user: User, rule_id: int) -> EnhancedAlarmRule:
    rule = db.query(EnhancedAlarmRule).filter(EnhancedAlarmRule.id == rule_id, EnhancedAlarmRule.user_id == user.id).first()
    if rule is None:
        raise InvalidEnhancedAlertError(f"Alarm bulunamadı: E{rule_id}")
    return rule


def delete_enhanced_alarm_rule(db: Session, user: User, rule_id: int) -> None:
    rule = get_enhanced_alarm_rule(db, user, rule_id)
    db.query(EnhancedAlarmTriggerEvent).filter(EnhancedAlarmTriggerEvent.rule_id == rule.id).delete()
    db.delete(rule)
    db.commit()


def set_enhanced_alarm_active(db: Session, user: User, rule_id: int, active: bool) -> EnhancedAlarmRule:
    rule = get_enhanced_alarm_rule(db, user, rule_id)
    rule.is_active = active
    db.commit()
    db.refresh(rule)
    return rule


def _timeframe_result(levels: MultiTimeframeLevelsResult, timeframe: str):
    return {TIMEFRAME_DAILY: levels.daily, TIMEFRAME_WEEKLY: levels.weekly, TIMEFRAME_MONTHLY: levels.monthly}.get(timeframe, levels.daily)


def _primary_zone(rule: EnhancedAlarmRule, context: AlarmEvaluationContext):
    if context.levels is None:
        return None
    alert_type = rule.alert_type
    if alert_type in {"ortak_destek"}:
        return context.confluence_supports[0] if context.confluence_supports else None
    if alert_type in {"ortak_direnc"}:
        return context.confluence_resistances[0] if context.confluence_resistances else None
    timeframe = rule.timeframe or context.timeframe or TIMEFRAME_DAILY
    tf = _timeframe_result(context.levels, timeframe)
    if alert_type in {"gunluk_destek", "haftalik_destek", "aylik_destek", "destek_tepki", "destek_kirilimi", "direnc_destek_retest"}:
        return tf.support_1 or tf.main_support
    return tf.resistance_1 or tf.main_resistance


def _indicator_values(df: pd.DataFrame) -> dict:
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    previous_volume = float(volume.iloc[-21:-1].mean()) if len(df) >= 21 else float(volume.iloc[:-1].mean())
    volume_ratio = float(volume.iloc[-1] / previous_volume) if previous_volume > 0 else 0.0
    atr_values = atr_series(df, 14)
    atr_now = float(atr_values.iloc[-1]) if pd.notna(atr_values.iloc[-1]) else 0.0
    atr_base = float(atr_values.iloc[-21:-1].mean()) if len(df) >= 21 else atr_now
    rsi_values = rsi(close, 14)
    macd_line, signal_line, histogram = macd(close)
    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    ema200 = ema(close, 200) if len(df) >= 200 else pd.Series(index=df.index, dtype=float)
    typical = (df["high"] + df["low"] + df["close"]) / 3
    rolling = df["volume"].tail(min(40, len(df))).cumsum().replace(0, pd.NA)
    rolling_vwap = (typical.tail(len(rolling)) * df["volume"].tail(len(rolling))).cumsum() / rolling
    upper, middle, lower, _ = bollinger_bands(close, 20, 2.0)
    returns = close.pct_change()
    current_volatility = float(returns.tail(5).std()) if len(returns) >= 6 else 0.0
    base_volatility = float(returns.tail(30).std()) if len(returns) >= 30 else current_volatility
    return {
        "volume_ratio": volume_ratio,
        "atr_now": atr_now,
        "atr_base": atr_base,
        "rsi": rsi_values,
        "macd": macd_line,
        "macd_signal": signal_line,
        "macd_hist": histogram,
        "ema20": ema20,
        "ema50": ema50,
        "ema200": ema200,
        "vwap": rolling_vwap,
        "bollinger_upper": upper,
        "bollinger_lower": lower,
        "current_volatility": current_volatility,
        "base_volatility": base_volatility,
    }


def _crossed_above(previous: float, current: float, level: float) -> bool:
    return previous <= level < current


def _crossed_below(previous: float, current: float, level: float) -> bool:
    return previous >= level > current


def _divergence(df: pd.DataFrame, rsi_values: pd.Series, positive: bool) -> bool:
    if len(df) < 40:
        return False
    first_prices = df["close"].iloc[-40:-20]
    second_prices = df["close"].iloc[-20:]
    first_rsi = rsi_values.iloc[-40:-20]
    second_rsi = rsi_values.iloc[-20:]
    if positive:
        return float(second_prices.min()) < float(first_prices.min()) and float(second_rsi.min()) > float(first_rsi.min())
    return float(second_prices.max()) > float(first_prices.max()) and float(second_rsi.max()) < float(first_rsi.max())


def _evaluate_condition(rule: EnhancedAlarmRule, context: AlarmEvaluationContext, df: pd.DataFrame) -> Optional[AlarmTrigger]:
    if len(df) < 2:
        return None
    current = df.iloc[-1]
    previous = df.iloc[-2]
    current_price = float(context.current_price) if context.current_price is not None else float(current["close"])
    candle_time = pd.Timestamp(context.current_price_timestamp or current["timestamp"])
    candle_key = f"{context.timeframe}:{candle_time.isoformat()}"
    values = _indicator_values(df)
    volume_ratio = values["volume_ratio"]
    atr_now = values["atr_now"]
    liquidity_ok = context.liquidity_score is None or context.liquidity_score >= 45
    base_kwargs = {
        "alert_type": rule.alert_type,
        "label": ALERT_LABELS[rule.alert_type],
        "current_price": current_price,
        "candle_key": candle_key,
        "timeframe": context.timeframe,
        "volume_ratio": round(volume_ratio, 2),
        "active_scenario": context.active_scenario or "-",
    }

    if rule.alert_type in SUPPORT_RESISTANCE_TYPES:
        zone = _primary_zone(rule, context)
        if rule.alert_type == "sahte_kirilim_riski" and context.levels is not None:
            tf = _timeframe_result(context.levels, rule.timeframe or context.timeframe)
            candidates = [tf.resistance_1 or tf.main_resistance, tf.support_1 or tf.main_support]
            zone = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate is not None
                    and (
                        _crossed_above(float(previous["close"]), current_price, float(candidate.high))
                        or _crossed_below(float(previous["close"]), current_price, float(candidate.low))
                    )
                ),
                zone,
            )
        if zone is None:
            return None
        low, high, mid = float(zone.low), float(zone.high), float(zone.mid)
        touches_zone = float(current["low"]) <= high and float(current["high"]) >= low
        broke_up = _crossed_above(float(previous["close"]), current_price, high)
        broke_down = _crossed_below(float(previous["close"]), current_price, low)
        range_ok = float(current["high"] - current["low"]) >= max(atr_now * 0.45, current_price * 0.003)
        volume_ok = volume_ratio >= 1.2
        confirmation = volume_ok and range_ok and liquidity_ok
        body = abs(float(current["close"] - current["open"]))
        upper_wick = float(current["high"] - max(current["open"], current["close"]))
        lower_wick = float(min(current["open"], current["close"]) - current["low"])
        long_rejection = upper_wick > max(body * 1.5, atr_now * 0.25) or lower_wick > max(body * 1.5, atr_now * 0.25)

        triggered = False
        main_risk = ""
        if rule.alert_type in {"gunluk_destek", "haftalik_destek", "aylik_destek", "ortak_destek"}:
            triggered = touches_zone and current_price >= low
        elif rule.alert_type == "destek_tepki":
            triggered = float(previous["low"]) <= high and current_price > float(previous["close"]) and lower_wick > body
        elif rule.alert_type == "destek_kirilimi":
            triggered = broke_down
            if triggered and not confirmation:
                main_risk = "Hacim/ATR/likidite doğrulaması zayıf; sahte kırılım riski."
        elif rule.alert_type in {"gunluk_direnc", "haftalik_direnc", "aylik_direnc", "ortak_direnc"}:
            triggered = touches_zone and current_price <= high
        elif rule.alert_type == "direnc_reddi":
            triggered = float(current["high"]) >= low and current_price < mid and upper_wick > body
        elif rule.alert_type == "direnc_kirilimi":
            triggered = broke_up
            if triggered and not confirmation:
                main_risk = "Hacim/ATR/likidite doğrulaması zayıf; sahte kırılım riski."
        elif rule.alert_type == "sahte_kirilim_riski":
            triggered = (broke_up or broke_down) and (not confirmation or long_rejection)
            main_risk = "Kırılım kapanışı yeterli hacim/ATR/likidite teyidi taşımıyor."
        elif rule.alert_type == "direnc_destek_retest":
            triggered = bool(getattr(zone, "role_reversal", False)) and "simdi_destek" in " ".join(getattr(zone, "sources", [])) and touches_zone
        elif rule.alert_type == "destek_direnc_retest":
            triggered = bool(getattr(zone, "role_reversal", False)) and "simdi_direnc" in " ".join(getattr(zone, "sources", [])) and touches_zone
        if not triggered:
            return None
        direction = "up" if broke_up else ("down" if broke_down else "touch")
        return AlarmTrigger(
            **base_kwargs,
            state_key=f"{rule.alert_type}:{direction}:{low:.4f}:{high:.4f}",
            level_low=low,
            level_high=high,
            level_confidence=float(getattr(zone, "confidence", 0.0)),
            strong_confirmation=confirmation if (broke_up or broke_down) else True,
            main_risk=main_risk,
        )

    if rule.alert_type in TRADE_PLAN_TYPES:
        signal = context.signal
        if signal is None and rule.alert_type not in {"ana_dip_senaryosu", "guclu_yukselis_senaryosu"}:
            return None
        level = None
        triggered = False
        if rule.alert_type == "alim_bolgesi":
            zone = getattr(signal, "entry_zone", (None, None))
            if zone and zone[0] is not None and zone[1] is not None:
                triggered = float(current["low"]) <= zone[1] and float(current["high"]) >= zone[0]
                level = (float(zone[0]), float(zone[1]))
        elif rule.alert_type == "tetik":
            value = getattr(signal, "entry_trigger", None)
            triggered = value is not None and _crossed_above(float(previous["close"]), current_price, float(value))
            level = (float(value), float(value)) if value is not None else None
        elif rule.alert_type == "stop":
            value = getattr(signal, "stop_price", None)
            triggered = value is not None and float(current["low"]) <= float(value)
            level = (float(value), float(value)) if value is not None else None
        elif rule.alert_type.startswith("hedef_"):
            target_index = int(rule.alert_type[-1])
            value = getattr(signal, f"target_{target_index}", None)
            triggered = value is not None and float(current["high"]) >= float(value)
            level = (float(value), float(value)) if value is not None else None
        elif rule.alert_type == "risk_getiri":
            value = getattr(signal, "risk_reward", None)
            threshold = rule.threshold_value or 2.0
            triggered = value is not None and float(value) >= threshold
        elif rule.alert_type == "ana_dip_senaryosu":
            scenario = context.price_scenario
            zone = (
                getattr(scenario, "main_dip_zone", None)
                or getattr(scenario, "decline_main", None)
                if scenario else None
            )
            triggered = zone is not None and float(current["low"]) <= zone.high and current_price >= zone.low
            level = (zone.low, zone.high) if zone else None
        elif rule.alert_type == "guclu_yukselis_senaryosu":
            scenario = context.price_scenario
            zone = (
                getattr(scenario, "strong_breakout_zone", None)
                or getattr(scenario, "rise_breakout", None)
                if scenario else None
            )
            triggered = zone is not None and current_price >= zone.low
            level = (zone.low, zone.high) if zone else None
        if not triggered:
            return None
        low, high = level if level else (None, None)
        return AlarmTrigger(
            **base_kwargs,
            state_key=f"{rule.alert_type}:{low}:{high}",
            level_low=low,
            level_high=high,
            strong_confirmation=True,
        )

    if rule.alert_type in TECHNICAL_TYPES:
        triggered = False
        state_suffix = "active"
        close_prev = float(previous["close"])
        if rule.alert_type == "hacim_patlamasi":
            triggered = volume_ratio >= (rule.threshold_value or 2.5)
        elif rule.alert_type == "relative_volume":
            triggered = volume_ratio >= (rule.threshold_value or 2.0)
        elif rule.alert_type == "volatilite_patlamasi":
            triggered = values["base_volatility"] > 0 and values["current_volatility"] >= values["base_volatility"] * 1.8
        elif rule.alert_type == "atr_artisi":
            triggered = values["atr_base"] > 0 and atr_now >= values["atr_base"] * 1.6
        elif rule.alert_type == "rsi_asiri_alim":
            triggered = float(values["rsi"].iloc[-1]) >= 70
        elif rule.alert_type == "rsi_asiri_satim":
            triggered = float(values["rsi"].iloc[-1]) <= 30
        elif rule.alert_type == "rsi_pozitif_uyumsuzluk":
            triggered = _divergence(df, values["rsi"], True)
        elif rule.alert_type == "rsi_negatif_uyumsuzluk":
            triggered = _divergence(df, values["rsi"], False)
        elif rule.alert_type == "macd_yukari_kesisim":
            triggered = values["macd"].iloc[-2] <= values["macd_signal"].iloc[-2] and values["macd"].iloc[-1] > values["macd_signal"].iloc[-1]
        elif rule.alert_type == "macd_asagi_kesisim":
            triggered = values["macd"].iloc[-2] >= values["macd_signal"].iloc[-2] and values["macd"].iloc[-1] < values["macd_signal"].iloc[-1]
        elif rule.alert_type == "ema20_ema50_kesisimi":
            up_cross = values["ema20"].iloc[-2] <= values["ema50"].iloc[-2] and values["ema20"].iloc[-1] > values["ema50"].iloc[-1]
            down_cross = values["ema20"].iloc[-2] >= values["ema50"].iloc[-2] and values["ema20"].iloc[-1] < values["ema50"].iloc[-1]
            triggered = up_cross or down_cross
            state_suffix = "yukari" if up_cross else "asagi"
        elif rule.alert_type == "golden_cross" and len(df) >= 201:
            triggered = values["ema50"].iloc[-2] <= values["ema200"].iloc[-2] and values["ema50"].iloc[-1] > values["ema200"].iloc[-1]
        elif rule.alert_type == "death_cross" and len(df) >= 201:
            triggered = values["ema50"].iloc[-2] >= values["ema200"].iloc[-2] and values["ema50"].iloc[-1] < values["ema200"].iloc[-1]
        elif rule.alert_type == "vwap_yukari":
            triggered = _crossed_above(close_prev, current_price, float(values["vwap"].iloc[-1]))
        elif rule.alert_type == "vwap_asagi":
            triggered = _crossed_below(close_prev, current_price, float(values["vwap"].iloc[-1]))
        elif rule.alert_type == "bollinger_ust":
            triggered = _crossed_above(close_prev, current_price, float(values["bollinger_upper"].iloc[-1]))
        elif rule.alert_type == "bollinger_alt":
            triggered = _crossed_below(close_prev, current_price, float(values["bollinger_lower"].iloc[-1]))
        elif rule.alert_type == "gap_yukari":
            triggered = float(current["open"]) > float(previous["high"]) * 1.005
        elif rule.alert_type == "gap_asagi":
            triggered = float(current["open"]) < float(previous["low"]) * 0.995
        elif rule.alert_type == "anormal_hareket":
            return_std = float(df["close"].pct_change().tail(30).std())
            triggered = volume_ratio >= 2.5 or (return_std > 0 and abs(current_price / close_prev - 1) >= return_std * 3)
        if not triggered:
            return None
        return AlarmTrigger(
            **base_kwargs,
            state_key=f"{rule.alert_type}:{state_suffix}",
            strong_confirmation=volume_ratio >= 1.2,
            main_risk="Gösterge tek başına işlem kararı değildir.",
        )

    threshold = rule.threshold_value
    triggered = False
    state_suffix = "active"
    if rule.alert_type == "haber_etkisi":
        triggered = context.news_score is not None and context.news_score >= (threshold or 60)
    elif rule.alert_type == "negatif_haber_etkisi":
        limit = threshold if threshold is not None else -60
        triggered = context.news_score is not None and context.news_score <= limit
    elif rule.alert_type == "xu100_guc":
        triggered = context.xu100_strength is not None and context.xu100_strength >= (threshold or 75)
    elif rule.alert_type == "xu100_guc_alt":
        triggered = context.xu100_strength is not None and context.xu100_strength <= (threshold or 35)
    elif rule.alert_type == "sektor_guc":
        triggered = context.sector_strength is not None and context.sector_strength >= (threshold or 75)
    elif rule.alert_type == "piyasa_rejimi":
        triggered = context.market_regime is not None and context.market_regime != context.previous_market_regime
        if rule.threshold_text:
            triggered = triggered and context.market_regime == rule.threshold_text
        state_suffix = context.market_regime or "unknown"
    elif rule.alert_type == "sektor_lideri":
        triggered = context.sector_strength is not None and context.sector_strength >= 75 and (context.xu100_strength is None or context.sector_strength > context.xu100_strength)
    elif rule.alert_type == "xu100_ayrisma":
        difference = None
        if context.stock_strength is not None and context.xu100_strength is not None:
            difference = context.stock_strength - context.xu100_strength
        elif context.xu100_strength is not None:
            # Göreceli güç skoru zaten hisse-XU100 ayrışmasını 50 nötr merkeziyle taşır.
            difference = context.xu100_strength - 50.0
        triggered = difference is not None and abs(difference) >= (threshold or 20)
        state_suffix = "pozitif" if triggered and difference > 0 else "negatif"
    if not triggered:
        return None
    return AlarmTrigger(
        **base_kwargs,
        state_key=f"{rule.alert_type}:{state_suffix}",
        strong_confirmation=True,
        main_risk="Piyasa/haber verisi teknik analizle birlikte değerlendirilmelidir.",
    )


def format_alarm_message(trigger: AlarmTrigger, context: AlarmEvaluationContext) -> str:
    quality = context.data_quality
    provider = quality.provider if quality is not None else context.provider
    fallback = bool(quality.fallback_used or quality.cache_used) if quality is not None else context.fallback_used
    level = "-"
    if trigger.level_low is not None:
        level = f"{trigger.level_low:.2f}"
        if trigger.level_high is not None and abs(trigger.level_high - trigger.level_low) >= 0.005:
            level = f"{trigger.level_low:.2f}-{trigger.level_high:.2f}"
    data_time = trigger.candle_key.split(":", 1)[-1]
    return (
        "🏹 MERGEN QUANT — ALARM\n\n"
        f"Sembol: {context.symbol.upper()}\n"
        f"Alarm türü: {trigger.label}\n"
        f"Güncel fiyat: {trigger.current_price:.2f}\n"
        f"Tetiklenen seviye veya bölge: {level}\n"
        f"Zaman dilimi: {trigger.timeframe}\n"
        f"Kapanış doğrulaması: {'Güçlü' if trigger.strong_confirmation else 'Düşük güvenli'}\n"
        f"Hacim oranı: {trigger.volume_ratio:.2f}x\n"
        f"Seviye güven puanı: {trigger.level_confidence if trigger.level_confidence is not None else '-'}\n"
        f"XU100 göreceli gücü: {context.xu100_strength if context.xu100_strength is not None else '-'}\n"
        f"Sektör göreceli gücü: {context.sector_strength if context.sector_strength is not None else '-'}\n"
        f"Piyasa rejimi: {context.market_regime or '-'}\n"
        f"Aktif senaryo: {trigger.active_scenario}\n"
        f"Ana risk: {trigger.main_risk or '-'}\n"
        f"Veri zamanı: {data_time}\n"
        f"Veri sağlayıcısı: {provider}{' (fallback/cache)' if fallback else ''}"
    )


def evaluate_enhanced_alarm(
    db: Session,
    rule: EnhancedAlarmRule,
    context: AlarmEvaluationContext,
    *,
    now: Optional[datetime] = None,
) -> Optional[AlarmTrigger]:
    if not rule.is_active:
        return None
    if context.data_quality is not None and not context.data_quality.usable_for_analysis:
        return None
    complete_df = DataQualityEngine().completed_candles(context.df, context.timeframe, now=now)
    if complete_df is None or len(complete_df) < 2:
        return None
    candle_key = f"{context.timeframe}:{pd.Timestamp(complete_df.iloc[-1]['timestamp']).isoformat()}"
    if (
        rule.alert_type == "piyasa_rejimi"
        and context.market_regime is not None
        and context.previous_market_regime is None
    ):
        # İlk gözlem değişim değildir; kalıcı başlangıç durumu olarak saklanır.
        rule.last_evaluated_candle_key = candle_key
        rule.last_state_key = f"piyasa_rejimi:{context.market_regime}"
        db.commit()
        return None
    trigger = _evaluate_condition(rule, context, complete_df)
    if trigger is None:
        rule.last_evaluated_candle_key = candle_key
        rule.last_state_key = (
            f"piyasa_rejimi:{context.market_regime}"
            if rule.alert_type == "piyasa_rejimi" and context.market_regime is not None
            else None
        )
        db.commit()
        return None

    if rule.last_evaluated_candle_key == trigger.candle_key or rule.last_state_key == trigger.state_key:
        return None
    exists = db.query(EnhancedAlarmTriggerEvent).filter(
        EnhancedAlarmTriggerEvent.rule_id == rule.id,
        EnhancedAlarmTriggerEvent.candle_key == trigger.candle_key,
        EnhancedAlarmTriggerEvent.state_key == trigger.state_key,
    ).first()
    if exists is not None:
        rule.last_evaluated_candle_key = trigger.candle_key
        rule.last_state_key = trigger.state_key
        db.commit()
        return None

    now = now or datetime.now(timezone.utc)
    last = rule.last_triggered_at
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() / 60 < rule.cooldown_minutes:
            rule.last_evaluated_candle_key = trigger.candle_key
            db.commit()
            return None

    trigger.message = format_alarm_message(trigger, context)
    quality_score = context.data_quality.score if context.data_quality else None
    event = EnhancedAlarmTriggerEvent(
        rule_id=rule.id,
        user_id=rule.user_id,
        symbol=rule.symbol,
        alert_type=rule.alert_type,
        candle_key=trigger.candle_key,
        state_key=trigger.state_key,
        current_price=trigger.current_price,
        triggered_level_low=trigger.level_low,
        triggered_level_high=trigger.level_high,
        timeframe=trigger.timeframe,
        close_confirmed=trigger.close_confirmed,
        volume_ratio=trigger.volume_ratio,
        level_confidence=trigger.level_confidence,
        provider=context.provider,
        fallback_used=context.fallback_used,
        data_quality_score=quality_score,
        message=trigger.message,
        triggered_at=now,
    )
    db.add(event)
    # 0004 tablosunu da audit görünürlüğü için besle; eski kayıtlar korunur.
    db.add(
        EnhancedAlertEvent(
            user_id=rule.user_id,
            symbol=rule.symbol,
            alert_type=rule.alert_type,
            current_price=trigger.current_price,
            triggered_level=trigger.level_low,
            level_timeframe=trigger.timeframe,
            level_confidence=trigger.level_confidence,
            volume_ratio=trigger.volume_ratio,
            market_regime=context.market_regime,
            related_scenario=trigger.active_scenario,
            main_risk=trigger.main_risk,
            candle_key=trigger.candle_key,
            data_time=pd.Timestamp(complete_df.iloc[-1]["timestamp"]).to_pydatetime(),
        )
    )
    rule.last_evaluated_candle_key = trigger.candle_key
    rule.last_state_key = trigger.state_key
    rule.last_triggered_at = now
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return trigger


async def scan_enhanced_alarms(application, db: Session, provider, settings) -> int:
    """Aktif kuralları sembol bazında izole tarar; tek hata diğerlerini durdurmaz."""
    from app.analysis.confluence_zone_engine import find_confluence_zones
    from app.analysis.indicator_engine import compute_technical_snapshot
    from app.analysis.liquidity_engine import compute_liquidity
    from app.analysis.market_regime_engine import classify_market_regime
    from app.analysis.price_scenario_engine import compute_price_scenarios
    from app.analysis.relative_strength_engine import compute_relative_strength
    from app.analysis.timeframe_levels_engine import _resample, compute_timeframe_levels
    from app.services.data_quality_service import assess_and_persist_quality
    from app.services.health_service import mark_runtime_health
    from app.services.current_price_service import resolve_current_price

    rules = db.query(EnhancedAlarmRule).filter(EnhancedAlarmRule.is_active.is_(True)).all()
    by_symbol: dict[str, list[EnhancedAlarmRule]] = {}
    for rule in rules:
        by_symbol.setdefault(rule.symbol, []).append(rule)
    sent = 0
    for symbol, symbol_rules in by_symbol.items():
        try:
            end = datetime.now(timezone.utc)
            daily_df = provider.get_ohlcv(symbol, "1d", end - timedelta(days=500), end)
            quality = assess_and_persist_quality(db, daily_df, provider=provider, symbol=symbol, timeframe="1d", min_bars=60)
            if not quality.usable_for_analysis:
                continue
            daily_complete = DataQualityEngine().completed_candles(daily_df, "1d")
            price_context = resolve_current_price(
                provider, symbol, daily_df=daily_df,
                timezone_name=getattr(settings, "timezone_name", "Europe/Istanbul"),
                allow_provider_calls=False,
            )
            current_price = price_context.current_price or float(daily_complete.iloc[-1]["close"])
            levels = compute_timeframe_levels(daily_complete, current_price)
            supports, resistances = find_confluence_zones(levels, current_price)
            snapshot = compute_technical_snapshot(daily_complete, symbol, "1d")
            liquidity = compute_liquidity(daily_complete)
            rule_types = {rule.alert_type for rule in symbol_rules}
            signal = SimpleNamespace(entry_zone=(None, None), entry_trigger=None, stop_price=None, target_1=None, target_2=None, target_3=None, risk_reward=None)
            # Açık/son sinyal varsa deterministik işlem planı seviyelerini kullan.
            from app.models.database import Signal
            last_signal = db.query(Signal).filter(Signal.symbol == symbol).order_by(Signal.created_at.desc()).first()
            if last_signal is not None:
                signal = SimpleNamespace(
                    entry_zone=(last_signal.entry_zone_low, last_signal.entry_zone_high),
                    entry_trigger=last_signal.entry_trigger,
                    stop_price=last_signal.stop_price,
                    target_1=last_signal.target_1,
                    target_2=last_signal.target_2,
                    target_3=last_signal.target_3,
                    risk_reward=last_signal.risk_reward,
                )

            price_scenario = None
            active_scenario = None
            if rule_types & {"ana_dip_senaryosu", "guclu_yukselis_senaryosu"}:
                price_scenario = compute_price_scenarios(
                    levels,
                    supports,
                    resistances,
                    current_price,
                    liquidity_score=liquidity.score if liquidity.available else None,
                )
                main_dip = getattr(price_scenario, "decline_main", None)
                breakout = getattr(price_scenario, "rise_breakout", None)
                if main_dip is not None and main_dip.low <= current_price <= main_dip.high:
                    active_scenario = "Ana dip senaryosu"
                elif breakout is not None and current_price >= breakout.low:
                    active_scenario = "Güçlü yükseliş senaryosu"

            news_score = None
            if rule_types & {"haber_etkisi", "negatif_haber_etkisi"}:
                news_row = (
                    db.query(NewsImpactSnapshot)
                    .filter(NewsImpactSnapshot.symbol == symbol, NewsImpactSnapshot.window_label == "24h")
                    .order_by(NewsImpactSnapshot.created_at.desc())
                    .first()
                )
                news_score = news_row.impact_score if news_row is not None else None

            xu100_strength = None
            sector_strength = None
            market_regime = None
            market_rule_types = {
                "xu100_guc", "xu100_guc_alt", "sektor_guc", "piyasa_rejimi",
                "sektor_lideri", "xu100_ayrisma",
            }
            if rule_types & market_rule_types:
                index_symbol = getattr(settings, "xu100_symbol", "XU100.IS")
                try:
                    index_df = provider.get_ohlcv(index_symbol, "1d", end - timedelta(days=500), end)
                    index_df = DataQualityEngine().completed_candles(index_df, "1d")
                    xu_result = compute_relative_strength(daily_complete, index_df)
                    if xu_result.available:
                        xu100_strength = xu_result.relative_score
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Alarm XU100 bağlamı hesaplanamadı symbol=%s: %s", symbol, exc)

                if "piyasa_rejimi" in rule_types:
                    market_regime = classify_market_regime(
                        provider, index_symbol=index_symbol, timeframe="1d"
                    ).regime

                if rule_types & {"sektor_guc", "sektor_lideri"}:
                    from app.services.sector_service import get_sector_info

                    sector_info = get_sector_info(symbol)
                    if sector_info is not None and sector_info.sector_index:
                        try:
                            sector_df = provider.get_ohlcv(
                                sector_info.sector_index, "1d", end - timedelta(days=500), end
                            )
                            sector_df = DataQualityEngine().completed_candles(sector_df, "1d")
                            sector_result = compute_relative_strength(daily_complete, sector_df)
                            if sector_result.available:
                                sector_strength = sector_result.relative_score
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Alarm sektör bağlamı hesaplanamadı symbol=%s: %s", symbol, exc)

            base_context = AlarmEvaluationContext(
                symbol=symbol,
                timeframe=TIMEFRAME_DAILY,
                df=daily_complete,
                levels=levels,
                confluence_supports=supports,
                confluence_resistances=resistances,
                signal=signal,
                price_scenario=price_scenario,
                liquidity_score=liquidity.score if liquidity.available else None,
                news_score=news_score,
                xu100_strength=xu100_strength,
                sector_strength=sector_strength,
                market_regime=market_regime,
                active_scenario=active_scenario,
                data_quality=quality,
                provider=quality.provider,
                fallback_used=quality.fallback_used or quality.cache_used,
                current_price=current_price,
                current_price_timestamp=price_context.current_price_timestamp,
            )
            for rule in symbol_rules:
                previous_regime = None
                if rule.alert_type == "piyasa_rejimi" and rule.last_state_key:
                    prefix = "piyasa_rejimi:"
                    if rule.last_state_key.startswith(prefix):
                        previous_regime = rule.last_state_key[len(prefix):]
                context = replace(base_context, previous_market_regime=previous_regime)
                if rule.timeframe == TIMEFRAME_WEEKLY:
                    context = replace(base_context, timeframe=TIMEFRAME_WEEKLY, df=_resample(daily_complete, "W-FRI"))
                elif rule.timeframe == TIMEFRAME_MONTHLY:
                    context = replace(base_context, timeframe=TIMEFRAME_MONTHLY, df=_resample(daily_complete, "ME"))
                try:
                    trigger = evaluate_enhanced_alarm(db, rule, context)
                    if trigger is None or application is None:
                        continue
                    user = db.query(User).filter(User.id == rule.user_id).first()
                    if user is not None:
                        await application.bot.send_message(chat_id=user.telegram_user_id, text=trigger.message)
                        sent += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Alarm kuralı değerlendirilemedi rule=%s symbol=%s: %s", rule.id, symbol, exc)
                    db.rollback()
        except Exception as exc:  # noqa: BLE001 - bir sembol diğerlerini engellemez
            logger.warning("Gelişmiş alarm taraması sembol hatası symbol=%s: %s", symbol, exc)
            db.rollback()
            continue
    mark_runtime_health("alarm_scan", "ok")
    return sent
