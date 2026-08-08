from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import or_
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.analysis.breakout_scenario_engine import compute_breakout_scenarios
from app.analysis.data_quality import DataQualityEngine, DataQualityResult
from app.analysis.confluence_zone_engine import find_confluence_zones, strongest_confluence
from app.analysis.indicator_engine import InsufficientDataError, compute_technical_snapshot
from app.analysis.liquidity_engine import compute_liquidity
from app.analysis.multi_timeframe_engine import STAGE5E_TIMEFRAMES, analyze_multi_timeframe
from app.analysis.price_scenario_engine import compute_price_scenarios
from app.analysis.gyo_valuation_engine import collect_fundamental_payload, evaluate_gyo_valuation
from app.analysis.target_roadmap_engine import build_target_roadmap
from app.analysis.user_target_engine import evaluate_user_target
from app.analysis.relative_strength_engine import compute_relative_strength
from app.analysis.timeframe_levels_engine import compute_timeframe_levels
from app.config.settings import get_settings, get_strategy_config
from app.data.base_provider import DataUnavailableError
from app.data.gdelt_provider import build_gdelt_provider
from app.data.provider_factory import build_fundamental_provider, build_kap_provider, build_market_data_provider
from app.models.database import (
    BreakoutScenario,
    ConfluenceZoneRecord,
    EnhancedAlarmTriggerEvent,
    PriceAlert,
    PriceScenario,
    Signal,
    SignalEvent,
    SignalStateEnum,
    TimeframeLevel,
    CorporateActionRecord,
    LongTermScenario,
    TargetRealismSnapshot,
    UserPriceTarget,
    ValuationSnapshot,
)
from app.risk.position_sizing import InvalidStopError, calculate_position_size
from app.services.alert_service import VALID_ALERT_TYPES, InvalidAlertError, create_alert, delete_alert, list_alerts
from app.services.analysis_service_v3 import AnalysisUnavailableErrorV3, run_symbol_analysis_v3
from app.services.current_price_service import resolve_current_price, resolve_portfolio_prices
from app.services.data_quality_service import assess_and_persist_quality, format_data_quality_status
from app.services.health_service import collect_health
from app.services.enhanced_alert_service import (
    ALERT_LABELS,
    InvalidEnhancedAlertError,
    create_enhanced_alarm_rule,
    delete_enhanced_alarm_rule,
    get_enhanced_alarm_rule,
    list_enhanced_alarm_rules,
    set_enhanced_alarm_active,
)
from app.services.anomaly_service import AnomalyDetectionUnavailableError, list_recent_anomalies, run_symbol_anomaly_scan
from app.services.groq_service import KIND_TECHNICAL, GroqExplainer
from app.services.intraday_service import IntradayAnalysisUnavailableError, run_intraday_preview
from app.services.market_breadth_service import compute_market_breadth
from app.services.news_service import (
    build_news_context_for_analysis,
    get_recent_articles,
    scan_symbol_news,
)
from app.services.performance_service import compute_performance_report
from app.services.portfolio_service import (
    portfolio_risk_summary,
    remove_position_by_symbol,
    set_cash_balance,
    set_total_capital,
    update_position,
)
from app.services.portfolio_service import add_position as pf_add_position
from app.services.portfolio_service import list_positions as pf_list_positions
from app.services.portfolio_service import portfolio_summary as pf_portfolio_summary
from app.services.relative_strength_service import compute_symbol_relative_strength, persist_relative_strength
from app.services.scan_service import ScanBlockedByKillSwitchError, get_distinct_watchlist_symbols, run_evening_scan
from app.services.sector_service import get_sector_info, list_sector_mappings, set_sector_mapping
from app.services.settings_service import InvalidSettingError, get_or_create_settings, update_setting
from app.services.stage5e_analysis_service import (
    Stage5EContextUnavailableError,
    build_stage5e_analysis_context,
)
from app.services.target_tracking_service import (
    compute_target_performance,
    list_target_history,
    persist_roadmap_steps,
    save_target_tracking,
)
from app.services.watchlist_service import InvalidSymbolError, get_or_create_user
from app.telegram.handlers import _get_db, _reject_unauthorized
from app.telegram.message_templates_v3 import (
    format_ai_explanation,
    format_anomaly_list,
    format_anomaly_scan,
    format_detailed_analysis,
    format_evening_report,
    format_intraday_preview,
    format_liquidity,
    format_market_breadth,
    format_breadth_candidate_messages,
    format_multi_timeframe,
    format_news_detail,
    format_news_list,
    format_news_radar,
    format_performance_report,
    format_position_size_result,
    format_breakout_scenarios,
    format_guc,
    format_score_detail,
    format_sector_info,
    format_sector_not_found,
    format_seviyeler,
    format_short_summary,
    format_price_scenarios,
    format_price_metadata,
    format_corporate_actions,
    format_long_term_scenarios,
    format_long_term_scenario_detail,
    format_target_history,
    format_target_performance_stage5e,
    format_target_roadmap,
    format_user_target_check,
    format_valuation,
    split_long_message,
)
from app.telegram.formatters import sanitize_provider_error

logger = logging.getLogger("mergen_quant.telegram.v3")


def _istanbul_time(value: datetime) -> datetime:
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(ZoneInfo("Europe/Istanbul"))


async def cmd_veri_durumu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Genel veri/provider sağlığı veya tek sembol için ayrıntılı kalite sonucu."""
    if await _reject_unauthorized(update):
        return
    settings = get_settings()
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        if not context.args:
            health = collect_health(db, settings, provider=provider)
            components = health["components"]
            provider_status = components["providers"]
            await update.message.reply_text(
                "🏔️ MONTANA FİNANS ROBOTU HİSSE BOT — VERİ DURUMU\n\n"
                f"Genel durum: {health['status'].upper()}\n"
                f"Provider: {provider_status.get('provider', settings.market_data_provider)}\n"
                f"Provider sağlığı: {provider_status.get('status', 'unknown')}\n"
                f"Cache: {components['cache'].get('status', 'unknown')}\n"
                f"Database: {components['database'].get('status', 'unknown')}\n"
                f"Son başarılı veri: {components['last_successful_data_fetch'] or '-'}"
            )
            return

        symbol = context.args[0].strip().upper()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=500)
        df = None
        try:
            df = provider.get_ohlcv(symbol, "1d", start, end)
            result = assess_and_persist_quality(
                db,
                df,
                provider=provider,
                symbol=symbol,
                timeframe="1d",
                min_bars=60,
                check_incomplete=False,
            )
        except DataUnavailableError as exc:
            logger.warning("Veri durumu alınamadı symbol=%s: %s", symbol, exc)
            result = DataQualityResult.provider_down(
                getattr(provider, "name", "unknown"),
                sanitize_provider_error(exc),
            )
        quality_text = format_data_quality_status(symbol, result)
        try:
            if df is None:
                raise DataUnavailableError("Fiyat bağlamı için günlük veri bulunamadı.")
            price_context = resolve_current_price(
                provider, symbol, daily_df=df, timezone_name=settings.timezone_name
            )
            quality_text = format_price_metadata(price_context, result) + "\n\n" + quality_text
        except Exception:  # noqa: BLE001 - kalite mesajı yine gösterilir
            pass
        await update.message.reply_text(quality_text)
    finally:
        db.close()


def _persist_timeframe_levels(db, symbol: str, levels_result) -> None:
    """Hesaplanan gunluk/haftalik/aylik seviyelerin anlik goruntusunu
    (snapshot) kaydeder. Bu, mevcut sinyal/portfoy/kullanici verisine
    dokunmayan, EK (additive) bir kayittir."""
    try:
        rows = []
        for tf_result in (levels_result.daily, levels_result.weekly, levels_result.monthly):
            if not tf_result.reliable:
                continue
            for level_type, level in (
                ("destek_1", tf_result.support_1),
                ("destek_2", tf_result.support_2),
                ("ana_destek", tf_result.main_support),
                ("direnc_1", tf_result.resistance_1),
                ("direnc_2", tf_result.resistance_2),
                ("ana_direnc", tf_result.main_resistance),
            ):
                if level is None:
                    continue
                rows.append(
                    TimeframeLevel(
                        symbol=symbol,
                        timeframe=tf_result.timeframe,
                        level_type=level_type,
                        low=level.low,
                        high=level.high,
                        mid=level.mid,
                        confidence=level.confidence,
                        touches=level.touches,
                        rejections=level.rejections,
                        last_test_date=level.last_test_date,
                        sources_json=json.dumps(level.sources, ensure_ascii=False),
                        volume_confirmed=level.volume_confirmed,
                    )
                )
        if rows:
            db.add_all(rows)
            db.commit()
    except Exception:  # pragma: no cover - persistence asla ana akisi bozmamali
        logger.exception("timeframe_levels kaydedilemedi symbol=%s", symbol)
        db.rollback()


def _persist_confluence_zones(db, symbol: str, supports: list, resistances: list) -> None:
    try:
        rows = []
        for kind, zones in (("destek", supports), ("direnc", resistances)):
            for zone in zones:
                rows.append(
                    ConfluenceZoneRecord(
                        symbol=symbol,
                        kind=kind,
                        low=zone.low,
                        high=zone.high,
                        mid=zone.mid,
                        confidence=zone.confidence,
                        timeframes_json=json.dumps(zone.timeframes, ensure_ascii=False),
                        sources_json=json.dumps(zone.sources, ensure_ascii=False),
                        total_touches=zone.total_touches,
                        volume_confirmed=zone.volume_confirmed,
                    )
                )
        if rows:
            db.add_all(rows)
            db.commit()
    except Exception:  # pragma: no cover
        logger.exception("confluence_zones kaydedilemedi symbol=%s", symbol)
        db.rollback()


async def _check_kill_switch(update: Update, user) -> bool:
    if user.kill_switch_active:
        await update.message.reply_text(
            "🛑 Kill switch aktif. Analiz/tarama/alarm islemleri durduruldu. /devam_et ile tekrar acabilirsin."
        )
        return True
    return False


def _holds_position(db, user, symbol: str) -> bool:
    """Kullanicinin portfoyunde bu sembolden hisse olup olmadigini kontrol eder.
    'sat' / 'pozisyon azalt' gibi ifadeler yalnizca gercekten pozisyonu olan
    kullanicilara gosterilir; olmayana asla gosterilmez."""
    try:
        positions = pf_list_positions(db, user)
    except Exception:  # noqa: BLE001 - portfoy okunamazsa guvenli varsayim: pozisyon yok say
        return False
    symbol_norm = symbol.strip().upper()
    return any((p.symbol or "").strip().upper() == symbol_norm and (p.lot or 0) > 0 for p in positions)


def _get_open_position(db, user, symbol: str):
    """Kullanicinin bu sembol icin acik pozisyonunu (varsa) doner, yoksa None."""
    try:
        positions = pf_list_positions(db, user)
    except Exception:  # noqa: BLE001
        return None
    symbol_norm = symbol.strip().upper()
    for p in positions:
        if (p.symbol or "").strip().upper() == symbol_norm and (p.lot or 0) > 0:
            return p
    return None


def _position_portfolio_weight_pct(db, user, symbol: str, current_price: float) -> float | None:
    """Pozisyonun portfoy icindeki agirligini (%) hesaplar; diger pozisyonlar
    icin guncel fiyat bilinmiyorsa maliyet fiyati kullanilir (yaklasik deger)."""
    try:
        positions = pf_list_positions(db, user)
        prices = {p.symbol: (current_price if p.symbol == symbol.upper() else p.average_cost) for p in positions}
        summary = pf_portfolio_summary(db, user, prices)
        for row in summary["positions"]:
            if row["symbol"] == symbol.upper():
                return row["weight_percent"]
    except Exception:  # noqa: BLE001 - agirlik hesaplanamasa da analiz akisi durmamali
        return None
    return None


async def _send_analysis_chart(
    update: Update, provider, symbol: str, outcome_signal, period_days: int = 250,
    advanced_score=None, decision=None, data_quality=None, liquidity=None,
    xu100_relative_strength=None, sector_relative_strength=None,
    chart_mode: str = "standard",
) -> None:
    """Analiz mesajindan ONCE grafigi otomatik gonderir (kullanici komut vermeden).
    Once profesyonel (Asama 5c) grafigi dener; herhangi bir katman/veri eksikse
    o katmanlari atlayarak yine profesyonel grafigi uretmeye calisir. Profesyonel
    grafik tamamen basarisiz olursa eski basit fiyat grafigine duser. Grafik hic
    uretilemezse analiz akisi SESSIZCE devam eder (grafik zorunlu degildir)."""
    from datetime import timedelta

    from app.analysis.confluence_zone_engine import find_confluence_zones
    from app.analysis.timeframe_levels_engine import compute_timeframe_levels
    from app.services.chart_service import (
        delete_chart_file,
        generate_price_chart,
        generate_professional_daily_chart,
    )

    strategy_config = get_strategy_config()
    timeframe = strategy_config["timeframes"]["primary"]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=period_days)
    chart_path = None
    try:
        df = provider.get_ohlcv(symbol, timeframe, start, end)
        current_price = outcome_signal.extras.get("current_price") or float(
            df.sort_values("timestamp")["close"].iloc[-1]
        )

        timeframe_levels = None
        confluence_zones = None
        try:
            timeframe_levels = compute_timeframe_levels(df, current_price)
            supports, resistances = find_confluence_zones(timeframe_levels, current_price)
            confluence_zones = supports + resistances
        except Exception as exc:  # noqa: BLE001 - seviyeler cizilemezse grafik yine de gonderilmeli
            logger.info("Grafik icin cok-zamanli seviyeler hesaplanamadi symbol=%s: %s", symbol, exc)

        info_box = {
            "Sembol": symbol,
            "Veri zamanı": outcome_signal.data_timestamp.strftime("%d.%m.%Y %H:%M"),
            "Güncel fiyat": f"{current_price:.2f}",
            "Son kapanış": f"{outcome_signal.extras.get('analysis_close', outcome_signal.extras.get('close')):.2f}",
            "Fiyat kaynağı": outcome_signal.extras.get("current_price_source", outcome_signal.provider),
            "Nihai karar": decision.decision_class if decision else "-",
            "Skor": f"{advanced_score.total}/100" if advanced_score else "-",
            "Güven": outcome_signal.confidence,
            "Trend": outcome_signal.extras.get("trend_direction", "-"),
            "Likidite": (
                f"{liquidity.liquidity_class} ({liquidity.score:.0f}/100)"
                if liquidity and liquidity.available else "-"
            ),
            "Piyasa rejimi": outcome_signal.market_regime,
            "XU100 RS": (
                f"{xu100_relative_strength.classification} ({xu100_relative_strength.relative_score:.0f})"
                if xu100_relative_strength and xu100_relative_strength.available else "-"
            ),
            "Sektör RS": (
                f"{sector_relative_strength.classification} ({sector_relative_strength.relative_score:.0f})"
                if sector_relative_strength and sector_relative_strength.available else "-"
            ),
            "Ana destek": f"{outcome_signal.support_resistance.support_1:.2f}" if outcome_signal.support_resistance and outcome_signal.support_resistance.support_1 else "-",
            "Ana direnç": f"{outcome_signal.support_resistance.resistance_1:.2f}" if outcome_signal.support_resistance and outcome_signal.support_resistance.resistance_1 else "-",
            "Stop": f"{outcome_signal.stop_price:.2f}" if outcome_signal.stop_price else "-",
            "Hedefler": " / ".join(
                f"{target:.2f}" for target in (
                    outcome_signal.target_1, outcome_signal.target_2, outcome_signal.target_3,
                ) if target is not None
            ) or "-",
            "Risk/Getiri": outcome_signal.risk_reward if outcome_signal.risk_reward is not None else "-",
            "Veri kalitesi": (
                f"{data_quality.status.value} ({data_quality.score:.0f}/100)"
                if data_quality else "-"
            ),
            "Sağlayıcı": data_quality.provider if data_quality else outcome_signal.provider,
        }

        try:
            chart_path = await asyncio.to_thread(
                generate_professional_daily_chart,
                df, symbol,
                info_box=info_box,
                timeframe_levels=timeframe_levels,
                confluence_zones=confluence_zones,
                entry_zone=outcome_signal.entry_zone,
                entry_trigger=outcome_signal.entry_trigger,
                stop_price=outcome_signal.stop_price,
                targets=[outcome_signal.target_1, outcome_signal.target_2, outcome_signal.target_3],
                chart_mode=chart_mode,
            )
        except Exception as exc:  # noqa: BLE001 - profesyonel grafik basarisizsa eski basit grafige don
            logger.warning("Profesyonel grafik uretilemedi, basit grafige donuluyor symbol=%s: %s", symbol, exc)
            chart_path = await asyncio.to_thread(
                generate_price_chart,
                df, symbol,
                sr=outcome_signal.support_resistance,
                entry_zone=outcome_signal.entry_zone,
                stop_price=outcome_signal.stop_price,
                targets=[outcome_signal.target_1, outcome_signal.target_2, outcome_signal.target_3],
            )

        with open(chart_path, "rb") as f:
            await update.message.reply_photo(photo=f, caption=f"{symbol} profesyonel grafiği")
    except Exception as exc:  # noqa: BLE001 - grafik gonderilemese de analiz metni ONEMLI, akis durmamali
        logger.warning("Otomatik grafik gonderilemedi symbol=%s: %s", symbol, exc)
    finally:
        if chart_path:
            delete_chart_file(chart_path)


async def cmd_islemplani(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """BIST icin kosullu long/short, TP1-TP5 ve katmanli SL haritasi."""
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /islemplani THYAO")
        return
    from app.analysis.bist_trade_plan import build_bist_trade_plan
    from app.telegram.trade_plan_formatter import format_bist_trade_plan
    from app.services.chart_service import delete_chart_file, generate_bist_trade_plan_chart

    symbol = context.args[0].strip().upper().removesuffix(".IS")
    provider = build_market_data_provider(get_settings())
    chart_path = None
    try:
        end = datetime.now(timezone.utc)
        df = await asyncio.to_thread(provider.get_ohlcv, symbol, "1d", end - timedelta(days=520), end)
        multi_result = None
        try:
            multi_result = await asyncio.to_thread(
                analyze_multi_timeframe,
                provider,
                symbol,
                ("4h", "1h", "15m", "5m"),
                timezone_name=get_settings().timezone_name,
            )
        except Exception as exc:  # çoklu zaman kesintisi günlük planı engellemez
            logger.warning("İşlem planı çoklu zaman teyidi alınamadı symbol=%s: %s", symbol, exc)
        plan = await asyncio.to_thread(build_bist_trade_plan, df, symbol, multi_result)
        chart_path = await asyncio.to_thread(generate_bist_trade_plan_chart, df, plan)
        with open(chart_path, "rb") as image:
            await update.message.reply_photo(
                photo=image,
                caption=f"📈 {symbol} • Long/Short işlem haritası • TP1–TP5 • Çok katmanlı SL",
            )
        await update.message.reply_text(format_bist_trade_plan(plan))
    except (DataUnavailableError, ValueError) as exc:
        await update.message.reply_text(f"⚠️ İşlem planı üretilemedi: {exc}")
    except Exception as exc:  # noqa: BLE001 - komut botu dusurmemeli
        logger.exception("Islem plani hatasi symbol=%s", symbol)
        await update.message.reply_text(f"⚠️ İşlem planı geçici olarak üretilemedi: {exc}")
    finally:
        if chart_path:
            delete_chart_file(chart_path)


async def cmd_kademe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the shared 40/35/25 OB/FVG staged-entry plan and scenario chart."""
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /kademe THYAO")
        return

    from app.analysis.bist_trade_plan import build_bist_trade_plan
    from app.analysis.indicator_engine import compute_indicator_bundle, evaluate_indicator_confluence
    from app.analysis.staged_entry import build_staged_entry_plan, format_staged_entry_plan
    from app.modules.scenario_chart import generate_scenario_chart
    from app.services.chart_service import delete_chart_file

    symbol = context.args[0].strip().upper().removesuffix(".IS")
    settings = get_settings()
    provider = build_market_data_provider(settings)
    db = _get_db()
    chart_path = None
    try:
        user = _current_user(db, update)
        end = datetime.now(timezone.utc)
        frame = await asyncio.to_thread(
            provider.get_ohlcv, symbol, "1d", end - timedelta(days=520), end
        )
        trade_plan = await asyncio.to_thread(build_bist_trade_plan, frame, symbol)
        scenario = trade_plan.quality_zone
        if scenario is None:
            await update.message.reply_text(
                f"⚠️ {symbol} için doğrulanmış aktif OB/FVG bölgesi bulunamadı; kademe fiyatı uydurulmadı."
            )
            return
        bundle = await asyncio.to_thread(
            compute_indicator_bundle, frame, symbol=symbol, timeframe="1d"
        )
        confluence = evaluate_indicator_confluence(
            bundle,
            "bullish" if scenario.direction == "LONG" else "bearish",
            minimum_required=settings.technical_screener_min_confluence,
        )
        allocations = tuple(
            float(value.strip())
            for value in settings.staged_entry_allocations.split(",")
            if value.strip()
        )
        staged = build_staged_entry_plan(
            scenario,
            symbol=symbol,
            allocations=allocations,
            confluence=confluence,
        )
        from app.services.staged_entry_tracking_service import save_staged_entry_plan

        await asyncio.to_thread(
            save_staged_entry_plan,
            db,
            user=user,
            telegram_chat_id=update.effective_chat.id,
            plan=staged,
        )
        chart_path = await asyncio.to_thread(
            generate_scenario_chart,
            frame,
            symbol=symbol,
            plan=staged,
            direction=scenario.direction,
            output_dir=settings.report_chart_output_dir,
            dpi=settings.chart_dpi,
        )
        with open(chart_path, "rb") as image:
            await update.message.reply_photo(
                photo=image,
                caption=f"🪜 {symbol} • Kademeli {scenario.direction} • {staged.status}",
            )
        await update.message.reply_text(format_staged_entry_plan(staged))
        await update.message.reply_text(
            "🔔 Sanal kademe izlemesi aktif. Her dolan kademede yeni ortalama maliyet, "
            "invalidation kapanışında ise kalan kademelerin iptal bildirimi gelir. "
            "Bot gerçek emir göndermez."
        )
    except (DataUnavailableError, ValueError, InsufficientDataError) as exc:
        await update.message.reply_text(f"⚠️ Kademeli plan üretilemedi: {exc}")
    except Exception as exc:  # noqa: BLE001 - command must never crash polling
        logger.exception("Kademeli plan hatasi symbol=%s", symbol)
        await update.message.reply_text(f"⚠️ Kademeli plan geçici olarak üretilemedi: {exc}")
    finally:
        if chart_path:
            delete_chart_file(chart_path)
        db.close()


def _current_user(db, update: Update):
    settings = get_settings()
    return get_or_create_user(
        db, update.effective_user.id, update.effective_user.id in settings.admin_ids, settings.default_total_capital
    )


# ---------------------------------------------------------------------------
# /analiz - V3 (gun ici / kesinlesmis kapanis ayrimi + kisa ozet + detay)
# ---------------------------------------------------------------------------


async def cmd_analiz_v3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /analiz SEMBOL (orn: /analiz THYAO)")
        return

    symbol = context.args[0].strip().upper()
    settings = get_settings()
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        user = _current_user(db, update)
        if await _check_kill_switch(update, user):
            return

        outcome = run_symbol_analysis_v3(db, provider, symbol, settings, news_provider=build_gdelt_provider(settings))
        holds_position = _holds_position(db, user, symbol)
        position = _get_open_position(db, user, symbol) if holds_position else None
        close_price = outcome.signal.extras.get("current_price") or outcome.signal.extras.get("close")
        weight_pct = (
            _position_portfolio_weight_pct(db, user, symbol, close_price)
            if position is not None and close_price
            else None
        )
        await _send_analysis_chart(
            update, provider, symbol, outcome.signal,
            advanced_score=outcome.advanced_score, decision=outcome.decision,
            data_quality=outcome.data_quality, liquidity=outcome.liquidity,
            xu100_relative_strength=outcome.xu100_relative_strength,
            sector_relative_strength=outcome.sector_relative_strength,
            chart_mode="standard",
        )
        short_text = format_short_summary(
            outcome.signal, symbol, outcome.mode, outcome.advanced_score, outcome.xu100_relative_strength,
            decision=outcome.decision, holds_position=holds_position, news=outcome.news,
            position=position, portfolio_weight_pct=weight_pct,
        )

        from app.analysis.score_calibration_engine import lookup_calibrated_success
        from app.services.signal_explanation_service import build_analysis_explanation
        explanation = build_analysis_explanation(
            outcome.advanced_score,
            quality_status=outcome.data_quality.status.value if outcome.data_quality else "VALID",
        )
        positives, negatives = explanation.top_reasons(2)
        reason_lines = ["", "Kararin ana nedenleri:"]
        reason_lines.extend(f"+ {item.description}: +{item.value:g}" for item in positives)
        reason_lines.extend(f"- {item.description}: {item.value:g}" for item in negatives)
        short_text += "\n".join(reason_lines)
        sector_info = get_sector_info(symbol)
        calibrated = lookup_calibrated_success(
            db,
            score=outcome.advanced_score.total,
            symbol=symbol,
            sector=sector_info.sector_name if sector_info else None,
            minimum_sample_size=settings.calibration_minimum_sample_size,
        )
        if calibrated is not None:
            short_text += (
                f"\nTarihsel basari: %{calibrated.calibrated_success_rate:.1f} "
                f"(n={calibrated.calibration_sample_count}, {calibrated.calibration_scope})"
                "\nGecmis benzer sinyallerin oranidir; gelecek sonucu garanti etmez."
            )

        await update.message.reply_text(short_text, reply_markup=_analysis_action_keyboard(symbol))
        try:
            from app.services.multi_timeframe_explanation_service import (
                build_multi_timeframe_package, format_multi_timeframe_explanation,
            )
            from app.services.chart_service import delete_chart_file, generate_multi_timeframe_chart
            multi, frames = await asyncio.to_thread(
                build_multi_timeframe_package, provider, symbol,
                timezone_name=settings.timezone_name,
            )
            await update.message.reply_text(format_multi_timeframe_explanation(symbol, multi))
            mtf_chart = await asyncio.to_thread(generate_multi_timeframe_chart, frames, symbol)
            try:
                with open(mtf_chart, "rb") as image:
                    await update.message.reply_photo(image, caption=f"⏱️ {symbol} • 5dk / 15dk / 1s / 4s")
            finally:
                delete_chart_file(mtf_chart)
        except Exception as exc:  # noqa: BLE001 - ana analiz bundan bağımsız çalışır
            logger.warning("Çoklu zaman açıklaması üretilemedi symbol=%s: %s", symbol, exc)
    except AnalysisUnavailableErrorV3 as exc:
        logger.warning("Analiz üretilemedi symbol=%s: %s", symbol, exc)
        await update.message.reply_text(sanitize_provider_error(exc))
    except InsufficientDataError as exc:
        logger.warning("Analiz verisi yetersiz symbol=%s: %s", symbol, exc)
        await update.message.reply_text("⚠️ Güncel veri yetersiz olduğu için bazı bölümler hesaplanamadı.")
    finally:
        db.close()


async def cmd_analiz_detay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dogrudan detayli analiz (kisa ozetten sonra buton yerine komutla da erisim)."""
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /analiz_detay SEMBOL")
        return

    symbol = context.args[0].strip().upper()
    settings = get_settings()
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        user = _current_user(db, update)
        if await _check_kill_switch(update, user):
            return

        outcome = run_symbol_analysis_v3(db, provider, symbol, settings, news_provider=build_gdelt_provider(settings))
        sector_info = get_sector_info(symbol)
        sector_name = sector_info.sector_name if sector_info else "Eslesmemis"
        holds_position = _holds_position(db, user, symbol)
        position = _get_open_position(db, user, symbol) if holds_position else None
        close_price = outcome.signal.extras.get("current_price") or outcome.signal.extras.get("close")
        weight_pct = (
            _position_portfolio_weight_pct(db, user, symbol, close_price)
            if position is not None and close_price
            else None
        )

        await _send_analysis_chart(
            update, provider, symbol, outcome.signal,
            advanced_score=outcome.advanced_score, decision=outcome.decision,
            data_quality=outcome.data_quality, liquidity=outcome.liquidity,
            xu100_relative_strength=outcome.xu100_relative_strength,
            sector_relative_strength=outcome.sector_relative_strength,
            chart_mode="detailed",
        )

        text = format_detailed_analysis(
            outcome.signal, symbol, outcome.mode, outcome.advanced_score,
            outcome.xu100_relative_strength, outcome.sector_relative_strength, sector_name,
            outcome.intraday_quote, outcome.warnings,
            decision=outcome.decision, holds_position=holds_position, news=outcome.news,
            position=position, portfolio_weight_pct=weight_pct,
        )
        for part in split_long_message(text):
            await update.message.reply_text(part)
    except AnalysisUnavailableErrorV3 as exc:
        logger.warning("Detay analiz üretilemedi symbol=%s: %s", symbol, exc)
        await update.message.reply_text(sanitize_provider_error(exc))
    finally:
        db.close()


async def handle_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    symbol = query.data.replace("detay_", "")
    settings = get_settings()
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        outcome = run_symbol_analysis_v3(db, provider, symbol, settings, news_provider=build_gdelt_provider(settings))
        sector_info = get_sector_info(symbol)
        sector_name = sector_info.sector_name if sector_info else "Eslesmemis"
        user = _current_user(db, update)
        holds_position = _holds_position(db, user, symbol)
        position = _get_open_position(db, user, symbol) if holds_position else None
        close_price = outcome.signal.extras.get("current_price") or outcome.signal.extras.get("close")
        weight_pct = (
            _position_portfolio_weight_pct(db, user, symbol, close_price)
            if position is not None and close_price
            else None
        )
        text = format_detailed_analysis(
            outcome.signal, symbol, outcome.mode, outcome.advanced_score,
            outcome.xu100_relative_strength, outcome.sector_relative_strength, sector_name,
            outcome.intraday_quote, outcome.warnings,
            decision=outcome.decision, holds_position=holds_position, news=outcome.news,
            position=position, portfolio_weight_pct=weight_pct,
        )
        for part in split_long_message(text):
            await query.message.reply_text(part)
    except AnalysisUnavailableErrorV3 as exc:
        logger.warning("Detay callback üretilemedi symbol=%s: %s", symbol, exc)
        await query.message.reply_text(sanitize_provider_error(exc))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# V3.1: /gunici - gun ici on analiz (bolum 3)
# ---------------------------------------------------------------------------


async def _send_intraday_chart(update: Update, provider, symbol: str, intraday_result) -> None:
    """/gunici icin gun ici (15dk) profesyonel grafik: VWAP + EMA20/50 + gunluk
    ana destek/direnc + hacim + RSI + anomali isaretleri. Grafik uretilemezse
    metin analizi yine de gonderilmeye devam eder."""
    from datetime import timedelta

    from app.services.chart_service import delete_chart_file, generate_intraday_chart

    chart_path = None
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=5)
        df_intraday = provider.get_ohlcv(symbol, "15m", start, end)
        chart_path = await asyncio.to_thread(
            generate_intraday_chart,
            df_intraday, symbol,
            daily_support=intraday_result.nearest_support,
            daily_resistance=intraday_result.nearest_resistance,
            previous_close=intraday_result.previous_close,
            info_box={
                "Sembol": symbol,
                "Zaman": datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC"),
                "Son fiyat": f"{intraday_result.last_price:.2f}",
                "Veri kalitesi": (
                    f"{intraday_result.data_quality.status.value} ({intraday_result.data_quality.score}/100)"
                    if intraday_result.data_quality else "-"
                ),
                "Sağlayıcı": intraday_result.data_quality.provider if intraday_result.data_quality else "-",
            },
        )
        with open(chart_path, "rb") as f:
            await update.message.reply_photo(photo=f, caption=f"{symbol} gün içi grafiği")
    except Exception as exc:  # noqa: BLE001 - grafik gonderilemese de analiz metni ONEMLI, akis durmamali
        logger.warning("Gun ici grafik gonderilemedi symbol=%s: %s", symbol, exc)
    finally:
        if chart_path:
            delete_chart_file(chart_path)


async def cmd_gunici(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /gunici SEMBOL (orn: /gunici SVGYO)")
        return

    symbol = context.args[0].strip().upper()
    settings = get_settings()
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        user = _current_user(db, update)
        if await _check_kill_switch(update, user):
            return

        result = run_intraday_preview(provider, symbol)
        result.current_price_context = resolve_current_price(
            provider, symbol, timezone_name=settings.timezone_name
        )
        holds_position = _holds_position(db, user, symbol)
        await _send_intraday_chart(update, provider, symbol, result)
        news_context = build_news_context_for_analysis(db, build_gdelt_provider(settings), symbol, settings)
        text = format_intraday_preview(result, symbol, holds_position=holds_position, news=news_context)
        await update.message.reply_text(text)
    except IntradayAnalysisUnavailableError as exc:
        await update.message.reply_text(str(exc))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# V3.2 (Asama 3): Anormal hareket / anomali komutlari
# ---------------------------------------------------------------------------


async def cmd_anomali(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tek bir sembol icin ANLIK anomali taramasi yapar (hacim/gap/kirilma/volatilite)."""
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /anomali SEMBOL (orn: /anomali THYAO)")
        return

    symbol = context.args[0].strip().upper()
    settings = get_settings()
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        user = _current_user(db, update)
        if await _check_kill_switch(update, user):
            return
        outcome = run_symbol_anomaly_scan(db, provider, symbol)
        await update.message.reply_text(format_anomaly_scan(outcome.result, symbol))
    except AnomalyDetectionUnavailableError as exc:
        await update.message.reply_text(str(exc))
    finally:
        db.close()


async def cmd_anomaliler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Izleme listesindeki semboller icin son N saatte KAYDEDILMIS anomalileri listeler.
    Kullanim: /anomaliler [saat] (varsayilan: 48 saat)"""
    if await _reject_unauthorized(update):
        return
    since_hours = 48
    if context.args:
        try:
            since_hours = max(1, int(context.args[0]))
        except ValueError:
            await update.message.reply_text("Kullanim: /anomaliler [saat] (orn: /anomaliler 24)")
            return

    db = _get_db()
    try:
        user = _current_user(db, update)
        from app.services.watchlist_service import list_symbols

        watch_items = list_symbols(db, user)
        symbols = [w.symbol for w in watch_items] or None
        anomalies = list_recent_anomalies(db, symbols=symbols, since_hours=since_hours)
        await update.message.reply_text(format_anomaly_list(anomalies, since_hours))
    finally:
        db.close()


async def cmd_zaman_dilimleri(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /zaman_dilimleri SEMBOL (orn: /zaman_dilimleri SVGYO)")
        return

    symbol = context.args[0].strip().upper()
    settings = get_settings()
    if not settings.multi_timeframe_enabled:
        await update.message.reply_text("Çoklu zaman dilimi analizi şu anda devre dışı (MULTI_TIMEFRAME_ENABLED=false).")
        return
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        user = _current_user(db, update)
        if await _check_kill_switch(update, user):
            return

        result = analyze_multi_timeframe(
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
        price_context = resolve_current_price(
            provider, symbol, timezone_name=settings.timezone_name
        )
        text = format_multi_timeframe(result, symbol, price_context=price_context)
        await update.message.reply_text(text)
        from app.services.multi_timeframe_explanation_service import build_multi_timeframe_package
        from app.services.chart_service import delete_chart_file, generate_multi_timeframe_chart
        _, frames = await asyncio.to_thread(
            build_multi_timeframe_package, provider, symbol, timezone_name=settings.timezone_name,
        )
        chart_path = await asyncio.to_thread(generate_multi_timeframe_chart, frames, symbol)
        try:
            with open(chart_path, "rb") as image:
                await update.message.reply_photo(image, caption=f"⏱️ {symbol} • Çoklu zaman teknik haritası")
        finally:
            delete_chart_file(chart_path)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# V3.2 (Asama 4): Haber radari + haber etkisi + Groq AI aciklama komutlari
# ---------------------------------------------------------------------------


async def cmd_haberler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bir sembol icin son GDELT haberlerini listeler (bolum 1)."""
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /haberler SEMBOL (orn: /haberler THYAO)")
        return

    symbol = context.args[0].strip().upper()
    settings = get_settings()
    if not settings.gdelt_enabled:
        await update.message.reply_text("Haber radarı şu anda devre dışı (GDELT_ENABLED=false).")
        return

    db = _get_db()
    try:
        user = _current_user(db, update)
        if await _check_kill_switch(update, user):
            return
        scan_symbol_news(db, symbol, build_gdelt_provider(settings), settings)
        articles = get_recent_articles(db, symbol, limit=10)
        await update.message.reply_text(format_news_list(symbol, articles))
    finally:
        db.close()


async def cmd_haber(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """24-48 hour source-linked symbol digest with 15-minute DB cache."""
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /haber THYAO")
        return

    from app.services.news_digest_service import build_news_digest, format_news_digest

    symbol = context.args[0].strip().upper().removesuffix(".IS")
    settings = get_settings()
    db = _get_db()
    try:
        digest = await asyncio.to_thread(
            build_news_digest,
            db,
            symbol=symbol,
            settings=settings,
            gdelt_provider=build_gdelt_provider(settings),
            kap_provider=build_kap_provider(settings),
        )
        await update.message.reply_text(
            format_news_digest(digest),
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001 - news cannot crash polling
        logger.exception("Haber ozet komutu hata verdi symbol=%s", symbol)
        await update.message.reply_text(
            f"⚠️ {symbol} haber taraması geçici olarak tamamlanamadı ({type(exc).__name__})."
        )
    finally:
        db.close()


async def cmd_haber_detay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bir sembol icin kural tabanli haber etkisi detayini gosterir (bolum 2)."""
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /haber_detay SEMBOL (orn: /haber_detay THYAO)")
        return

    symbol = context.args[0].strip().upper()
    settings = get_settings()
    if not settings.gdelt_enabled:
        await update.message.reply_text("Haber radarı şu anda devre dışı (GDELT_ENABLED=false).")
        return

    db = _get_db()
    try:
        user = _current_user(db, update)
        if await _check_kill_switch(update, user):
            return
        outcome = scan_symbol_news(db, symbol, build_gdelt_provider(settings), settings)
        await update.message.reply_text(format_news_detail(symbol, outcome.summary_24h, outcome.summary_7d))
    finally:
        db.close()


async def cmd_haber_radari(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Izleme listesindeki semboller arasinda ONEMLI haberi olanlari tarar (bolum 1)."""
    if await _reject_unauthorized(update):
        return
    settings = get_settings()
    if not settings.gdelt_enabled:
        await update.message.reply_text("Haber radarı şu anda devre dışı (GDELT_ENABLED=false).")
        return

    db = _get_db()
    try:
        user = _current_user(db, update)
        if await _check_kill_switch(update, user):
            return
        from app.services.watchlist_service import list_symbols

        watch_items = list_symbols(db, user)
        symbols = [w.symbol for w in watch_items]
        if not symbols:
            await update.message.reply_text("İzleme listen boş; önce /ekle SEMBOL ile sembol ekleyebilirsin.")
            return

        provider = build_gdelt_provider(settings)
        results = []
        for symbol in symbols:
            try:
                outcome = scan_symbol_news(db, symbol, provider, settings)
                results.append((symbol, outcome.summary_7d))
            except Exception as exc:  # noqa: BLE001 - bir sembol hata verse de tarama devam etmeli
                logger.warning("Haber radarı sembol taraması başarısız symbol=%s: %s", symbol, exc)
                results.append((symbol, None))
        await update.message.reply_text(format_news_radar(results))
    finally:
        db.close()


async def cmd_ai_aciklama(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Groq (opsiyonel) ile mevcut teknik analizin sade Turkce aciklamasini uretir (bolum 3).
    Groq kapali/hata/kota durumunda deterministik sablona duser; hicbir zaman
    fiyat/hedef/stop uretmez veya AL/SAT kararini degistirmez."""
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /ai_aciklama SEMBOL (orn: /ai_aciklama THYAO)")
        return

    symbol = context.args[0].strip().upper()
    settings = get_settings()
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        user = _current_user(db, update)
        if await _check_kill_switch(update, user):
            return

        outcome = run_symbol_analysis_v3(db, provider, symbol, settings)

        structured_payload = {
            "sinyal": outcome.signal.signal_type,
            "guven": outcome.signal.confidence,
            "skor": outcome.advanced_score.total,
            "trend": outcome.signal.extras.get("trend_direction"),
            "piyasa_rejimi": outcome.signal.market_regime,
            "xu100_goreceli_guc": outcome.xu100_relative_strength.relative_score if outcome.xu100_relative_strength.available else None,
        }

        explainer = GroqExplainer(settings)
        try:
            explanation, is_fallback = explainer.explain(db, symbol, KIND_TECHNICAL, structured_payload)
        finally:
            explainer.close()

        await update.message.reply_text(format_ai_explanation(symbol, explanation, is_fallback))
    except AnalysisUnavailableErrorV3 as exc:
        await update.message.reply_text(str(exc))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# V3.1: /likidite - likidite filtresi (bolum 5)
# ---------------------------------------------------------------------------


async def cmd_likidite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /likidite SEMBOL (orn: /likidite SVGYO)")
        return

    symbol = context.args[0].strip().upper()
    settings = get_settings()
    if not settings.liquidity_filter_enabled:
        await update.message.reply_text("Likidite filtresi şu anda devre dışı (LIQUIDITY_FILTER_ENABLED=false).")
        return
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        user = _current_user(db, update)
        if await _check_kill_switch(update, user):
            return

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=200)
        try:
            df = provider.get_ohlcv(symbol, "1d", start, end)
        except DataUnavailableError as exc:
            await update.message.reply_text(f"Likidite hesaplanamadi: {exc}")
            return

        liquidity_config = {
            "minimum_average_volume": settings.minimum_average_volume,
            "minimum_average_turnover_try": settings.minimum_average_turnover_try,
            "maximum_atr_percent": settings.maximum_atr_percent,
            "strong_signal_minimum_score": settings.strong_signal_minimum_liquidity_score,
        }
        result = compute_liquidity(df, config=liquidity_config)
        text = format_liquidity(result, symbol)
        await update.message.reply_text(text)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# MERGEN QUANT - Asama 5: /seviyeler (gunluk/haftalik/aylik destek-direnc)
# ---------------------------------------------------------------------------


def _persist_price_scenarios(db, symbol: str, result) -> None:
    if not result.reliable:
        return
    try:
        rows = []
        for direction, tier, zone in (
            ("dusus", "yakin", result.decline_near),
            ("dusus", "ana", result.decline_main),
            ("dusus", "asiri", result.decline_extreme),
            ("yukselis", "yakin", result.rise_near),
            ("yukselis", "ana", result.rise_main),
            ("yukselis", "guclu_kirilim", result.rise_breakout),
            ("yukselis", "asiri", result.rise_extreme),
        ):
            if zone is None:
                continue
            rows.append(
                PriceScenario(
                    symbol=symbol,
                    direction=direction,
                    scenario_type=tier,
                    low=zone.low,
                    high=zone.high,
                    confidence=zone.confidence,
                    activation_condition=zone.activation_condition,
                )
            )
        if rows:
            db.add_all(rows)
            db.commit()
    except Exception:  # pragma: no cover
        logger.exception("price_scenarios kaydedilemedi symbol=%s", symbol)
        db.rollback()


def _persist_breakout_scenarios(db, symbol: str, result) -> None:
    if not result.reliable:
        return
    try:
        rows = []
        for level_type, case in (("direnc", result.resistance_breakout), ("destek", result.support_breakdown)):
            if case is None:
                continue
            rows.append(
                BreakoutScenario(
                    symbol=symbol,
                    level_type=level_type,
                    level_price=case.confirmation_close_level,
                    confirmation_close_level=case.confirmation_close_level,
                    min_volume_note=case.min_volume_note,
                    target_1=case.target_1,
                    target_2=case.target_2,
                    failure_level=case.failure_level,
                    false_breakout_risk_note=f"{case.false_breakout_risk}: {case.false_breakout_note}",
                )
            )
        if rows:
            db.add_all(rows)
            db.commit()
    except Exception:  # pragma: no cover
        logger.exception("breakout_scenarios kaydedilemedi symbol=%s", symbol)
        db.rollback()


async def cmd_seviyeler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /seviyeler SEMBOL (orn: /seviyeler THYAO)")
        return

    symbol = context.args[0].strip().upper()
    settings = get_settings()
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=500)
        try:
            df = provider.get_ohlcv(symbol, "1d", start, end)
        except DataUnavailableError as exc:
            logger.warning("Seviyeler verisi alınamadı symbol=%s: %s", symbol, exc)
            await update.message.reply_text(sanitize_provider_error(exc))
            return

        if df is None or df.empty:
            await update.message.reply_text("Seviyeler hesaplanamadi: veri bulunamadi.")
            return

        quality = assess_and_persist_quality(
            db,
            df,
            provider=provider,
            symbol=symbol,
            timeframe="1d",
            min_bars=60,
            check_incomplete=False,
        )
        if not quality.usable_for_analysis:
            await update.message.reply_text(
                f"⚠️ Seviyeler hesaplanmadı: veri durumu {quality.status.value} ({quality.score}/100)."
            )
            return
        df = DataQualityEngine().completed_candles(df, "1d")

        price_context = resolve_current_price(
            provider, symbol, daily_df=df, timezone_name=settings.timezone_name
        )
        current_price = price_context.current_price
        levels_result = compute_timeframe_levels(df, price_context.analysis_close)
        confluence_supports, confluence_resistances = find_confluence_zones(levels_result, current_price)

        _persist_timeframe_levels(db, symbol, levels_result)
        _persist_confluence_zones(db, symbol, confluence_supports, confluence_resistances)

        text = format_seviyeler(
            symbol, current_price, levels_result, confluence_supports, confluence_resistances,
            price_context=price_context, quality=quality,
        )
        for chunk in split_long_message(text):
            await update.message.reply_text(chunk)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# MERGEN QUANT - Asama 5b: /senaryo (dusus/yukselis senaryo bolgeleri)
# ---------------------------------------------------------------------------


async def cmd_senaryo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /senaryo SEMBOL (orn: /senaryo THYAO)")
        return

    symbol = context.args[0].strip().upper()
    settings = get_settings()
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=500)
        try:
            df = provider.get_ohlcv(symbol, "1d", start, end)
        except DataUnavailableError as exc:
            logger.warning("Senaryo verisi alınamadı symbol=%s: %s", symbol, exc)
            await update.message.reply_text(sanitize_provider_error(exc))
            return

        if df is None or df.empty:
            await update.message.reply_text("Senaryolar hesaplanamadi: veri bulunamadi.")
            return

        quality = assess_and_persist_quality(
            db, df, provider=provider, symbol=symbol, timeframe="1d",
            min_bars=60, check_incomplete=False,
        )
        if not quality.usable_for_analysis:
            await update.message.reply_text(
                f"⚠️ Senaryolar hesaplanmadı: veri durumu {quality.status.value} ({quality.score}/100)."
            )
            return
        df = DataQualityEngine().completed_candles(df, "1d")

        price_context = resolve_current_price(
            provider, symbol, daily_df=df, timezone_name=settings.timezone_name
        )
        current_price = price_context.current_price
        levels_result = compute_timeframe_levels(df, price_context.analysis_close)
        confluence_supports, confluence_resistances = find_confluence_zones(levels_result, current_price)

        liquidity_config = get_strategy_config().get("liquidity", {})
        liquidity = compute_liquidity(df, config=liquidity_config)

        scenario_result = compute_price_scenarios(
            levels_result,
            confluence_supports,
            confluence_resistances,
            current_price,
            liquidity_score=liquidity.score if liquidity.available else None,
        )

        _persist_price_scenarios(db, symbol, scenario_result)

        text = format_price_scenarios(
            symbol, current_price, scenario_result,
            price_context=price_context, quality=quality,
        )
        for chunk in split_long_message(text):
            await update.message.reply_text(chunk)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# MERGEN QUANT - Asama 5b: /kirilsanaryo ("bu seviye kirilirsa ne olur?")
# ---------------------------------------------------------------------------


async def cmd_kirilsanaryo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /kirilsanaryo SEMBOL (orn: /kirilsanaryo THYAO)")
        return

    symbol = context.args[0].strip().upper()
    settings = get_settings()
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=500)
        try:
            df = provider.get_ohlcv(symbol, "1d", start, end)
        except DataUnavailableError as exc:
            logger.warning("Kırılım senaryosu verisi alınamadı symbol=%s: %s", symbol, exc)
            await update.message.reply_text(sanitize_provider_error(exc))
            return

        if df is None or df.empty:
            await update.message.reply_text("Kirilim senaryolari hesaplanamadi: veri bulunamadi.")
            return

        quality = assess_and_persist_quality(
            db, df, provider=provider, symbol=symbol, timeframe="1d",
            min_bars=60, check_incomplete=False,
        )
        if not quality.usable_for_analysis:
            await update.message.reply_text(
                f"⚠️ Kırılım senaryoları hesaplanmadı: veri durumu {quality.status.value} ({quality.score}/100)."
            )
            return
        df = DataQualityEngine().completed_candles(df, "1d")

        try:
            snapshot = compute_technical_snapshot(df, symbol, "1d")
        except InsufficientDataError as exc:
            await update.message.reply_text(f"Kirilim senaryolari hesaplanamadi: {exc}")
            return

        price_context = resolve_current_price(
            provider, symbol, daily_df=df, timezone_name=settings.timezone_name
        )
        current_price = price_context.current_price
        levels_result = compute_timeframe_levels(df, snapshot.close)
        confluence_supports, confluence_resistances = find_confluence_zones(levels_result, current_price)
        best_support, best_resistance = strongest_confluence(levels_result, current_price)

        # En onemli direnc/destek: once cakisan guclu bolge, yoksa gunluk
        # ana destek/direnc, o da yoksa haftalik ana destek/direnc.
        resistance_zone = (
            best_resistance
            or levels_result.daily.main_resistance
            or levels_result.weekly.main_resistance
        )
        support_zone = (
            best_support
            or levels_result.daily.main_support
            or levels_result.weekly.main_support
        )

        liquidity_config = get_strategy_config().get("liquidity", {})
        liquidity = compute_liquidity(df, config=liquidity_config)

        # Hedefler ATR ile uydurulmaz: mevcut çoklu-zaman seviyeleri, gerçek
        # OB/FVG sınırları ve teyit edilmiş swing/MSS fiyatları birlikte verilir.
        from app.analysis.smart_money_engine import detect_smart_money

        smart_money = detect_smart_money(df)
        pd_array_levels: list[tuple[float, str]] = []
        for level in levels_result.all_zones():
            pd_array_levels.append(
                (float(level.mid), f"{level.timeframe} {level.strength_class} yapı bölgesi")
            )
        for zone in (*smart_money.order_blocks, *smart_money.fvg):
            target_edge = zone.low if zone.direction == "bearish" else zone.high
            pd_array_levels.append(
                (float(target_edge), f"{zone.direction} {zone.kind} bölgesi")
            )
        for event in smart_money.structure:
            pd_array_levels.append(
                (float(event.price), f"{event.kind} / {event.direction} swing likiditesi")
            )

        breakout_result = compute_breakout_scenarios(
            resistance_zone=resistance_zone,
            support_zone=support_zone,
            current_price=current_price,
            atr_value=snapshot.atr,
            relative_volume=snapshot.relative_volume,
            adx=snapshot.adx,
            liquidity_score=liquidity.score if liquidity.available else None,
            pd_array_levels=pd_array_levels,
        )

        _persist_breakout_scenarios(db, symbol, breakout_result)

        text = format_breakout_scenarios(
            symbol, current_price, breakout_result,
            price_context=price_context, quality=quality,
        )
        from app.services.chart_service import delete_chart_file

        chart_path = None
        try:
            from app.modules.scenario_chart import render_breakout_scenario_chart

            chart_path = await asyncio.to_thread(
                render_breakout_scenario_chart,
                df,
                symbol=symbol,
                result=breakout_result,
                output_dir=settings.report_chart_output_dir,
                dpi=settings.chart_dpi,
            )
            with open(chart_path, "rb") as image:
                await update.message.reply_photo(
                    photo=image,
                    caption=(
                        f"📊 {symbol} • Koşullu kırılım haritası\n"
                        "Mum kapanışı ve hacim teyidi olmadan hiçbir rota aktif değildir."
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - görsel aksasa da metin gönderilir
            logger.warning("Kırılım senaryo grafiği üretilemedi symbol=%s: %s", symbol, exc)
        finally:
            if chart_path:
                delete_chart_file(chart_path)
        for chunk in split_long_message(text):
            await update.message.reply_text(chunk)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Skor detayi
# ---------------------------------------------------------------------------


async def cmd_skor_detay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /skor_detay SEMBOL")
        return
    symbol = context.args[0].strip().upper()
    settings = get_settings()
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        outcome = run_symbol_analysis_v3(db, provider, symbol, settings)
        await update.message.reply_text(format_score_detail(outcome.signal, symbol, outcome.advanced_score))
    except AnalysisUnavailableErrorV3 as exc:
        await update.message.reply_text(str(exc))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Sektor komutlari
# ---------------------------------------------------------------------------


async def cmd_sektor_ayarla(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text("Kullanim: /sektor_ayarla SEMBOL ENDEKS.IS Sektor Adi")
        return
    symbol, sector_index = context.args[0], context.args[1]
    sector_name = " ".join(context.args[2:])
    info = set_sector_mapping(symbol, sector_index, sector_name)
    await update.message.reply_text(f"'{info.symbol}' için sektör eşleştirmesi kaydedildi: {info.sector_name} ({info.sector_index})")


async def cmd_sektor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /sektor SEMBOL")
        return
    symbol = context.args[0].strip().upper()
    info = get_sector_info(symbol)
    if info is None:
        await update.message.reply_text(format_sector_not_found(symbol))
        return

    settings = get_settings()
    provider = build_market_data_provider(settings)
    from datetime import timedelta
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=500)
    strategy_config = get_strategy_config()
    timeframe = strategy_config["timeframes"]["primary"]
    try:
        stock_df = provider.get_ohlcv(symbol, timeframe, start, end)
        sector_df = provider.get_ohlcv(info.sector_index, timeframe, start, end)
        rs = compute_relative_strength(stock_df, sector_df)
    except Exception:  # noqa: BLE001
        from app.analysis.relative_strength_engine import RelativeStrengthResult
        rs = RelativeStrengthResult(available=False, note="Sektör endeksi verisi alınamadı.")

    await update.message.reply_text(format_sector_info(symbol, info.sector_name, info.sector_index, rs))


# ---------------------------------------------------------------------------
# Asama 5c, Bolum 2: /guc SEMBOL - gelismis XU100/sektor donemsel goreceli guc.
# ---------------------------------------------------------------------------


async def cmd_guc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /guc SEMBOL (orn: /guc THYAO)")
        return

    symbol = context.args[0].strip().upper()
    settings = get_settings()
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        result = compute_symbol_relative_strength(provider, symbol, settings)
        try:
            persist_relative_strength(db, result)
        except Exception as exc:  # noqa: BLE001 - kaydetme basarisiz olsa da kullaniciya sonuc gosterilmeli
            logger.warning("relative_strength_periods kaydedilemedi symbol=%s: %s", symbol, exc)
            db.rollback()
        price_context = resolve_current_price(
            provider, symbol, timezone_name=settings.timezone_name
        )
        await update.message.reply_text(
            format_price_metadata(price_context) + "\n\n" + format_guc(symbol, result)
        )
    except DataUnavailableError as exc:
        logger.warning("Göreceli güç verisi alınamadı symbol=%s: %s", symbol, exc)
        await update.message.reply_text(sanitize_provider_error(exc))
    finally:
        db.close()


async def cmd_sektor_listesi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    mappings = list_sector_mappings()
    if not mappings:
        await update.message.reply_text("Henüz sektör eşleştirmesi yok.")
        return
    lines = [f"• {sym}: {info['sector_name']} ({info['sector_index']})" for sym, info in sorted(mappings.items())]
    await update.message.reply_text("Sektör eşleştirmeleri:\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Tarama komutlari
# ---------------------------------------------------------------------------


async def cmd_tara(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    settings = get_settings()
    db = _get_db()
    try:
        user = _current_user(db, update)
        if await _check_kill_switch(update, user):
            return

        provider = build_market_data_provider(settings)
        symbols = get_distinct_watchlist_symbols(db)
        if not symbols:
            await update.message.reply_text("İzleme listende hiç sembol yok. /ekle SEMBOL ile ekleyebilirsin.")
            return

        await update.message.reply_text(f"Tarama başladı: {len(symbols)} sembol...")
        try:
            summary = run_evening_scan(db, provider, settings, symbols=symbols, top_n=5)
        except ScanBlockedByKillSwitchError as exc:
            await update.message.reply_text(str(exc))
            return

        candidates = []
        for sym, outcome in summary.top_candidates:
            candidates.append({
                "symbol": sym, "score": outcome.advanced_score.total,
                "signal_type": outcome.signal.signal_type, "close": outcome.signal.extras.get("close"),
                "entry_trigger": outcome.signal.entry_trigger, "stop_price": outcome.signal.stop_price,
                "target_1": outcome.signal.target_1, "risk_reward": outcome.signal.risk_reward,
                "xu100_score": outcome.xu100_relative_strength.relative_score,
            })
        risks = []
        for sym, outcome in summary.top_risks:
            risk_reasons = [r.description for r in outcome.signal.reasons if r.is_risk]
            risks.append({
                "symbol": sym, "score": outcome.advanced_score.total,
                "broken_support": "evet" if (outcome.signal.support_resistance and outcome.signal.support_resistance.support_broken_with_volume) else "-",
                "main_reason": risk_reasons[0] if risk_reasons else "-",
            })

        text = format_evening_report(
            scan_date=datetime.now(timezone.utc).strftime("%d.%m.%Y"),
            market_regime=summary.market_regime or "veri_yetersiz",
            xu100_daily_change=None,
            symbols_scanned=summary.symbols_scanned,
            symbols_succeeded=summary.symbols_succeeded,
            symbols_failed=summary.symbols_failed,
            top_candidates=candidates,
            top_risks=risks,
            upcoming_breakouts=[f"{c['symbol']} — {c['entry_trigger']} üzeri" for c in candidates[:3] if c.get("entry_trigger")],
        )
        for part in split_long_message(text):
            await update.message.reply_text(part)

        if summary.failed_symbols:
            failed_text = "Veri alınamayan semboller:\n" + "\n".join(f"• {s}: {reason}" for s, reason in summary.failed_symbols[:10])
            await update.message.reply_text(failed_text)
    finally:
        db.close()


async def cmd_tara_liste(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    db = _get_db()
    try:
        symbols = get_distinct_watchlist_symbols(db)
        await update.message.reply_text("Tarama listesi (izleme listesi):\n" + ("\n".join(f"• {s}" for s in symbols) if symbols else "(boş)"))
    finally:
        db.close()


async def cmd_tarama_durumu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    db = _get_db()
    try:
        from app.models.database import Scan
        last_scan = db.query(Scan).order_by(Scan.started_at.desc()).first()
        if last_scan is None:
            await update.message.reply_text("Henüz hiç tarama yapılmadı. /tara ile başlatabilirsin.")
            return
        await update.message.reply_text(
            f"Son tarama: {last_scan.started_at}\n"
            f"Durum: {last_scan.status}\n"
            f"Taranan: {last_scan.symbols_scanned}  Başarılı: {last_scan.symbols_succeeded}  Başarısız: {last_scan.symbols_failed}\n"
            f"Piyasa rejimi: {last_scan.market_regime or '-'}"
        )
    finally:
        db.close()


async def cmd_aksam_raporu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_tara(update, context)


async def cmd_tarama_ayarlari(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    settings = get_settings()
    await update.message.reply_text(
        f"Tarama ayarları:\n"
        f"Kapanış tarama saati: {settings.close_scan_time}\n"
        f"Otomatik akşam raporu: {'açık' if settings.close_scan_enabled else 'kapalı'}\n"
        f"Gün içi ön analiz: {'açık' if settings.intraday_preview_enabled else 'kapalı'}\n"
        f"Değiştirmek için: /ayarlar evening_report_time 18:30"
    )


# ---------------------------------------------------------------------------
# Sinyal gecmisi ve performans
# ---------------------------------------------------------------------------


async def cmd_sinyaller(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    db = _get_db()
    try:
        user = _current_user(db, update)
        rows = (
            db.query(Signal)
            .filter(or_(Signal.user_id.is_(None), Signal.user_id == user.id))
            .order_by(Signal.created_at.desc(), Signal.id.desc())
            .limit(20)
            .all()
        )
        from app.telegram.signal_list_formatter import format_recent_signals

        await update.message.reply_text(format_recent_signals(rows))
    finally:
        db.close()


async def cmd_aktif_sinyaller(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    db = _get_db()
    try:
        user = _current_user(db, update)
        open_states = [
            SignalStateEnum.CREATED, SignalStateEnum.WAITING_TRIGGER, SignalStateEnum.CONFIRMED,
            SignalStateEnum.SENT, SignalStateEnum.ACTIVE, SignalStateEnum.TARGET_1_HIT, SignalStateEnum.TARGET_2_HIT,
            SignalStateEnum.PENDING_ENTRY, SignalStateEnum.TP1_HIT, SignalStateEnum.TP2_HIT,
            SignalStateEnum.EXIT_PENDING, SignalStateEnum.SUSPENDED,
        ]
        rows = (
            db.query(Signal)
            .filter(
                or_(Signal.user_id.is_(None), Signal.user_id == user.id),
                Signal.state.in_(open_states),
            )
            .order_by(Signal.score.desc(), Signal.id.desc())
            .limit(30)
            .all()
        )
        from app.telegram.signal_list_formatter import format_active_signals

        await update.message.reply_text(format_active_signals(rows))
    finally:
        db.close()


async def cmd_sinyal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /sinyal <id veya SEMBOL> (ör. /sinyal 123 ya da /sinyal THYAO)")
        return
    lookup = context.args[0].strip().upper()
    db = _get_db()
    try:
        user = _current_user(db, update)
        visible = or_(Signal.user_id.is_(None), Signal.user_id == user.id)
        if lookup.isdigit():
            sig = db.query(Signal).filter(Signal.id == int(lookup), visible).one_or_none()
        else:
            symbol = lookup.removesuffix(".IS")
            sig = (
                db.query(Signal)
                .filter(Signal.symbol == symbol, visible)
                .order_by(Signal.created_at.desc(), Signal.id.desc())
                .first()
            )
        if sig is None:
            await update.message.reply_text("Sinyal bulunamadı veya bu kaydı görme yetkin yok.")
            return
        state = sig.state.value if hasattr(sig.state, "value") else str(sig.state)
        signal_type = sig.signal_type.value if hasattr(sig.signal_type, "value") else str(sig.signal_type)
        planned_entry = sig.planned_entry_price or sig.entry_trigger
        entry_text = f"{float(planned_entry):.2f} TL" if planned_entry is not None else "yok"
        actual_text = f"{float(sig.actual_entry_price):.2f} TL" if sig.actual_entry_price is not None else "henüz yok"
        stop = sig.current_stop_price or sig.stop_price
        stop_text = f"{float(stop):.2f} TL" if stop is not None else "yok"
        quantity = f"{float(sig.requested_quantity):.0f} lot" if sig.requested_quantity is not None else "belirlenmedi"
        remaining = f"{float(sig.remaining_quantity):.0f} lot" if sig.remaining_quantity is not None else "-"
        event_rows = (
            db.query(SignalEvent)
            .filter(SignalEvent.signal_id == sig.id)
            .order_by(SignalEvent.created_at.desc(), SignalEvent.id.desc())
            .limit(8)
            .all()
        )
        event_lines = []
        for event in reversed(event_rows):
            stamp = event.candle_open_time or event.trading_date or event.created_at
            stamp = _istanbul_time(stamp)
            label = event.event_type or event.to_state
            price = event.execution_price or event.price_at_event
            price_suffix = f" • {float(price):.2f} TL" if price is not None else ""
            event_lines.append(f"• {stamp:%d.%m.%Y %H:%M} • {label}{price_suffix}")
        history = "\n".join(event_lines) or "• Henüz yaşam döngüsü olayı yok"
        await update.message.reply_text(
            f"📌 #{sig.id} {sig.symbol} — {signal_type}\n"
            f"Durum: {state} • Skor: {sig.score:.0f}/100\n"
            f"Planlanan giriş: {entry_text}\nGerçekleşen giriş: {actual_text}\n"
            f"Stop: {stop_text}\nTP1: {sig.target_1} • TP2: {sig.target_2} • TP3: {sig.target_3}\n"
            f"Planlanan miktar: {quantity} • Kalan: {remaining}\n"
            f"Risk/Getiri: {sig.risk_reward or '-'}\n"
            f"Veri: {sig.provider} • {_istanbul_time(sig.data_timestamp):%d.%m.%Y %H:%M} (İstanbul)\n"
            f"Kaynak: {sig.source or 'analiz motoru'}\n\n"
            f"🧾 OLAY GEÇMİŞİ\n{history}\n\n"
            "Takip: /takip <id> • Bırak: /takip_birak <id> • Replay: /backtest_signal <id>"
        )
    finally:
        db.close()


async def cmd_sinyal_gecmisi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /sinyal_gecmisi SEMBOL")
        return
    symbol = context.args[0].strip().upper()
    db = _get_db()
    try:
        user = _current_user(db, update)
        rows = (
            db.query(Signal)
            .filter(
                Signal.symbol == symbol,
                or_(Signal.user_id.is_(None), Signal.user_id == user.id),
            )
            .order_by(Signal.created_at.desc(), Signal.id.desc())
            .limit(10)
            .all()
        )
        if not rows:
            await update.message.reply_text(f"'{symbol}' için geçmiş sinyal bulunamadı.")
            return
        lines = [f"• #{s.id} {s.created_at.strftime('%d.%m.%Y')}: {s.signal_type.value} (skor {s.score:.0f}) → {s.state.value}" for s in rows]
        await update.message.reply_text(f"{symbol} sinyal geçmişi:\n" + "\n".join(lines))
    finally:
        db.close()


async def cmd_performans(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    period_days = 90
    if context.args:
        try:
            period_days = int(context.args[0])
        except ValueError:
            pass
    settings = get_settings()
    db = _get_db()
    try:
        report = compute_performance_report(db, period_days=period_days, minimum_sample_size=settings.performance_minimum_sample_size)
        await update.message.reply_text(format_performance_report(report))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Portfoy genisletmeleri
# ---------------------------------------------------------------------------


async def cmd_pozisyon_ekle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text("Kullanim: /pozisyon_ekle SEMBOL LOT MALIYET (orn: /pozisyon_ekle SVGYO 1000 15.20)")
        return
    db = _get_db()
    try:
        user = _current_user(db, update)
        symbol, lot_str, cost_str = context.args[0], context.args[1], context.args[2]
        position = pf_add_position(db, user, symbol, float(lot_str), float(cost_str))
        await update.message.reply_text(f"Pozisyon eklendi: {position.symbol} {position.lot} lot @ {position.average_cost}")
    except (ValueError, InvalidSymbolError) as exc:
        await update.message.reply_text(f"Hata: {exc}")
    finally:
        db.close()


async def cmd_pozisyon_sil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /pozisyon_sil SEMBOL")
        return
    db = _get_db()
    try:
        user = _current_user(db, update)
        remove_position_by_symbol(db, user, context.args[0])
        await update.message.reply_text(f"'{context.args[0].upper()}' pozisyonu silindi.")
    except ValueError as exc:
        await update.message.reply_text(f"Hata: {exc}")
    finally:
        db.close()


async def cmd_pozisyon_guncelle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text("Kullanim: /pozisyon_guncelle SEMBOL LOT MALIYET")
        return
    db = _get_db()
    try:
        user = _current_user(db, update)
        position = update_position(db, user, context.args[0], float(context.args[1]), float(context.args[2]))
        await update.message.reply_text(f"Pozisyon güncellendi: {position.symbol} {position.lot} lot @ {position.average_cost}")
    except ValueError as exc:
        await update.message.reply_text(f"Hata: {exc}")
    finally:
        db.close()


async def cmd_portfoy_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    settings = get_settings()
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        user = _current_user(db, update)
        positions = pf_list_positions(db, user)
        prices, _ = await asyncio.to_thread(
            resolve_portfolio_prices, provider, [position.symbol for position in positions],
            timezone_name=settings.timezone_name,
        )
        risk = portfolio_risk_summary(db, user, current_prices=prices)
        if risk["position_count"] == 0:
            await update.message.reply_text("Portföyünde açık pozisyon yok.")
            return
        sector_lines = "\n".join(f"  • {name}: %{pct}" for name, pct in risk["sector_exposure_percent"].items())
        await update.message.reply_text(
            f"💼 PORTFÖY RİSKİ\n\n"
            f"Toplam değer: {risk['total_value']}\n"
            f"Toplam K/Z: {risk['total_pnl']}\n"
            f"En büyük pozisyon: {risk['largest_position']['symbol'] if risk['largest_position'] else '-'}\n"
            f"Tüm stoplar çalışırsa zarar: {risk['worst_case_stop_loss']}\n\n"
            f"Sektör dağılımı:\n{sector_lines}\n\n"
            f"En yoğun sektör: {risk['max_sector_concentration'][0] if risk['max_sector_concentration'] else '-'} "
            f"(%{risk['max_sector_concentration'][1] if risk['max_sector_concentration'] else 0})"
        )
    finally:
        db.close()


async def cmd_pozisyon_boyutu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text("Kullanim: /pozisyon_boyutu SEMBOL SERMAYE RISK_YUZDESI (orn: /pozisyon_boyutu SVGYO 100000 0.75)")
        return
    symbol = context.args[0].strip().upper()
    try:
        capital = float(context.args[1])
        risk_percent = float(context.args[2])
    except ValueError:
        await update.message.reply_text("Sermaye ve risk yüzdesi sayısal olmalı.")
        return

    settings = get_settings()
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        outcome = run_symbol_analysis_v3(db, provider, symbol, settings)
        signal = outcome.signal
        if signal.stop_price is None:
            await update.message.reply_text(f"'{symbol}' için güvenilir stop hesaplanamadığından pozisyon boyutu hesaplanamıyor.")
            return

        entry = signal.extras.get("close")
        sizing = calculate_position_size(capital, risk_percent, entry, signal.stop_price)

        sector_warning = None
        if sizing.position_percent_of_capital > settings.maximum_position_percent:
            sector_warning = f"Pozisyon, maksimum tek pozisyon yüzdesini (%{settings.maximum_position_percent}) aşıyor."

        text = format_position_size_result(
            symbol, sizing, entry, signal.stop_price, signal.target_1, signal.risk_reward, sector_warning
        )
        await update.message.reply_text(text)
    except InvalidStopError as exc:
        await update.message.reply_text(f"Pozisyon boyutu hesaplanamadı: {exc}")
    except AnalysisUnavailableErrorV3 as exc:
        await update.message.reply_text(str(exc))
    finally:
        db.close()


async def cmd_maliyet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /maliyet SEMBOL")
        return
    symbol = context.args[0].strip().upper()
    db = _get_db()
    try:
        user = _current_user(db, update)
        positions = [p for p in pf_list_positions(db, user) if p.symbol == symbol]
        if not positions:
            await update.message.reply_text(f"'{symbol}' için pozisyon bulunamadı.")
            return
        pos = positions[0]
        await update.message.reply_text(f"{symbol} ortalama maliyet: {pos.average_cost} ({pos.lot} lot)")
    finally:
        db.close()


async def cmd_nakit_ayarla(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /nakit_ayarla TUTAR")
        return
    db = _get_db()
    try:
        user = _current_user(db, update)
        amount = float(context.args[0])
        set_cash_balance(db, user, amount)
        await update.message.reply_text(f"Nakit bakiyesi {amount} olarak ayarlandı.")
    except ValueError as exc:
        await update.message.reply_text(f"Hata: {exc}")
    finally:
        db.close()


async def cmd_sermaye_ayarla(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /sermaye_ayarla TUTAR")
        return
    db = _get_db()
    try:
        user = _current_user(db, update)
        amount = float(context.args[0])
        set_total_capital(db, user, amount)
        await update.message.reply_text(f"Toplam sermaye {amount} olarak ayarlandı.")
    except ValueError as exc:
        await update.message.reply_text(f"Hata: {exc}")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Alarm komutlari
# ---------------------------------------------------------------------------


async def cmd_alarm_kur(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Kullanım: /alarm_kur SEMBOL ALARM_TURU [DEĞER]\n"
            "Eski türler: ust, alt, hacim, skor, skor_altinda, sinyal, rejim, anomali\n"
            "Yeni örnekler: gunluk_destek, haftalik_direnc_kirilimi, ortak_destek, "
            "hacim_patlamasi, rsi_asiri_satim, haber_etkisi 60, xu100_guc 75, hedef 1"
        )
        return
    symbol = context.args[0]
    alert_type = context.args[1].lower()
    value_arg = context.args[2] if len(context.args) > 2 else None

    db = _get_db()
    try:
        user = _current_user(db, update)
        if alert_type not in VALID_ALERT_TYPES:
            settings = get_settings()
            rule = create_enhanced_alarm_rule(
                db,
                user,
                symbol,
                alert_type,
                list(context.args[2:]),
                cooldown_minutes=settings.enhanced_alarm_default_cooldown_minutes,
            )
            await update.message.reply_text(
                f"Alarm kuruldu: #E{rule.id} {rule.symbol} — {ALERT_LABELS[rule.alert_type]}"
            )
            return
        threshold_value = None
        threshold_text = None
        if alert_type in ("sinyal", "rejim", "anomali"):
            threshold_text = value_arg
        elif value_arg is not None:
            try:
                threshold_value = float(value_arg)
            except ValueError:
                await update.message.reply_text("Değer sayısal olmalı.")
                return

        alert = create_alert(db, user, symbol, alert_type, threshold_value, threshold_text)
        await update.message.reply_text(f"Alarm kuruldu: #{alert.id} {alert.symbol} {alert.alert_type} {value_arg or ''}")
    except (InvalidAlertError, InvalidEnhancedAlertError) as exc:
        await update.message.reply_text(f"Hata: {exc}")
    finally:
        db.close()


async def cmd_alarmlar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    db = _get_db()
    try:
        user = _current_user(db, update)
        alerts = list_alerts(db, user)
        enhanced = list_enhanced_alarm_rules(db, user)
        if not alerts and not enhanced:
            await update.message.reply_text("Kurulu alarm yok.")
            return
        lines = [f"#{a.id} {a.symbol} {a.alert_type} {a.threshold_value or a.threshold_text or ''}" for a in alerts]
        lines.extend(
            f"#E{rule.id} {rule.symbol} {ALERT_LABELS.get(rule.alert_type, rule.alert_type)} "
            f"[{'açık' if rule.is_active else 'durduruldu'}]"
            for rule in enhanced
        )
        await update.message.reply_text("Alarmların:\n" + "\n".join(lines))
    finally:
        db.close()


async def cmd_alarm_sil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /alarm_sil ID")
        return
    db = _get_db()
    try:
        user = _current_user(db, update)
        raw_id = context.args[0].strip()
        if raw_id.upper().startswith("E"):
            delete_enhanced_alarm_rule(db, user, int(raw_id[1:]))
        else:
            delete_alert(db, user, int(raw_id))
        await update.message.reply_text(f"Alarm #{context.args[0]} silindi.")
    except (ValueError, InvalidAlertError, InvalidEnhancedAlertError) as exc:
        await update.message.reply_text(f"Hata: {exc}")
    finally:
        db.close()


def _enhanced_rule_id(raw: str) -> int:
    normalized = raw.strip().upper()
    return int(normalized[1:] if normalized.startswith("E") else normalized)


async def _set_alarm_state(update: Update, context: ContextTypes.DEFAULT_TYPE, active: bool) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        command = "alarm_ac" if active else "alarm_durdur"
        await update.message.reply_text(f"Kullanım: /{command} ALARM_ID")
        return
    db = _get_db()
    try:
        user = _current_user(db, update)
        raw = context.args[0].strip()
        if raw.upper().startswith("E"):
            rule = set_enhanced_alarm_active(db, user, _enhanced_rule_id(raw), active)
            label = ALERT_LABELS.get(rule.alert_type, rule.alert_type)
        else:
            old = db.query(PriceAlert).filter(PriceAlert.id == int(raw), PriceAlert.user_id == user.id).first()
            if old is not None:
                old.is_active = active
                db.commit()
                label = old.alert_type
            else:
                rule = set_enhanced_alarm_active(db, user, int(raw), active)
                label = ALERT_LABELS.get(rule.alert_type, rule.alert_type)
        await update.message.reply_text(
            f"Alarm #{raw} {'açıldı' if active else 'durduruldu'}: {label}"
        )
    except (ValueError, InvalidEnhancedAlertError) as exc:
        await update.message.reply_text(f"Hata: {exc}")
    finally:
        db.close()


async def cmd_alarm_durdur(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_alarm_state(update, context, False)


async def cmd_alarm_ac(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_alarm_state(update, context, True)


async def cmd_alarm_detay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /alarm_detay ALARM_ID")
        return
    db = _get_db()
    try:
        user = _current_user(db, update)
        raw = context.args[0].strip()
        if not raw.upper().startswith("E"):
            old = db.query(PriceAlert).filter(PriceAlert.id == int(raw), PriceAlert.user_id == user.id).first()
            if old is not None:
                await update.message.reply_text(
                    f"Alarm #{old.id}\nSembol: {old.symbol}\nTür: {old.alert_type}\n"
                    f"Durum: {'Açık' if old.is_active else 'Durduruldu'}\n"
                    f"Cooldown: {old.cooldown_minutes} dk\nSon tetik: {old.last_triggered_at or '-'}"
                )
                return
        rule = get_enhanced_alarm_rule(db, user, _enhanced_rule_id(raw))
        latest = (
            db.query(EnhancedAlarmTriggerEvent)
            .filter(EnhancedAlarmTriggerEvent.rule_id == rule.id)
            .order_by(EnhancedAlarmTriggerEvent.triggered_at.desc())
            .first()
        )
        await update.message.reply_text(
            f"Alarm #E{rule.id}\nSembol: {rule.symbol}\n"
            f"Tür: {ALERT_LABELS.get(rule.alert_type, rule.alert_type)}\n"
            f"Zaman dilimi: {rule.timeframe or 'otomatik'}\n"
            f"Eşik: {rule.threshold_value if rule.threshold_value is not None else rule.threshold_text or '-'}\n"
            f"Durum: {'Açık' if rule.is_active else 'Durduruldu'}\n"
            f"Cooldown: {rule.cooldown_minutes} dk\n"
            f"Son tetik: {rule.last_triggered_at or '-'}\n"
            f"Son olay: {latest.message if latest and latest.message else '-'}"
        )
    except (ValueError, InvalidEnhancedAlertError) as exc:
        await update.message.reply_text(f"Hata: {exc}")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Grafikler
# ---------------------------------------------------------------------------


async def cmd_grafik(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /grafik SEMBOL [6ay|1yil]")
        return
    symbol = context.args[0].strip().upper()
    period = context.args[1] if len(context.args) > 1 else "6ay"

    from datetime import timedelta

    from app.services.chart_service import delete_chart_file, generate_professional_daily_chart, resolve_period_days

    settings = get_settings()
    provider = build_market_data_provider(settings)
    strategy_config = get_strategy_config()
    timeframe = strategy_config["timeframes"]["primary"]

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(resolve_period_days(period), 250))
    db = _get_db()
    chart_path = None
    try:
        outcome = run_symbol_analysis_v3(db, provider, symbol, settings)
        df = provider.get_ohlcv(symbol, timeframe, start, end)
        current = outcome.signal.extras.get("current_price") or float(df.iloc[-1]["close"])
        timeframe_levels = compute_timeframe_levels(df, current)
        chart_path = await asyncio.to_thread(
            generate_professional_daily_chart,
            df, symbol, timeframe_levels=timeframe_levels, entry_zone=outcome.signal.entry_zone,
            stop_price=outcome.signal.stop_price,
            targets=[outcome.signal.target_1, outcome.signal.target_2, outcome.signal.target_3],
            chart_mode="standard",
        )
        with open(chart_path, "rb") as f:
            await update.message.reply_photo(photo=f, caption=f"{symbol} fiyat grafiği ({period})")
    except AnalysisUnavailableErrorV3 as exc:
        logger.warning("Standart grafik üretilemedi symbol=%s: %s", symbol, exc)
        await update.message.reply_text(sanitize_provider_error(exc))
    finally:
        if chart_path:
            delete_chart_file(chart_path)
        db.close()


async def cmd_rs_grafik(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /rs_grafik SEMBOL")
        return
    symbol = context.args[0].strip().upper()

    from datetime import timedelta

    from app.services.chart_service import delete_chart_file, generate_relative_strength_chart

    settings = get_settings()
    provider = build_market_data_provider(settings)
    strategy_config = get_strategy_config()
    timeframe = strategy_config["timeframes"]["primary"]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)

    chart_path = None
    try:
        stock_df = provider.get_ohlcv(symbol, timeframe, start, end)
        index_df = provider.get_ohlcv(settings.xu100_symbol, timeframe, start, end)
        chart_path = await asyncio.to_thread(
            generate_relative_strength_chart, stock_df, index_df, symbol, settings.xu100_symbol
        )
        with open(chart_path, "rb") as f:
            await update.message.reply_photo(photo=f, caption=f"{symbol} vs {settings.xu100_symbol} göreceli güç")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Göreceli güç grafiği üretilemedi symbol=%s: %s", symbol, exc)
        await update.message.reply_text(sanitize_provider_error(exc))
    finally:
        if chart_path:
            delete_chart_file(chart_path)


# ---------------------------------------------------------------------------
# Piyasa genisligi
# ---------------------------------------------------------------------------


async def cmd_piyasa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    settings = get_settings()
    provider = build_market_data_provider(settings)
    await update.message.reply_text(
        "⏳ 571 hisselik BIST evreni taranıyor. Veri sağlayıcısının hızına göre biraz sürebilir…"
    )
    breadth = await asyncio.to_thread(
        compute_market_breadth,
        provider,
        settings.bist_universe_json_path,
        "1d",
        settings.universe_scan_max_symbols_per_run,
        provider_factory=lambda: build_market_data_provider(settings),
        max_workers=settings.universe_scan_workers,
        minimum_signal_score=settings.universe_scan_minimum_score,
        top_n=12,
        cache_minutes=settings.universe_scan_cache_minutes,
    )
    await update.message.reply_text(format_market_breadth(breadth))
    detail = context.args[0].casefold() if context.args else ""
    if breadth.available and detail in {"long", "short", "risk", "tum", "tüm"}:
        for message in format_breadth_candidate_messages(breadth, detail):
            await update.message.reply_text(message)


cmd_genislik = cmd_piyasa


# ---------------------------------------------------------------------------
# Ayarlar
# ---------------------------------------------------------------------------


async def cmd_ayarlar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    db = _get_db()
    try:
        user = _current_user(db, update)
        if not context.args:
            s = get_or_create_settings(db, user)
            await update.message.reply_text(
                "Mevcut ayarların:\n"
                f"minimum_signal_score: {s.minimum_signal_score}\n"
                f"minimum_risk_reward: {s.minimum_risk_reward}\n"
                f"evening_report_enabled: {s.evening_report_enabled}\n"
                f"evening_report_time: {s.evening_report_time}\n"
                f"top_candidate_count: {s.top_candidate_count}\n"
                f"intraday_preview_enabled: {s.intraday_preview_enabled}\n"
                f"chart_type: {s.chart_type}\n"
                f"maximum_open_positions: {s.maximum_open_positions}\n"
                f"maximum_sector_exposure_percent: {s.maximum_sector_exposure_percent}\n\n"
                "Değiştirmek için: /ayarlar ANAHTAR DEGER (örn: /ayarlar minimum_signal_score 70)"
            )
            return
        if len(context.args) < 2:
            await update.message.reply_text("Kullanim: /ayarlar ANAHTAR DEGER")
            return
        key, value = context.args[0], " ".join(context.args[1:])
        update_setting(db, user, key, value)
        await update.message.reply_text(f"Ayar güncellendi: {key} = {value}")
    except InvalidSettingError as exc:
        await update.message.reply_text(f"Hata: {exc}")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# MERGEN QUANT — Aşama 5e
# ---------------------------------------------------------------------------


async def _stage5e_context(provider, symbol: str, settings):
    return await asyncio.to_thread(build_stage5e_analysis_context, provider, symbol, settings)


def _parse_positive_target(raw: str) -> float:
    try:
        value = float(raw.replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise ValueError("Hedef fiyat sayısal olmalı.") from exc
    if value <= 0:
        raise ValueError("Hedef fiyat sıfırdan büyük olmalı.")
    return round(value, 2)


def _persist_long_term_scenarios(db, stage_context) -> int:
    saved = 0
    for zone in stage_context.long_term_scenarios.all_scenarios():
        exists = (
            db.query(LongTermScenario)
            .filter(
                LongTermScenario.symbol == stage_context.symbol,
                LongTermScenario.scenario_class == zone.scenario_class,
                LongTermScenario.price_low == zone.low,
                LongTermScenario.price_high == zone.high,
                LongTermScenario.data_timestamp == stage_context.data_timestamp,
            )
            .first()
        )
        if exists is not None:
            continue
        db.add(
            LongTermScenario(
                symbol=stage_context.symbol, direction=zone.direction,
                scenario_class=zone.scenario_class, price_low=zone.low,
                price_high=zone.high, price_mid=zone.mid,
                required_change_percent=zone.required_change_percent,
                required_price_multiple=zone.required_price_multiple,
                confidence=zone.confidence, time_horizon=zone.time_horizon,
                activation_json=json.dumps(zone.activation_conditions, ensure_ascii=False),
                invalidation_json=json.dumps(zone.invalidation_conditions, ensure_ascii=False),
                evidence_json=json.dumps(zone.evidence, ensure_ascii=False),
                fundamental_support=zone.fundamental_support,
                speculation_risk=zone.speculation_risk,
                data_timestamp=stage_context.data_timestamp,
            )
        )
        saved += 1
    if saved:
        db.commit()

    # Botun yükseliş hedefleri duplicate-korumalı takip tablosuna yazılır.
    for zone in stage_context.long_term_scenarios.all_scenarios():
        if zone.direction != "yükseliş":
            continue
        invalidation = zone.supports_to_hold[-1] if zone.supports_to_hold else None
        save_target_tracking(
            db, symbol=stage_context.symbol,
            current_price=stage_context.current_price.current_price,
            target_low=zone.low, target_high=zone.high,
            target_type=zone.scenario_class, time_horizon=zone.time_horizon,
            confidence=zone.confidence, technical_reasons=zone.evidence,
            fundamental_status=zone.fundamental_support,
            invalidation_level=invalidation,
            data_timestamp=stage_context.data_timestamp,
        )
    return saved


def _valuation_for_context(stage_context, settings):
    if getattr(stage_context, "valuation", None) is not None:
        return stage_context.valuation
    fundamental_provider = build_fundamental_provider(settings)
    payload = collect_fundamental_payload(fundamental_provider, stage_context.symbol)
    sector_info = get_sector_info(stage_context.symbol)
    return evaluate_gyo_valuation(
        stage_context.symbol,
        stage_context.current_price.current_price,
        payload,
        sector_name=sector_info.sector_name if sector_info else None,
    )


def _persist_valuation(db, result) -> None:
    period = (
        datetime(result.financial_period_date.year, result.financial_period_date.month, result.financial_period_date.day, tzinfo=timezone.utc)
        if result.financial_period_date else None
    )
    db.add(
        ValuationSnapshot(
            symbol=result.symbol, valuation_type="GYO" if result.applicable else "GENEL",
            classification=result.classification, market_cap=result.current_market_cap,
            shares_outstanding=result.shares_outstanding, net_asset_value=result.net_asset_value,
            nav_per_share=result.nav_per_share, market_cap_to_nav=result.market_cap_to_nav,
            discount_premium_percent=result.nav_discount_premium_percent,
            financial_period_date=period, data_is_stale=result.data_is_stale,
            payload_json=json.dumps(result.__dict__, ensure_ascii=False, default=str),
        )
    )
    db.commit()


def _persist_corporate_actions(db, events) -> None:
    for event in events:
        effective = (
            datetime(event.effective_date.year, event.effective_date.month, event.effective_date.day, tzinfo=timezone.utc)
            if event.effective_date else None
        )
        exists = (
            db.query(CorporateActionRecord)
            .filter(
                CorporateActionRecord.symbol == event.symbol,
                CorporateActionRecord.corporate_action_type == event.corporate_action_type,
                CorporateActionRecord.effective_date == effective,
            )
            .first()
        )
        if exists:
            continue
        db.add(
            CorporateActionRecord(
                symbol=event.symbol, corporate_action_type=event.corporate_action_type,
                effective_date=effective, raw_price=event.raw_price,
                adjusted_price=event.adjusted_price, adjustment_factor=event.adjustment_factor,
                cash_amount=event.cash_amount, share_ratio=event.share_ratio,
                old_share_count=event.old_share_count, new_share_count=event.new_share_count,
                source=event.source,
                payload_json=json.dumps(event.__dict__, ensure_ascii=False, default=str),
            )
        )
    db.commit()


def _long_term_keyboard(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Boğa Detayı", callback_data=f"stage5f_bull_{symbol}"),
                InlineKeyboardButton("Ayı Detayı", callback_data=f"stage5f_bear_{symbol}"),
            ],
            [
                InlineKeyboardButton("Hedef Yolu", callback_data=f"menu_stage5e_yol_{symbol}"),
                InlineKeyboardButton("Değerleme", callback_data=f"menu_stage5e_degerleme_{symbol}"),
            ],
            [
                InlineKeyboardButton("Uzun Grafik", callback_data=f"menu_stage5e_uzungrafik_{symbol}"),
                InlineKeyboardButton("Veri Kaynakları", callback_data=f"menu_stage5e_data_{symbol}"),
            ],
        ]
    )


def _target_keyboard(symbol: str, target: float) -> InlineKeyboardMarkup:
    encoded = f"{target:.2f}"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Teknik Detay", callback_data=f"stage5f_tdetail_{symbol}_{encoded}"),
                InlineKeyboardButton("Değerleme Detayı", callback_data=f"stage5f_tvalue_{symbol}_{encoded}"),
            ],
            [
                InlineKeyboardButton("Hedef Yolu", callback_data=f"stage5f_troad_{symbol}_{encoded}"),
                InlineKeyboardButton("Uzun Grafik", callback_data=f"stage5f_tchart_{symbol}_{encoded}"),
            ],
        ]
    )


async def handle_stage5f_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("_")
    if len(parts) < 3:
        await query.message.reply_text("İşlem bilgisi eksik.")
        return
    action, symbol = parts[1], parts[2].upper()
    settings = get_settings()
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        if action in {"bull", "bear"}:
            stage_context = await _stage5e_context(provider, symbol, settings)
            text = format_long_term_scenario_detail(
                symbol,
                stage_context.current_price,
                stage_context.long_term_scenarios,
                "boğa" if action == "bull" else "ayı",
            )
            for chunk in split_long_message(text):
                await query.message.reply_text(chunk)
            return

        if len(parts) < 4:
            await query.message.reply_text("Hedef bilgisi eksik.")
            return
        target = _parse_positive_target(parts[3])
        if action == "tvalue":
            await query.message.reply_text(f"Değerleme ayrıntısı için /degerleme {symbol} yazabilirsin.")
            return
        if action == "tchart":
            await query.message.reply_text(f"Uzun grafik için /uzungrafik {symbol} {target:.2f} yazabilirsin.")
            return

        stage_context = await _stage5e_context(provider, symbol, settings)
        evaluation = evaluate_user_target(
            symbol,
            stage_context.current_price.current_price,
            target,
            intermediate_levels=stage_context.intermediate_levels(),
            support_levels=stage_context.support_levels(),
            valuation_class=stage_context.valuation.classification,
            market_support=stage_context.market_regime.regime,
            shares_outstanding=stage_context.valuation.shares_outstanding,
            current_market_cap=stage_context.valuation.current_market_cap,
            liquidity_score=stage_context.liquidity.score if stage_context.liquidity.available else None,
            relative_strength=(
                stage_context.xu100_relative_strength.relative_score
                if stage_context.xu100_relative_strength.available else None
            ),
            fundamental_available=stage_context.valuation.classification not in {"Veri yetersiz", "Uygulanamaz"},
        )
        if action == "troad":
            text = format_target_roadmap(symbol, evaluation.roadmap, stage_context.current_price, stage_context.data_quality)
        else:
            close_conditions = "\n".join(f"- {item}" for item in evaluation.required_close_conditions)
            invalidations = "\n".join(f"- {item}" for item in evaluation.invalidation_conditions)
            text = (
                f"🎯 {symbol} — KULLANICI HEDEFİ TEKNİK DETAY\n"
                f"Hedef: {target:.2f} TL\nKanıt gücü: {evaluation.evidence_strength:.0f}/100\n\n"
                f"Güçlenme koşulları:\n{close_conditions}\n\nGeçersizlik koşulları:\n{invalidations}\n\n"
                "Bu kullanıcı hedefi bot hedefi veya AL/SAT sinyali değildir."
            )
        for chunk in split_long_message(text):
            await query.message.reply_text(chunk)
    except (Stage5EContextUnavailableError, ValueError) as exc:
        logger.warning("Aşama 5f callback tamamlanamadı action=%s symbol=%s: %s", action, symbol, exc)
        await query.message.reply_text(sanitize_provider_error(exc))
    finally:
        db.close()


async def cmd_uzunsenaryo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /uzunsenaryo SEMBOL")
        return
    symbol = context.args[0].upper()
    settings = get_settings(); provider = build_market_data_provider(settings); db = _get_db()
    try:
        stage_context = await _stage5e_context(provider, symbol, settings)
        _persist_long_term_scenarios(db, stage_context)
        text = format_long_term_scenarios(
            symbol, stage_context.current_price, stage_context.long_term_scenarios,
            stage_context.data_quality,
        )
        parts = split_long_message(text)
        for index, part in enumerate(parts):
            await update.message.reply_text(
                part,
                reply_markup=_long_term_keyboard(symbol) if index == len(parts) - 1 else None,
            )
    except Stage5EContextUnavailableError as exc:
        logger.warning("Uzun senaryo üretilemedi symbol=%s: %s", symbol, exc)
        await update.message.reply_text(sanitize_provider_error(exc))
    finally:
        db.close()


async def cmd_hedefkontrol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Kullanım: /hedefkontrol SEMBOL FİYAT")
        return
    symbol = context.args[0].upper()
    try:
        target = _parse_positive_target(context.args[1])
    except ValueError as exc:
        await update.message.reply_text(str(exc)); return
    settings = get_settings(); provider = build_market_data_provider(settings); db = _get_db()
    try:
        stage_context = await _stage5e_context(provider, symbol, settings)
        valuation = await asyncio.to_thread(_valuation_for_context, stage_context, settings)
        liquidity = stage_context.liquidity
        evaluation = evaluate_user_target(
            symbol, stage_context.current_price.current_price, target,
            intermediate_levels=stage_context.intermediate_levels(),
            support_levels=stage_context.support_levels(),
            valuation_class=valuation.classification,
            market_support=stage_context.market_regime.regime,
            shares_outstanding=valuation.shares_outstanding,
            current_market_cap=valuation.current_market_cap,
            liquidity_score=liquidity.score if liquidity.available else None,
            average_daily_turnover=liquidity.avg_turnover_20d_try,
            atr_percent=liquidity.atr_percent,
            fundamental_available=valuation.classification not in {"Veri yetersiz", "Uygulanamaz"},
            nav=valuation.net_asset_value,
            relative_strength=(
                stage_context.xu100_relative_strength.relative_score
                if stage_context.xu100_relative_strength.available else None
            ),
        )
        user = _current_user(db, update)
        db.add(
            UserPriceTarget(
                user_id=user.id, symbol=symbol, target_price=target,
                current_price=evaluation.current_price,
                required_change_percent=evaluation.realism.required_change_percent,
                required_price_multiple=evaluation.realism.required_price_multiple,
                technical_class=evaluation.technical_class,
                fundamental_class=evaluation.fundamental_valuation_class,
                risk_class=evaluation.risk_class,
                realism_score=evaluation.realism.realism_score,
                data_timestamp=stage_context.data_timestamp,
            )
        )
        db.add(
            TargetRealismSnapshot(
                symbol=symbol, current_price=evaluation.current_price, target_price=target,
                current_market_cap=evaluation.realism.current_market_cap,
                target_market_cap=evaluation.realism.target_market_cap,
                technical_class=evaluation.realism.technical_probability_class,
                fundamental_class=evaluation.realism.fundamental_support_class,
                liquidity_risk=evaluation.realism.liquidity_risk,
                valuation_risk=evaluation.realism.valuation_risk,
                speculation_risk=evaluation.realism.speculation_risk,
                manipulation_indicator=evaluation.realism.manipulation_indicator,
                realism_score=evaluation.realism.realism_score,
                payload_json=json.dumps(evaluation.realism.__dict__, ensure_ascii=False, default=str),
            )
        )
        db.commit()
        text = format_user_target_check(evaluation, stage_context.current_price, stage_context.data_quality)
        parts = split_long_message(text)
        for index, part in enumerate(parts):
            await update.message.reply_text(
                part,
                reply_markup=_target_keyboard(symbol, target) if index == len(parts) - 1 else None,
            )
    except Stage5EContextUnavailableError as exc:
        logger.warning("Hedef kontrolü üretilemedi symbol=%s: %s", symbol, exc)
        await update.message.reply_text(sanitize_provider_error(exc))
    finally:
        db.close()


async def cmd_hedefyolu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /hedefyolu SEMBOL [FİYAT]")
        return
    symbol = context.args[0].upper(); user_target = None
    if len(context.args) > 1:
        try:
            user_target = _parse_positive_target(context.args[1])
        except ValueError as exc:
            await update.message.reply_text(str(exc)); return
    settings = get_settings(); provider = build_market_data_provider(settings); db = _get_db()
    try:
        stage_context = await _stage5e_context(provider, symbol, settings)
        main_zone = (
            stage_context.long_term_scenarios.long_term_main_target
            or stage_context.long_term_scenarios.strong_bull
            or stage_context.long_term_scenarios.medium_term_target
            or stage_context.long_term_scenarios.short_term_target
        )
        target = user_target or (main_zone.mid if main_zone else None)
        if target is None:
            await update.message.reply_text("Teknik hedef yolu için yeterli kanıt bulunamadı."); return
        roadmap = build_target_roadmap(
            stage_context.current_price.current_price, target,
            intermediate_levels=stage_context.intermediate_levels(),
            support_levels=stage_context.support_levels(),
            volume_available=stage_context.liquidity.available,
            target_source="kullanıcı hedefi" if user_target is not None else "motor ana hedefi",
        )
        if user_target is None and roadmap.reliable:
            record, _ = save_target_tracking(
                db, symbol=symbol, current_price=stage_context.current_price.current_price,
                target_low=roadmap.steps[-1].price_low, target_high=roadmap.steps[-1].price_high,
                target_type="Hedef yol haritası ana hedefi",
                time_horizon=roadmap.steps[-1].estimated_duration,
                confidence=roadmap.steps[-1].confidence,
                technical_reasons=roadmap.steps[-1].evidence,
                fundamental_status="Veri yetersiz",
                invalidation_level=roadmap.steps[0].invalidation_level,
                data_timestamp=stage_context.data_timestamp,
            )
            persist_roadmap_steps(db, record, roadmap)
        text = format_target_roadmap(symbol, roadmap, stage_context.current_price, stage_context.data_quality)
        for part in split_long_message(text):
            await update.message.reply_text(part)
    except Stage5EContextUnavailableError as exc:
        logger.warning("Hedef yolu üretilemedi symbol=%s: %s", symbol, exc)
        await update.message.reply_text(sanitize_provider_error(exc))
    finally:
        db.close()


async def cmd_degerleme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update): return
    if not context.args:
        await update.message.reply_text("Kullanım: /degerleme SEMBOL"); return
    symbol = context.args[0].upper(); settings = get_settings()
    provider = build_market_data_provider(settings); db = _get_db()
    try:
        stage_context = await _stage5e_context(provider, symbol, settings)
        result = await asyncio.to_thread(_valuation_for_context, stage_context, settings)
        _persist_valuation(db, result)
        for part in split_long_message(format_valuation(result, stage_context.current_price, stage_context.data_quality)):
            await update.message.reply_text(part)
    except Stage5EContextUnavailableError as exc:
        logger.warning("Değerleme üretilemedi symbol=%s: %s", symbol, exc)
        await update.message.reply_text(sanitize_provider_error(exc))
    finally:
        db.close()


async def cmd_sermaye_islemleri(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update): return
    if not context.args:
        await update.message.reply_text("Kullanım: /sermaye_islemleri SEMBOL"); return
    symbol = context.args[0].upper(); settings = get_settings()
    provider = build_market_data_provider(settings); db = _get_db()
    try:
        stage_context = await _stage5e_context(provider, symbol, settings)
        _persist_corporate_actions(db, stage_context.corporate_actions)
        text = format_corporate_actions(
            symbol, stage_context.corporate_actions,
            stage_context.current_price, stage_context.data_quality,
        )
        await update.message.reply_text(text)
    except Stage5EContextUnavailableError as exc:
        await update.message.reply_text(str(exc))
    finally:
        db.close()


async def cmd_hedefgecmisi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update): return
    if not context.args:
        await update.message.reply_text("Kullanım: /hedefgecmisi SEMBOL"); return
    db = _get_db()
    try:
        symbol = context.args[0].upper()
        await update.message.reply_text(format_target_history(symbol, list_target_history(db, symbol)))
    finally:
        db.close()


async def cmd_hedefbasari(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update): return
    symbol = context.args[0].upper() if context.args else None
    db = _get_db()
    try:
        await update.message.reply_text(format_target_performance_stage5e(compute_target_performance(db, symbol)))
    finally:
        db.close()


async def cmd_uzungrafik(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update): return
    if not context.args:
        await update.message.reply_text("Kullanım: /uzungrafik SEMBOL [FİYAT]"); return
    symbol = context.args[0].upper(); target = None
    if len(context.args) > 1:
        try:
            target = _parse_positive_target(context.args[1])
        except ValueError as exc:
            await update.message.reply_text(str(exc)); return
    settings = get_settings(); provider = build_market_data_provider(settings); db = _get_db()
    chart_paths: list[str] = []
    try:
        stage_context = await _stage5e_context(provider, symbol, settings)
        main_zone = stage_context.long_term_scenarios.long_term_main_target or stage_context.long_term_scenarios.medium_term_target
        chart_target = target or (main_zone.mid if main_zone else None)
        roadmap = build_target_roadmap(
            stage_context.current_price.current_price, chart_target,
            intermediate_levels=stage_context.intermediate_levels(),
            support_levels=stage_context.support_levels(),
            volume_available=stage_context.liquidity.available,
            target_source="kullanıcı hedefi" if target is not None else "motor ana hedefi",
        ) if chart_target else None
        valuation = await asyncio.to_thread(_valuation_for_context, stage_context, settings)
        from app.services.chart_service import delete_chart_file, generate_long_term_chart
        for timeframe, caption in (("weekly", "haftalık"), ("monthly", "aylık")):
            try:
                path = await asyncio.to_thread(
                    generate_long_term_chart,
                    stage_context.completed_daily_df, symbol,
                    timeframe=timeframe,
                    current_price=stage_context.current_price.current_price,
                    user_target=target,
                    roadmap=roadmap,
                    long_term_scenario=stage_context.long_term_scenarios,
                    corporate_actions=stage_context.corporate_actions,
                    valuation_status=valuation.classification,
                    speculation_risk=("Yüksek" if target and target / stage_context.current_price.current_price >= 3 else "Orta"),
                )
                chart_paths.append(path)
                with open(path, "rb") as chart_file:
                    await update.message.reply_photo(chart_file, caption=f"{symbol} {caption} logaritmik uzun vadeli grafik")
            except Exception as exc:  # noqa: BLE001 - metin her durumda devam eder
                logger.warning("Uzun grafik üretilemedi symbol=%s timeframe=%s: %s", symbol, timeframe, exc)
                await update.message.reply_text(f"⚠️ {caption.title()} grafik üretilemedi; metin analizi devam ediyor.")
        if roadmap:
            for part in split_long_message(format_target_roadmap(symbol, roadmap, stage_context.current_price, stage_context.data_quality)):
                await update.message.reply_text(part)
        else:
            await update.message.reply_text("Uzun grafik üretildi; hedef yol haritası için teknik veri yetersiz.")
    except Stage5EContextUnavailableError as exc:
        logger.warning("Uzun grafik bağlamı üretilemedi symbol=%s: %s", symbol, exc)
        await update.message.reply_text(sanitize_provider_error(exc))
    finally:
        try:
            from app.services.chart_service import delete_chart_file
            for path in chart_paths:
                delete_chart_file(path)
        finally:
            db.close()


# UTF-8 uyumlu, sade ana panel. Modül sonunda tanımlanarak eski sürümün yerini alır.
async def cmd_start_v3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    keyboard = [
        [InlineKeyboardButton("📊 Teknik Analiz", callback_data="menu_analiz"),
         InlineKeyboardButton("🎯 AL / SAT Planı", callback_data="menu_islemplani")],
        [InlineKeyboardButton("📚 Tüm Hisseler", callback_data="menu_all_stocks"),
         InlineKeyboardButton("🏆 En İyi 50", callback_data="menu_best50")],
        [InlineKeyboardButton("🌅 Sabah Raporu", callback_data="menu_morning_report"),
         InlineKeyboardButton("🌙 Kapanış Raporu", callback_data="menu_smxm_evening")],
        [InlineKeyboardButton("🏢 Şirket Analizi", callback_data="menu_fundamental_prompt"),
         InlineKeyboardButton("📣 KAP Bildirimleri", callback_data="menu_kap_prompt")],
        [InlineKeyboardButton("🔔 Alarm Kur", callback_data="menu_alarm_prompt"),
         InlineKeyboardButton("🔊 Alarmı Dene", callback_data="menu_alarm_test")],
        [InlineKeyboardButton("📋 Alarmlarım", callback_data="menu_alarm_list"),
         InlineKeyboardButton("🎯 Aktif Sinyaller", callback_data="menu_sinyaller")],
        [InlineKeyboardButton("⭐ İzleme Listem", callback_data="menu_liste"),
         InlineKeyboardButton("🔎 Piyasa Tara", callback_data="menu_tara")],
        [InlineKeyboardButton("💼 Portföy", callback_data="menu_portfoy"),
         InlineKeyboardButton("🌍 Piyasa Özeti", callback_data="menu_piyasa")],
        [InlineKeyboardButton("🧪 Backtest", callback_data="menu_backtest_prompt"),
         InlineKeyboardButton("📈 Test Sonuçları", callback_data="menu_backtest_summary")],
        [InlineKeyboardButton("📚 Komut Rehberi", callback_data="menu_commands"),
         InlineKeyboardButton("⚙️ Ayarlar", callback_data="menu_ayarlar")],
    ]
    await update.message.reply_text(
        "🏔️✨ MONTANA FİNANS ROBOTU HİSSE BOT ✨📈\n"
        "BIST Teknik • Temel Analiz • Alarm Asistanı\n\n"
        "🧭 Hızlı başlangıç:\n"
        "• /analiz THYAO — teknik + 5dk/15dk/1s/4s\n"
        "• /islemplani THYAO — AL/SAT, stop ve TP1–TP5\n"
        "• /sirket THYAO — şirket ve bilanço analizi\n"
        "• /alarm 9.20 THYAO — fiyat alarmı\n\n"
        "📚 Bütün seçenekler: /komutlar\n"
        "ℹ️ Çıktılar teknik senaryodur; yatırım tavsiyesi değildir.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def _analysis_action_keyboard(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗺️ İşlem Planı", callback_data=f"menu_plan_{symbol}"),
         InlineKeyboardButton("📋 Detaylı Analiz", callback_data=f"detay_{symbol}")],
        [InlineKeyboardButton("🧱 Destek / Direnç", callback_data=f"menu_stage5e_seviyeler_{symbol}"),
         InlineKeyboardButton("⏱️ Çoklu Zaman", callback_data=f"menu_stage5e_coklu_{symbol}")],
        [InlineKeyboardButton("🏢 Temel Analiz", callback_data=f"menu_fundamental_{symbol}"),
         InlineKeyboardButton("📣 KAP Bildirimleri", callback_data=f"menu_kap_{symbol}")],
        [InlineKeyboardButton("🧪 2 Yıllık Backtest", callback_data=f"menu_backtest_{symbol}"),
         InlineKeyboardButton("📈 Backtest Sonuçları", callback_data="menu_backtest_summary")],
        [InlineKeyboardButton("Standart Grafik", callback_data=f"menu_stage5e_grafik_{symbol}"),
         InlineKeyboardButton("Detaylı Grafik", callback_data=f"menu_stage5e_detaygrafik_{symbol}")],
        [InlineKeyboardButton("🔔 Alarm Kur", callback_data=f"menu_stage5e_alarm_{symbol}"),
         InlineKeyboardButton("💼 Portföye Ekle", callback_data=f"menu_stage5e_portfoy_{symbol}")],
    ])


async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ana panel butonlarını sade komut yönlendirmelerine çevirir."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    direct = {
        "menu_analiz": "Bir sembol yaz: /analiz THYAO",
        "menu_islemplani": "Bir sembol yaz: /islemplani THYAO",
        "menu_all_stocks": "PDF'den aktarılan 571 kodu görmek için /tum_hisseler yaz.",
        "menu_best50": "Tüm evrende en kaliteli giriş bölgelerini taramak için /eniyi50 yaz.",
        "menu_morning_report": "09:00 raporunu şimdi üretmek için /sabah_raporu yaz.",
        "menu_smxm_evening": "21:00 kapanış raporunu şimdi üretmek için /smxm_aksam_raporu yaz.",
        "menu_fundamental_prompt": "Şirketi bilanço, borç, kârlılık ve riskleriyle incelemek için: /sirket THYAO",
        "menu_kap_prompt": "Son resmî şirket bildirimleri için: /kap THYAO",
        "menu_alarm_prompt": "Fiyat alarmı örneği: /alarm 9.20 THYAO",
        "menu_alarm_test": "Alarm sesini denemek için: /alarm_test radar",
        "menu_alarm_list": "Açık ve tetiklenen alarmların için: /alarmlar",
        "menu_commands": "Tüm özellikleri açıklayan rehber için /komutlar yaz.",
        "menu_backtest_prompt": "Bir hissenin son iki yılını test etmek için: /backtest THYAO",
        "menu_backtest_summary": "Son backtest sonuçları için /backtest_ozet yaz.",
        "menu_liste": "İzleme listen için /liste yaz.",
        "menu_tara": "Piyasa taraması için /tara yaz.",
        "menu_portfoy": "Portföyün için /portfoy yaz.",
        "menu_sinyaller": "Aktif sinyaller için /aktif_sinyaller yaz.",
        "menu_piyasa": "571 hisse piyasa özeti: /piyasa • tüm adaylar: /piyasa tum • yalnız long/short: /piyasa long veya /piyasa short",
        "menu_ayarlar": "Ayarların için /ayarlar yaz.",
    }
    if data in direct:
        await query.message.reply_text(direct[data])
        return
    if data.startswith("menu_plan_"):
        await query.message.reply_text(f"İşlem planı için /islemplani {data.removeprefix('menu_plan_')} yaz.")
        return
    if data.startswith("menu_fundamental_"):
        await query.message.reply_text(f"Temel analiz için /sirket {data.removeprefix('menu_fundamental_')} yaz.")
        return
    if data.startswith("menu_kap_"):
        await query.message.reply_text(f"KAP bildirimleri için /kap {data.removeprefix('menu_kap_')} yaz.")
        return
    if data.startswith("menu_backtest_") and data != "menu_backtest_summary":
        await query.message.reply_text(
            f"🧪 İki yıllık geçmiş testi başlatmak için /backtest {data.removeprefix('menu_backtest_')} yaz. "
            "Komisyon, spread ve fiyat kayması hesaba katılır."
        )
        return
    if data.startswith("menu_stage5e_"):
        _, _, action, symbol = data.split("_", 3)
        commands = {
            "coklu": "cokluzaman", "seviyeler": "seviyeler", "kisa": "senaryo",
            "uzun": "uzunsenaryo", "grafik": "grafik", "detaygrafik": "analiz_detay",
        }
        if action in commands:
            await query.message.reply_text(f"/{commands[action]} {symbol}")
        elif action == "alarm":
            await query.message.reply_text(f"Alarm örneği: /alarm_kur {symbol} ust 100")
        elif action == "portfoy":
            await query.message.reply_text(f"Portföy örneği: /pozisyon_ekle {symbol} 100 50.00")
        else:
            await query.message.reply_text("Bu bölüm için /yardim menüsünü kullanabilirsin.")
        return
    await query.message.reply_text("Bu seçenek bulunamadı. /yardim yazabilirsin.")


async def cmd_alarm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Örnek: /alarm 9.20 THYAO ASELS TUPRS ses=zil"""
    if await _reject_unauthorized(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "🔔 Basit fiyat alarmı\n\n"
            "Tek hisse: /alarm 9.20 THYAO\n"
            "Çoklu: /alarm 9.20 THYAO ASELS TUPRS\n"
            "Ses: ses=zil, ses=radar veya ses=acil\n"
            "Tek seferde en fazla 60 hisse eklenebilir."
        )
        return
    try:
        target = float(context.args[0].replace(",", "."))
        if target <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Fiyat geçerli ve sıfırdan büyük olmalı. Örnek: /alarm 9.20 THYAO")
        return
    from app.services.alarm_sound_service import normalize_sound
    sound_arg = next((item.split("=", 1)[1] for item in context.args[1:] if item.casefold().startswith("ses=")), "zil")
    sound = normalize_sound(sound_arg)
    symbols = [item.strip().upper().removesuffix(".IS") for item in context.args[1:] if not item.casefold().startswith("ses=")]
    symbols = list(dict.fromkeys(symbols))
    if not symbols or len(symbols) > 60 or any(not symbol.isalnum() or len(symbol) > 12 for symbol in symbols):
        await update.message.reply_text("1–60 arasında geçerli BIST sembolü girmelisin.")
        return
    db = _get_db()
    try:
        user = _current_user(db, update)
        created = [create_alert(db, user, symbol, "fiyat", target, sound, cooldown_minutes=1440) for symbol in symbols]
        preview = ", ".join(alert.symbol for alert in created[:12])
        suffix = f" ve {len(created) - 12} hisse daha" if len(created) > 12 else ""
        await update.message.reply_text(
            f"✅ {len(created)} alarm kuruldu\n"
            f"Hedef: {target:.2f} TL\nSes: {sound}\n"
            f"Hisseler: {preview}{suffix}\n\n"
            "Her hisse hedefe geldiğinde bağımsız bildirim ve ses gönderilir."
        )
    finally:
        db.close()


async def cmd_alarm_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    from app.services.alarm_sound_service import normalize_sound, send_alarm
    sound = normalize_sound(context.args[0] if context.args else "zil")
    await send_alarm(update.get_bot(), update.effective_chat.id, f"🔔 Alarm testi başarılı • Ses: {sound}", sound)


async def cmd_sirket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /sirket THYAO")
        return
    from app.fundamentals import FundamentalDataError, build_fundamental_provider
    from app.services.company_analysis_service import analyze_company, format_company_analysis
    try:
        fundamental_provider = build_fundamental_provider(get_settings())
        result = await asyncio.to_thread(
            analyze_company,
            context.args[0],
            fundamental_provider=fundamental_provider,
        )
        await update.message.reply_text(format_company_analysis(result), disable_web_page_preview=True)
    except FundamentalDataError as exc:
        await update.message.reply_text(
            f"Şirket temel analizi doğrulanamadı: {exc}\n\n"
            "Lisanslı kaynak yapılandırılmadan veri uydurulmaz. Kaynak ayarlarını /veri_durumu ile kontrol et."
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Temel analiz başarısız symbol=%s error=%s", context.args[0], type(exc).__name__)
        await update.message.reply_text("Şirket temel analizi şu anda alınamadı; daha sonra tekrar dene.")


async def cmd_kap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /kap THYAO")
        return
    from urllib.parse import quote
    from app.data.base_provider import DataUnavailableError
    from app.data.provider_factory import build_kap_provider
    symbol = context.args[0].strip().upper().removesuffix(".IS")
    url = f"https://www.kap.org.tr/tr/search/{quote(symbol)}/1"
    settings = get_settings()
    if str(settings.kap_provider).casefold() != "kap_rest":
        await update.message.reply_text(
            f"📣 {symbol} KAP BİLDİRİMLERİ\n\nResmî KAP araması: {url}\n\n"
            "Bot içi akış için KAP_PROVIDER=kap_rest ve sözleşmeli KAP REST erişimi gerekir; "
            "erişim bilgileri olmadan bildirim uydurulmaz.",
            disable_web_page_preview=True,
        )
        return
    try:
        disclosures = await asyncio.to_thread(build_kap_provider(settings).get_latest_disclosures, symbol)
    except (DataUnavailableError, ValueError) as exc:
        logger.warning("KAP bildirimleri alınamadı symbol=%s error=%s", symbol, type(exc).__name__)
        await update.message.reply_text(
            f"KAP bildirimleri şu anda doğrulanamadı.\nResmî arama: {url}",
            disable_web_page_preview=True,
        )
        return
    if not disclosures:
        await update.message.reply_text(
            f"📣 {symbol} için lisanslı akışta bildirim bulunamadı.\nResmî arama: {url}",
            disable_web_page_preview=True,
        )
        return
    lines = [f"📣 {symbol} KAP BİLDİRİMLERİ", "━━━━━━━━━━━━━━━━━━"]
    for item in disclosures[:10]:
        published = item.get("published_at")
        if published:
            from zoneinfo import ZoneInfo
            stamp = published.astimezone(ZoneInfo(settings.timezone_name)).strftime("%d.%m.%Y %H:%M")
        else:
            stamp = "Zaman bilgisi yok"
        lines.extend([
            f"\n• {item['title']}",
            f"  {stamp} • {item['classification']}",
            f"  {item['source_url']}",
        ])
    lines.extend(["", "Sınıflandırma yalnız anahtar kelime özetidir; yatırım tavsiyesi değildir."])
    await update.message.reply_text("\n".join(lines)[:4096], disable_web_page_preview=True)


async def cmd_komutlar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    await update.message.reply_text(
        "🤖 AI HİSSE ASİSTANI\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "/ai_analiz THYAO — teknik + temel + haber + risk raporu\n"
        "THYAO yaz — aynı AI analizini hızlıca başlatır\n"
        "Grafik fotoğrafı gönder — görseli okuyup koşullu senaryolar üretir\n"
        "İpucu: Fotoğraf açıklamasına hisse, süre ve risk toleransını yaz."
    )
    await update.message.reply_text(
        "🏔️📚 MONTANA FİNANS ROBOTU BOT • KOMUT REHBERİ\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🎯 ENTRY • OB/FVG • MSS\n"
        "/islemplani THYAO — en yakın kaliteli OB/FVG bölgesini bulur; net entry, gerekçe, "
        "MSS/CHoCH teyidi, stop, TP1–TP2 ve RR verir\n"
        "  Örnek çıktı: ‘Ben olsam X seviyesinden girerim, çünkü OB/FVG retestidir.’\n"
        "  Not: Son kapanışı entry yapmaz; fiyat uzaktaysa beklenen retest seviyesini yazar.\n"
        "/kademe THYAO — aktif OB/FVG'yi %40/%35/%25 böler; PENDING/CONFIRMED durumunu, "
        "ortak SL'yi ve sade varsayımsal senaryo grafiğini gösterir; dolan kademeleri sanal izleyip "
        "yeni ortalama maliyeti bildirir\n"
        "/kirilsanaryo THYAO — destek/direnç kırılırsa gidilebilecek sonraki dinamik hedefi "
        "ve bullish/bearish mini mum grafiğini gösterir\n\n"
        "📊 ANALİZ\n"
        "/analiz THYAO — teknik analiz + 5dk/15dk/1s/4s okuması\n"
        "/islemplani THYAO — tek ana LONG/SHORT senaryosu ve yapısal entry planı\n"
        "/sirket THYAO — şirketi ve finansal durumunu anlatır\n"
        "/kap THYAO — lisanslı akış varsa son KAP bildirimlerini, yoksa resmî aramayı gösterir\n"
        "/haber THYAO — son 24–48 saat haber/KAP başlıklarını kaynak, tarih ve piyasa algısıyla özetler\n"
        "/seviyeler THYAO — destek ve dirençleri gösterir\n"
        "/cokluzaman THYAO — farklı zaman dilimlerini karşılaştırır\n\n"
        "📚 TÜM HİSSELER VE RAPORLAR\n"
        "/tum_hisseler — PDF'den aktarılan 571 kodu sayfalı gösterir\n"
        "/eniyi50 — tüm evrende en kaliteli 50 giriş bölgesini tarar\n"
        "/sabah_raporu — 09:00 SMXM raporunu şimdi üretir\n"
        "/smxm_aksam_raporu — 21:00 kapanış ve tahmin karşılaştırmasını üretir\n\n"
        "🧪 GEÇMİŞ PERFORMANS\n"
        "/backtest THYAO — son 2 yılı masraflarla test eder\n"
        "/backtest THYAO 2023-01-01 2026-01-01 — özel dönem\n"
        "/backtest_ozet — son testleri ve başarı oranlarını gösterir\n\n"
        "/smxm_backtest THYAO 2025-01-01 2025-06-01 10000 — A+ setup simülasyonu\n"
        "/sanal_portfoy_olustur Ana 10000 — bağımsız sanal hesap oluşturur\n"
        "/sanal_portfoyler — tüm SMXM sanal hesaplarını listeler\n\n"
        "📌 SİNYAL TAKİBİ\n"
        "/sinyaller — üretilen sinyalleri listeler\n"
        "/sinyal 123 — sinyal planı ve olay geçmişi\n"
        "/takip 123 — planı sana ait PENDING_ENTRY kaydı olarak izler\n"
        "/takip_birak 123 — otomatik izlemeyi durdurur\n"
        "/sinyal_iptal 123 — gerçekleşmemiş giriş planını iptal eder\n"
        "/stop_girise 123 — aktif pozisyon stopunu girişe taşır\n"
        "/pozisyon_kapat 123 — doğrulanmış canlı fiyatla sanal takibi kapatır\n"
        "/aktif_pozisyonlar — açık sanal takip pozisyonlarını gösterir\n\n"
        "🧪 GELİŞMİŞ BACKTEST\n"
        "/backtest_signal 123 — kayıtlı sinyali kronolojik yeniden oynatır\n"
        "/backtest_watchlist 1g 3y — izleme listesini test eder\n"
        "/backtest_sector XBANK 1g 5y — sektör evrenini test eder\n"
        "/backtest_bist30 1g 3y — doğrulanmış üyelik dosyasıyla BIST 30\n"
        "/backtest_stats — yalnız sana ait toplam istatistikler\n\n"
        "🔔 ALARMLAR\n"
        "/alarm 9.20 THYAO — fiyat alarmı kurar\n"
        "/alarm 9.20 THYAO ASELS ses=radar — çoklu alarm kurar\n"
        "/alarm_kur ASELS 72.50 üstü — koşullu kalıcı alarm\n"
        "/toplu_alarm — metin/CSV/XLSX ile önizlemeli toplu kurulum\n"
        "/alarm_test zil — alarm sesini dener\n"
        "/alarm_yardim — alarmı en sade şekilde öğretir\n"
        "/alarmlar — açık alarmları listeler\n"
        "/alarm_sil 12 — alarmı siler\n\n"
        "⭐ TAKİP VE PİYASA\n"
        "/ekle THYAO — izleme listesine ekler\n"
        "/liste — izleme listesini gösterir\n"
        "/tara — listedeki hisseleri tarar\n"
        "/piyasa — 571 hisse piyasa genişliği ve yarın yön çerçevesi\n"
        "/piyasa long|short|tum — puan eşiğini geçen tüm adayları sayfalar\n"
        "/firsatlar — 3+ teyitli al-sat ve kısa vade adaylarını; uzun vade teknik takip listesini ve yüksek oynaklık uyarılarını sade kartta gösterir\n"
        "/aksam_raporu — kapanış raporunu hemen üretir\n\n"
        "💼 PORTFÖY\n"
        "/portfoy — portföy özetini gösterir\n"
        "/pozisyon_ekle THYAO 100 250 — pozisyon ekler\n"
        "/portfoy_risk — toplam portföy riskini gösterir\n\n"
        "ℹ️ Komutu sembolle birlikte yaz. Örnek: /analiz THYAO"
    )
