from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from app.analysis.data_quality import DataQualityEngine, DataQualityResult
from app.analysis.decision_engine import DecisionResult, decide
from app.analysis.indicator_engine import InsufficientDataError, compute_technical_snapshot
from app.analysis.liquidity_engine import LiquidityResult, compute_liquidity
from app.analysis.multi_timeframe_engine import STAGE5E_TIMEFRAMES, MultiTimeframeResult, analyze_multi_timeframe
from app.analysis.signal_engine import compute_trade_plan
from app.analysis.support_resistance_engine import SupportResistanceResult, compute_support_resistance
from app.config.settings import get_strategy_config
from app.data.base_provider import BaseMarketDataProvider, DataUnavailableError

INTRADAY_PREVIEW_STATE = "PREVIEW"

logger = logging.getLogger("mergen_quant.intraday")

CLASS_ALIM_ADAYI = "Gün içi alım adayı"
CLASS_TETIK_BEKLENIYOR = "Tetik bekleniyor"
CLASS_RISK_ARTTI = "Gün içi risk arttı"
CLASS_NOTR = "Nötr"
CLASS_VERI_YETERSIZ = "Veri yetersiz"

_TREND_LABELS_TR = {"up": "Yükseliş", "down": "Düşüş", "sideways": "Yatay"}

# Gun ici on analiz siniflarini (Turkce, kisa) DecisionEngine'in bekledigi
# temel sinyal koduna esler; bu sayede /gunici de /analiz ile AYNI nihai
# karar mantigindan (likidite + coklu zaman dilimi kapisi) gecer.
_CLASSIFICATION_TO_BASE_SIGNAL = {
    CLASS_ALIM_ADAYI: "BUY_CANDIDATE",
    CLASS_TETIK_BEKLENIYOR: "WATCH",
    CLASS_RISK_ARTTI: "WEAK_RISK",
    CLASS_NOTR: "NEUTRAL",
    CLASS_VERI_YETERSIZ: "NEUTRAL",
}


@dataclass
class _PreviewPseudoSignal:
    """DecisionEngine.decide() sadece bu 4 alani okur (duck-typing); gun ici
    on analiz icin tam bir SignalResult uretmek yerine kucuk bir kopru nesnesi
    kullanilir. Skor ve guven asla uydurulmaz; mevcut on analiz verisinden turetilir."""

    signal_type: str
    confidence: str
    is_actionable_buy: bool
    score: float


class IntradayAnalysisUnavailableError(Exception):
    """Gun ici on analiz uretilemedigi durumlarda firlatilir; asla mock veriye gecilmez."""


@dataclass
class IntradayPreviewResult:
    symbol: str
    state: str
    classification: str
    last_price: Optional[float]
    today_open: Optional[float]
    today_high: Optional[float]
    today_low: Optional[float]
    previous_close: Optional[float]
    daily_change_percent: Optional[float]
    today_volume: Optional[float]
    relative_volume: Optional[float]
    last_update: Optional[datetime]
    is_delayed: bool
    intraday_trend: str
    daily_main_trend: str
    nearest_support: Optional[float]
    nearest_resistance: Optional[float]
    distance_to_support_percent: Optional[float]
    distance_to_resistance_percent: Optional[float]
    vwap: Optional[float]
    rsi: Optional[float]
    macd_histogram: Optional[float]
    is_anomalous: bool
    warnings: list[str] = field(default_factory=list)
    # V3.2: /analiz ile ayni motorlardan (DecisionEngine, LiquidityEngine,
    # MultiTimeframeEngine, gelismis destek/direnc) uretilen ek alanlar.
    support_resistance: Optional[SupportResistanceResult] = None
    entry_zone: tuple = (None, None)
    entry_trigger: Optional[float] = None
    stop_price: Optional[float] = None
    target_1: Optional[float] = None
    target_2: Optional[float] = None
    target_3: Optional[float] = None
    risk_reward: Optional[float] = None
    invalidation_note: str = ""
    contextual_notes: list[str] = field(default_factory=list)
    liquidity: Optional[LiquidityResult] = None
    multi_timeframe: Optional[MultiTimeframeResult] = None
    decision: Optional[DecisionResult] = None
    data_quality: Optional[DataQualityResult] = None


def _date_of(ts) -> object:
    if getattr(ts, "tzinfo", None) is not None:
        return ts.tz_convert("UTC").date()
    return ts.date()


def _compute_vwap(df: pd.DataFrame) -> Optional[float]:
    if df is None or df.empty or df["volume"].sum() <= 0:
        return None
    typical = (df["high"] + df["low"] + df["close"]) / 3
    return float((typical * df["volume"]).sum() / df["volume"].sum())


def run_intraday_preview(provider: BaseMarketDataProvider, symbol: str) -> IntradayPreviewResult:
    """Bolum 3 spesifikasyonuna gore /gunici komutu icin GUN ICI ON ANALIZ uretir.

    ONEMLI: Bu analiz KESINLESMIS kapanis sinyali DEGILDIR ve asla veritabanina
    kesinlesmis bir sinyal olarak kaydedilmez (state='PREVIEW'). Veri
    alinamazsa/kalitesizse mock veriye KESINLIKLE gecilmez;
    IntradayAnalysisUnavailableError firlatilir.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=5)

    try:
        intraday_df = provider.get_ohlcv(symbol, "15m", start, end)
    except DataUnavailableError as exc:
        raise IntradayAnalysisUnavailableError(f"Gün içi veri alınamadı. (Detay: {exc})") from exc

    quality_engine = DataQualityEngine()
    original_count = len(intraday_df)
    intraday_df = quality_engine.completed_candles(intraday_df, "15m", now=end)
    metadata = {}
    if hasattr(provider, "metadata_for"):
        metadata = provider.metadata_for(symbol, "15m") or {}
    quality = quality_engine.evaluate(
        intraday_df,
        symbol=symbol,
        timeframe="15m",
        min_bars=20,
        max_staleness_minutes=120,
        now=end,
        provider=metadata.get("provider", getattr(provider, "name", "unknown")),
        fallback_used=bool(metadata.get("fallback_used")),
        cache_used=bool(metadata.get("cache_used")),
        cache_age_minutes=metadata.get("cache_age_minutes"),
    )
    if not quality.usable_for_analysis:
        raise IntradayAnalysisUnavailableError(
            f"⚠️ Veri kalitesi düşük olduğu için gün içi analiz oluşturulmadı. "
            f"Durum: {quality.status.value}. Sorunlar: " + "; ".join(quality.issues)
        )

    intraday_df = intraday_df.sort_values("timestamp").reset_index(drop=True)
    last_row = intraday_df.iloc[-1]
    last_ts = last_row["timestamp"]
    last_ts_py = last_ts.to_pydatetime() if hasattr(last_ts, "to_pydatetime") else last_ts
    last_date = _date_of(last_ts)

    today_mask = intraday_df["timestamp"].apply(lambda ts: _date_of(ts) == last_date)
    today_rows = intraday_df.loc[today_mask]

    previous_close: Optional[float] = None
    try:
        daily_df = provider.get_ohlcv(symbol, "1d", end - timedelta(days=20), end)
        daily_df = daily_df.sort_values("timestamp").reset_index(drop=True)
        daily_before_today = daily_df[daily_df["timestamp"].apply(lambda ts: _date_of(ts)) < last_date]
        if not daily_before_today.empty:
            previous_close = round(float(daily_before_today.iloc[-1]["close"]), 2)
    except DataUnavailableError:
        previous_close = None

    last_price = round(float(last_row["close"]), 2)
    daily_change_percent = (
        round(((last_price - previous_close) / previous_close) * 100, 2)
        if previous_close and previous_close > 0
        else None
    )

    is_delayed = True
    try:
        freshness = provider.get_data_freshness(symbol, "15m")
        is_delayed = not freshness.is_fresh
    except Exception:  # noqa: BLE001 - tazelik kontrolu asla analiz akisini cokertmemeli
        is_delayed = True

    warnings = ["Gün içi fiyat gecikmeli olabilir; destek/direnç/stop/hedefler kapanışa kadar değişebilir."]
    warnings.extend(quality.warnings)
    if len(intraday_df) < original_count:
        warnings.append("Tamamlanmamış son intraday mum analiz dışında bırakıldı.")
    if quality.fallback_used:
        warnings.append(f"Fallback veri kaynağı kullanıldı: {quality.provider}.")
    if quality.cache_used:
        warnings.append("Canlı veri yerine izin verilen yaş içindeki yerel cache kullanıldı.")

    try:
        snapshot = compute_technical_snapshot(intraday_df, symbol, "15m")
    except InsufficientDataError:
        snapshot = None

    sr_result: Optional[SupportResistanceResult] = None
    if snapshot is not None:
        sr_result = compute_support_resistance(
            intraday_df,
            snapshot.close,
            snapshot.ema20,
            snapshot.ema50,
            snapshot.atr,
            ema100=snapshot.ema100,
            ema200=snapshot.ema200,
        )

    daily_main_trend = "Veri yetersiz"
    try:
        daily_df_for_trend = provider.get_ohlcv(symbol, "1d", end - timedelta(days=400), end)
        daily_snapshot = compute_technical_snapshot(daily_df_for_trend, symbol, "1d")
        daily_main_trend = _TREND_LABELS_TR.get(daily_snapshot.trend_direction, "Belirsiz")
    except (DataUnavailableError, InsufficientDataError):
        pass

    intraday_trend = "Belirsiz"
    if snapshot is not None:
        intraday_trend = _TREND_LABELS_TR.get(snapshot.trend_direction, "Belirsiz")

    vwap = _compute_vwap(today_rows if not today_rows.empty else intraday_df.tail(20))

    nearest_support = sr_result.support_1 if sr_result else None
    nearest_resistance = sr_result.resistance_1 if sr_result else None
    dist_support = round(((last_price - nearest_support) / last_price) * 100, 2) if nearest_support else None
    dist_resistance = (
        round(((nearest_resistance - last_price) / last_price) * 100, 2) if nearest_resistance else None
    )

    relative_volume = snapshot.relative_volume if snapshot else None
    is_anomalous = bool(relative_volume and relative_volume >= 2.5)

    if snapshot is None:
        classification = CLASS_VERI_YETERSIZ
    elif is_anomalous or (sr_result is not None and sr_result.support_broken_with_volume):
        classification = CLASS_RISK_ARTTI
    elif (
        intraday_trend == "Yükseliş"
        and daily_main_trend in ("Yükseliş", "Yatay")
        and nearest_resistance is not None
        and last_price < nearest_resistance
        and (relative_volume or 0) >= 1.0
    ):
        classification = (
            CLASS_TETIK_BEKLENIYOR if (dist_resistance is not None and dist_resistance < 1.5) else CLASS_ALIM_ADAYI
        )
    else:
        classification = CLASS_NOTR

    # --- Giris/stop/hedef plani (ayni destek/direnc tabanli mantik, /analiz ile ORTAK) ---
    entry_zone: tuple = (None, None)
    entry_trigger = None
    stop_price = None
    target_1 = target_2 = target_3 = None
    risk_reward = None
    invalidation_note = "Guvenilir stop hesaplanamadigi icin islem plani net degil."
    contextual_notes: list[str] = []
    liquidity_result: Optional[LiquidityResult] = None

    if snapshot is not None and sr_result is not None:
        try:
            strategy_config = get_strategy_config()
            trade_plan = compute_trade_plan(snapshot, sr_result, strategy_config["risk"], strategy_config["thresholds"])
            entry_zone = trade_plan["entry_zone"]
            entry_trigger = trade_plan["entry_trigger"]
            stop_price = trade_plan["stop_price"]
            target_1 = trade_plan["target_1"]
            target_2 = trade_plan["target_2"]
            target_3 = trade_plan["target_3"]
            risk_reward = trade_plan["risk_reward"]
            invalidation_note = trade_plan["invalidation_note"]
            contextual_notes = trade_plan["contextual_notes"]
        except Exception as exc:  # noqa: BLE001 - islem plani cikmasa da on analiz devam etmeli
            logger.warning("Gun ici islem plani hesaplanamadi symbol=%s: %s", symbol, exc)

    # --- Likidite (bolum 5) ---
    try:
        liquidity_result = compute_liquidity(intraday_df)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gun ici likidite hesaplanamadi symbol=%s: %s", symbol, exc)
        liquidity_result = None

    # --- Coklu zaman dilimi (bolum 4) ---
    multi_timeframe_result: Optional[MultiTimeframeResult] = None
    try:
        multi_timeframe_result = analyze_multi_timeframe(provider, symbol, STAGE5E_TIMEFRAMES)
    except Exception as exc:  # noqa: BLE001 - tek bir zaman dilimi hatasi tum on analizi cokertmemeli
        logger.warning("Gun ici coklu zaman dilimi hesaplanamadi symbol=%s: %s", symbol, exc)
        multi_timeframe_result = None

    # --- Nihai karar (DecisionEngine): likidite + coklu zaman dilimi kapisi ---
    decision_result: Optional[DecisionResult] = None
    if snapshot is not None:
        base_signal_type = _CLASSIFICATION_TO_BASE_SIGNAL.get(classification, "NEUTRAL")
        pseudo_confidence = "dusuk" if (is_anomalous or classification == CLASS_VERI_YETERSIZ) else "orta"
        pseudo_is_actionable = classification == CLASS_ALIM_ADAYI and stop_price is not None
        pseudo_signal = _PreviewPseudoSignal(
            signal_type=base_signal_type,
            confidence=pseudo_confidence,
            is_actionable_buy=pseudo_is_actionable,
            score=0.0,
        )
        decision_result = decide(
            pseudo_signal,
            liquidity=liquidity_result,
            multi_timeframe=multi_timeframe_result,
        )

    return IntradayPreviewResult(
        symbol=symbol,
        state=INTRADAY_PREVIEW_STATE,
        classification=classification,
        last_price=last_price,
        today_open=round(float(today_rows.iloc[0]["open"]), 2) if not today_rows.empty else None,
        today_high=round(float(today_rows["high"].max()), 2) if not today_rows.empty else None,
        today_low=round(float(today_rows["low"].min()), 2) if not today_rows.empty else None,
        previous_close=previous_close,
        daily_change_percent=daily_change_percent,
        today_volume=float(today_rows["volume"].sum()) if not today_rows.empty else None,
        relative_volume=round(relative_volume, 2) if relative_volume is not None else None,
        last_update=last_ts_py,
        is_delayed=is_delayed,
        intraday_trend=intraday_trend,
        daily_main_trend=daily_main_trend,
        nearest_support=nearest_support,
        nearest_resistance=nearest_resistance,
        distance_to_support_percent=dist_support,
        distance_to_resistance_percent=dist_resistance,
        vwap=round(vwap, 2) if vwap is not None else None,
        rsi=round(snapshot.rsi, 1) if snapshot else None,
        macd_histogram=round(snapshot.macd_histogram, 4) if snapshot else None,
        is_anomalous=is_anomalous,
        warnings=warnings,
        support_resistance=sr_result,
        entry_zone=entry_zone,
        entry_trigger=entry_trigger,
        stop_price=stop_price,
        target_1=target_1,
        target_2=target_2,
        target_3=target_3,
        risk_reward=risk_reward,
        invalidation_note=invalidation_note,
        contextual_notes=contextual_notes,
        liquidity=liquidity_result,
        multi_timeframe=multi_timeframe_result,
        decision=decision_result,
        data_quality=quality,
    )
