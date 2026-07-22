from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.analysis.advanced_scoring import AdvancedScoreBreakdown, compute_advanced_score
from app.analysis.consistency_validator import apply_consistency_guard, validate_signal_consistency
from app.analysis.data_quality import DataQualityResult
from app.analysis.decision_engine import DecisionResult, decide
from app.analysis.indicator_engine import InsufficientDataError, compute_technical_snapshot
from app.analysis.liquidity_engine import LiquidityResult, compute_liquidity
from app.analysis.market_regime_engine import classify_market_regime
from app.analysis.market_state import MODE_CONFIRMED_CLOSE, MODE_INTRADAY_PREVIEW, determine_analysis_mode
from app.analysis.multi_timeframe_engine import STAGE5E_TIMEFRAMES, MultiTimeframeResult, analyze_multi_timeframe
from app.analysis.relative_strength_engine import RelativeStrengthResult, compute_relative_strength
from app.analysis.signal_engine import SignalResult, evaluate_signal
from app.config.settings import Settings, get_strategy_config
from app.data.base_provider import BaseMarketDataProvider, DataUnavailableError
from app.data.gdelt_provider import GdeltNewsProvider
from app.models.database import Signal, SignalReason, SignalStateEnum, SignalTypeEnum
from app.services.data_quality_service import assess_and_persist_quality
from app.services.health_service import mark_runtime_health
from app.services.news_service import NewsAnalysisContext, build_news_context_for_analysis
from app.services.sector_service import get_sector_info
from app.services.current_price_service import CurrentPriceResult, resolve_current_price
from app.services.signal_explanation_service import build_analysis_explanation, serialize_contribution
from app.services.signal_outcome_tracker import SignalOutcomeTracker, SignalSnapshotInput

logger = logging.getLogger("mergen_quant.analysis_v3")


def _capture_stage5g_snapshot(
    db: Session,
    db_signal: Signal,
    signal: SignalResult,
    advanced_score: AdvancedScoreBreakdown,
    quality,
    liquidity_result,
    xu100_rs,
    sector_rs,
    news_context,
    settings,
) -> None:
    """Onayli sinyali sonradan degismeyen 5g snapshot'ina kaydeder."""
    from dataclasses import asdict
    from app.models.database import SignalScoreContribution

    explanation = build_analysis_explanation(advanced_score, quality_status=quality.status.value)
    positives = [serialize_contribution(item) for item in explanation.positive_contributions]
    negatives = [serialize_contribution(item) for item in explanation.negative_contributions]
    sr = asdict(signal.support_resistance) if signal.support_resistance is not None else {}
    item = SignalSnapshotInput(
        symbol=signal.symbol,
        signal_time=signal.data_timestamp,
        signal_price=float(signal.extras.get("analysis_close") or signal.extras.get("close")),
        last_confirmed_close=float(signal.extras.get("analysis_close") or signal.extras.get("close")),
        signal_type=signal.signal_type,
        raw_signal_score=advanced_score.total,
        rule_based_confidence=signal.confidence,
        displayed_confidence=signal.confidence,
        market_regime=signal.market_regime,
        benchmark_strength=xu100_rs.relative_score if xu100_rs and xu100_rs.available else None,
        sector_strength=sector_rs.relative_score if sector_rs and sector_rs.available else None,
        liquidity_score=liquidity_result.score if liquidity_result and liquidity_result.available else None,
        data_quality_score=float(getattr(quality, "score", 0.0) or 0.0),
        trends={"direction": signal.extras.get("trend_direction")},
        support_resistance=sr,
        stop_price=signal.stop_price,
        targets=(signal.target_1, signal.target_2, signal.target_3),
        news_impact=(news_context.impact_score if news_context and news_context.available else None),
        positive_contributions=positives,
        negative_contributions=negatives,
        strategy_version=signal.strategy_version,
        provider=signal.provider,
        price_adjustment_mode=settings.price_adjustment_mode,
    )
    snapshot = SignalOutcomeTracker(db).capture(item, signal_id=db_signal.id)
    if db.query(SignalScoreContribution).filter_by(signal_snapshot_id=snapshot.id).count() == 0:
        for contribution in explanation.all_contributions:
            db.add(SignalScoreContribution(
                signal_snapshot_id=snapshot.id,
                factor_key=contribution.factor_key,
                description=contribution.description,
                contribution=contribution.value,
                source_engine=contribution.source_engine,
                source_field=contribution.source_field,
                data_available=contribution.data_available,
            ))
        db.commit()


class AnalysisUnavailableErrorV3(Exception):
    """Analiz yapilamadiginda (veri eksik/eski/kalitesiz) firlatilir; hicbir sinyal uretilmez/kaydedilmez."""


@dataclass
class AnalysisOutcomeV3:
    signal: SignalResult
    mode: str  # confirmed_close | intraday_preview
    advanced_score: AdvancedScoreBreakdown
    xu100_relative_strength: RelativeStrengthResult
    sector_relative_strength: Optional[RelativeStrengthResult]
    intraday_quote: Optional[dict]
    is_new_signal: bool
    is_duplicate_or_cooldown: bool
    warnings: list[str] = field(default_factory=list)
    # V3.2: DecisionEngine + LiquidityEngine + MultiTimeframeEngine tam entegrasyonu.
    # Bunlar mevcut sinyal motorunu DEGISTIRMEZ; onun ciktisi uzerine ek, daha
    # temkinli bir nihai karar katmani ve gorunurluk saglar.
    liquidity: Optional[LiquidityResult] = None
    multi_timeframe: Optional[MultiTimeframeResult] = None
    decision: Optional[DecisionResult] = None
    # V3.2 (Asama 4): haber baglami (GDELT + NewsImpactEngine). GDELT
    # kapaliysa/eslestirme yoksa/hata varsa `available=False` ile gelir;
    # bu durum teknik analizi hicbir zaman etkilemez.
    news: Optional[NewsAnalysisContext] = None
    data_quality: Optional[DataQualityResult] = None
    current_price: Optional[CurrentPriceResult] = None


def run_symbol_analysis_v3(
    db: Session,
    provider: BaseMarketDataProvider,
    symbol: str,
    settings: Settings,
    strategy_config: Optional[dict] = None,
    news_provider: Optional[GdeltNewsProvider] = None,
) -> AnalysisOutcomeV3:
    """V3 uctan uca analiz: veri kalitesi -> gun ici/kapanis ayrimi ->
    teknik + destek/direnc + piyasa rejimi -> XU100/sektor goreceli guc ->
    gelismis skor -> tutarlilik denetimi -> kaydetme (yalnizca kesinlesmis
    kapanista, ayni sembol+gun icin bir kez).
    """
    strategy_config = strategy_config or get_strategy_config()
    timeframe = strategy_config["timeframes"]["primary"]
    warnings: list[str] = []

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)

    try:
        raw_df = provider.get_ohlcv(symbol, timeframe, start, end)
    except DataUnavailableError as exc:
        raise AnalysisUnavailableErrorV3(f"Bu sembol için güncel veri alınamadı. (Detay: {exc})") from exc

    quality = assess_and_persist_quality(
        db,
        raw_df,
        provider=provider,
        symbol=symbol,
        timeframe=timeframe,
        min_bars=60,
        check_incomplete=False,
    )
    if not quality.usable_for_analysis:
        raise AnalysisUnavailableErrorV3(
            "⚠️ Veri kalitesi düşük olduğu için güçlü işlem kararı üretilmedi. "
            f"Durum: {quality.status.value}. Sorunlar: " + "; ".join(quality.issues)
        )
    mark_runtime_health("data_fetch", "ok")
    warnings.extend(quality.warnings)
    if quality.fallback_used:
        warnings.append(f"Veri fallback kaynağından alındı: {quality.provider}.")
    if quality.cache_used:
        warnings.append(
            f"Canlı provider kullanılamadı; {quality.cache_age_minutes:.0f} dakikalık yerel cache kullanıldı."
            if quality.cache_age_minutes is not None
            else "Canlı provider kullanılamadı; yerel cache kullanıldı."
        )

    mode_result = determine_analysis_mode(
        raw_df,
        close_scan_time=settings.close_scan_time,
        tz_name=settings.timezone_name,
    )
    analysis_df = mode_result.analysis_df

    # Teknik motor yalnızca tamamlanmış ``analysis_df`` mumlarını kullanır.
    # Anlık işlem fiyatı ayrı bir bağlamdır ve sinyal skorunu değiştirmez.
    current_price_result = resolve_current_price(
        provider, symbol, now=end, daily_df=raw_df, timezone_name=settings.timezone_name
    )
    if current_price_result.warning:
        warnings.append(current_price_result.warning)

    quality2 = assess_and_persist_quality(
        db,
        analysis_df,
        provider=provider,
        symbol=symbol,
        timeframe=timeframe,
        min_bars=60,
        check_incomplete=False,
    )
    if not quality2.usable_for_analysis:
        raise AnalysisUnavailableErrorV3(
            "Kesinleşmiş kapanış verisi yetersiz olduğu için analiz oluşturulmadı."
        )

    try:
        snapshot = compute_technical_snapshot(analysis_df, symbol, timeframe)
    except InsufficientDataError as exc:
        raise AnalysisUnavailableErrorV3(str(exc)) from exc

    try:
        regime_result = classify_market_regime(provider, index_symbol=settings.xu100_symbol, timeframe=timeframe)
    except Exception as exc:  # noqa: BLE001 - XU100 verisi olmadan analiz cokmemeli
        logger.warning("XU100 rejim hesaplanamadi: %s", exc)
        from app.analysis.market_regime_engine import REGIME_INSUFFICIENT, MarketRegimeResult

        regime_result = MarketRegimeResult(
            regime=REGIME_INSUFFICIENT, index_symbol=settings.xu100_symbol, snapshot=None, detail="veri alinamadi"
        )
        warnings.append("XU100 piyasa rejimi verisi alınamadı; rejim 'veri_yetersiz' olarak işaretlendi.")

    signal = evaluate_signal(
        snapshot=snapshot,
        regime_result=regime_result,
        provider_name=provider.name,
        strategy_config=strategy_config,
        df=analysis_df,
    )
    signal.extras["close"] = snapshot.close
    signal.extras["analysis_close"] = current_price_result.analysis_close or snapshot.close
    signal.extras["previous_close"] = current_price_result.previous_close
    signal.extras["current_price"] = current_price_result.current_price
    signal.extras["current_price_timestamp"] = current_price_result.current_price_timestamp
    signal.extras["current_price_source"] = current_price_result.current_price_source
    signal.extras["is_live_price"] = current_price_result.is_live_price
    signal.extras["daily_change_percent"] = current_price_result.daily_change_percent
    signal.extras["trend_direction"] = snapshot.trend_direction
    signal.extras["data_quality"] = quality

    # --- XU100 goreceli guc ---
    xu100_rs = RelativeStrengthResult(available=False, note="XU100 verisi alınamadı.")
    try:
        index_df = provider.get_ohlcv(settings.xu100_symbol, timeframe, start, end)
        xu100_rs = compute_relative_strength(analysis_df, index_df)
    except DataUnavailableError as exc:
        warnings.append(f"XU100 göreceli güç hesaplanamadı: {exc}")

    # --- Sektor goreceli guc (mapping varsa) ---
    sector_rs: Optional[RelativeStrengthResult] = None
    sector_info = get_sector_info(symbol)
    if sector_info is not None and sector_info.sector_index:
        try:
            sector_df = provider.get_ohlcv(sector_info.sector_index, timeframe, start, end)
            sector_rs = compute_relative_strength(analysis_df, sector_df)
        except DataUnavailableError as exc:
            sector_rs = RelativeStrengthResult(available=False, note=f"Sektör endeksi verisi alınamadı: {exc}")
    else:
        sector_rs = RelativeStrengthResult(available=False, note="Sektör eşleştirmesi bulunamadı.")

    advanced_score = compute_advanced_score(signal, xu100_rs, sector_rs)
    if quality.status.value == "DEGRADED":
        advanced_score.total = round(max(0.0, advanced_score.total - 8.0), 2)
        warnings.append("Veri kalitesi DEGRADED olduğu için skor ve güven temkinli düşürüldü.")

    # --- Haber baglami (bolum 4): GDELT + NewsImpactEngine. Haber etkisi
    # toplam skora en fazla +-3 puan olarak eklenir; haber/GDELT verisi yoksa
    # skor DEGISMEZ ve haber tek basina AL/SAT kararini degistiremez. ---
    try:
        news_context = build_news_context_for_analysis(db, news_provider, symbol, settings)
    except Exception as exc:  # noqa: BLE001 - haber baglami analiz akisini ASLA cokertmemeli
        logger.warning("Haber baglami hesaplanamadi symbol=%s: %s", symbol, exc)
        news_context = NewsAnalysisContext(
            available=False, count_24h=0, count_7d=0, impact_score=None,
            confidence_score=None, score_contribution=0.0, note="Haber verisi alınamadı.",
        )
    if news_context.available:
        advanced_score.news_adjustment = news_context.score_contribution
        advanced_score.total = round(advanced_score.total + news_context.score_contribution, 2)

    signal.extras["advanced_score"] = advanced_score

    consistency = validate_signal_consistency(signal, snapshot.close)
    signal = apply_consistency_guard(signal, consistency)
    if not consistency.is_consistent:
        warnings.append(f"Tutarlılık kontrolü {len(consistency.issues)} bulgu ile uyarı verdi.")

    # --- Likidite filtresi (bolum 5) ---
    try:
        liquidity_result = compute_liquidity(analysis_df)
    except Exception as exc:  # noqa: BLE001 - likidite hesaplanamasa da analiz cokmemeli
        logger.warning("Likidite hesaplanamadi symbol=%s: %s", symbol, exc)
        liquidity_result = None

    # --- Coklu zaman dilimi analizi (bolum 4) ---
    try:
        multi_timeframe_result = analyze_multi_timeframe(
            provider,
            symbol,
            STAGE5E_TIMEFRAMES,
            weights={
                "1wk": settings.timeframe_weight_weekly,
                "1d": settings.timeframe_weight_daily,
                "4h": settings.timeframe_weight_4h,
                "1h": settings.timeframe_weight_1h,
                "15m": settings.timeframe_weight_15m,
                "5m": settings.timeframe_weight_5m,
            },
            timezone_name=settings.timezone_name,
        )
    except Exception as exc:  # noqa: BLE001 - bir zaman dilimi hatasi tum analizi cokertmemeli
        logger.warning("Coklu zaman dilimi hesaplanamadi symbol=%s: %s", symbol, exc)
        multi_timeframe_result = None

    # --- Nihai karar (DecisionEngine): mevcut sinyal motorunu likidite ve
    # coklu zaman dilimi kapisiyla birlikte degerlendirir; guclu AL'i
    # gerekirse engeller, ama sinyal motorunun kendisini DEGISTIRMEZ. ---
    decision_result = decide(
        signal, liquidity=liquidity_result, multi_timeframe=multi_timeframe_result,
        news_score=news_context.impact_score if news_context.available else None,
    )

    if mode_result.mode == MODE_INTRADAY_PREVIEW:
        warnings.append(mode_result.note)
        # Gun ici onizleme: guveni bir kademe dusur, aksiyona gecilebilirligi kapat.
        from app.analysis.consistency_validator import CONFIDENCE_DEMOTION

        signal.confidence = CONFIDENCE_DEMOTION.get(signal.confidence, signal.confidence)
        signal.is_actionable_buy = False
        decision_result = decide(
            signal, liquidity=liquidity_result, multi_timeframe=multi_timeframe_result,
            news_score=news_context.impact_score if news_context.available else None,
        )
        return AnalysisOutcomeV3(
            signal=signal,
            mode=MODE_INTRADAY_PREVIEW,
            advanced_score=advanced_score,
            xu100_relative_strength=xu100_rs,
            sector_relative_strength=sector_rs,
            intraday_quote=mode_result.intraday_quote,
            is_new_signal=False,
            is_duplicate_or_cooldown=False,
            warnings=warnings,
            liquidity=liquidity_result,
            multi_timeframe=multi_timeframe_result,
            decision=decision_result,
            news=news_context,
            data_quality=quality,
            current_price=current_price_result,
        )

    # --- Kesinlesmis kapanis: veritabanina kaydet (ayni sembol+gun icin tek kez) ---
    trading_date = mode_result.last_confirmed_date
    trading_date_dt = (
        datetime(trading_date.year, trading_date.month, trading_date.day, tzinfo=timezone.utc)
        if trading_date
        else None
    )

    existing = None
    if trading_date_dt is not None:
        existing = (
            db.query(Signal)
            .filter(
                Signal.symbol == symbol,
                Signal.timeframe == timeframe,
                Signal.trading_date == trading_date_dt,
                Signal.signal_type == SignalTypeEnum(signal.signal_type),
            )
            .first()
        )
    if existing is None:
        existing = db.query(Signal).filter(Signal.idempotency_key == signal.idempotency_key).first()

    if existing is not None:
        try:
            _capture_stage5g_snapshot(
                db, existing, signal, advanced_score, quality, liquidity_result,
                xu100_rs, sector_rs, news_context, settings,
            )
        except Exception as exc:  # takip hatasi analizi kullanilamaz hale getirmemeli
            db.rollback()
            logger.error("5g sinyal snapshot kaydi tamamlanamadi signal_id=%s: %s", existing.id, exc)
        return AnalysisOutcomeV3(
            signal=signal,
            mode=MODE_CONFIRMED_CLOSE,
            advanced_score=advanced_score,
            xu100_relative_strength=xu100_rs,
            sector_relative_strength=sector_rs,
            intraday_quote=None,
            is_new_signal=False,
            is_duplicate_or_cooldown=True,
            warnings=warnings,
            liquidity=liquidity_result,
            multi_timeframe=multi_timeframe_result,
            decision=decision_result,
            news=news_context,
            data_quality=quality,
            current_price=current_price_result,
        )

    db_signal = Signal(
        symbol=signal.symbol,
        timeframe=signal.timeframe,
        signal_type=SignalTypeEnum(signal.signal_type),
        state=SignalStateEnum.WAITING_TRIGGER if signal.entry_trigger else SignalStateEnum.CREATED,
        score=advanced_score.total,
        confidence=signal.confidence,
        entry_zone_low=signal.entry_zone[0],
        entry_zone_high=signal.entry_zone[1],
        entry_trigger=signal.entry_trigger,
        stop_price=signal.stop_price,
        target_1=signal.target_1,
        target_2=signal.target_2,
        target_3=signal.target_3,
        risk_reward=signal.risk_reward,
        market_regime=signal.market_regime,
        relative_strength_score=xu100_rs.relative_score,
        sector_strength_score=sector_rs.relative_score if sector_rs else None,
        analysis_mode=MODE_CONFIRMED_CLOSE,
        trading_date=trading_date_dt,
        strategy_version=signal.strategy_version,
        data_timestamp=signal.data_timestamp,
        provider=signal.provider,
        idempotency_key=signal.idempotency_key,
    )
    db.add(db_signal)
    db.flush()

    for reason in signal.reasons:
        db.add(
            SignalReason(
                signal_id=db_signal.id,
                category=reason.category,
                description=reason.description,
                is_risk=reason.is_risk,
            )
        )
    db.commit()

    try:
        _capture_stage5g_snapshot(
            db, db_signal, signal, advanced_score, quality, liquidity_result,
            xu100_rs, sector_rs, news_context, settings,
        )
    except Exception as exc:  # sinyal kaydi korunur, tracker scheduler tekrar deneyebilir
        db.rollback()
        logger.error("5g sinyal snapshot kaydi tamamlanamadi signal_id=%s: %s", db_signal.id, exc)

    return AnalysisOutcomeV3(
        signal=signal,
        mode=MODE_CONFIRMED_CLOSE,
        advanced_score=advanced_score,
        xu100_relative_strength=xu100_rs,
        sector_relative_strength=sector_rs,
        intraday_quote=None,
        is_new_signal=True,
        is_duplicate_or_cooldown=False,
        warnings=warnings,
        liquidity=liquidity_result,
        multi_timeframe=multi_timeframe_result,
        decision=decision_result,
        news=news_context,
        data_quality=quality,
        current_price=current_price_result,
    )
