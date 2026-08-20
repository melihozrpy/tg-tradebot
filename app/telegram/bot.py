from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from app.config.settings import get_settings
from app.telegram import (
    handlers,
    handlers_stage5g,
    handlers_v3,
    smxm_report_handlers,
    ultra_backtest_handlers,
    ultra_signal_handlers,
)

logger = logging.getLogger("mergen_quant.telegram.bot")


def build_telegram_application() -> Application:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN ayarlanmamis. .env dosyasina BotFather'dan aldigin tokeni ekle."
        )

    application = Application.builder().token(settings.telegram_bot_token).build()
    from app.services.health_service import mark_runtime_health
    mark_runtime_health("telegram", "configured")

    # ---- V2 komutlari (korunuyor) ----
    application.add_handler(CommandHandler("help", handlers.cmd_help))
    application.add_handler(CommandHandler("yardim", handlers.cmd_help))
    application.add_handler(CommandHandler("komutlar", handlers_v3.cmd_komutlar))
    application.add_handler(CommandHandler("ekle", handlers.cmd_ekle))
    application.add_handler(CommandHandler("sil", handlers.cmd_sil))
    application.add_handler(CommandHandler("liste", handlers.cmd_liste))
    application.add_handler(CommandHandler("portfoy", handlers.cmd_portfoy))
    application.add_handler(CommandHandler("backtest", handlers_stage5g.cmd_backtest))
    application.add_handler(CommandHandler("acil_durdur", handlers.cmd_acil_durdur))
    application.add_handler(CommandHandler("devam_et", handlers.cmd_devam_et))
    application.add_handler(CommandHandler("durum", handlers.cmd_durum))

    # ---- V3: /start ve /analiz gelistirilmis surumlerle degistirildi ----
    # (komut adlari AYNI kaldigi icin mevcut kullanim bozulmuyor, ic mantik iyilesti)
    application.add_handler(CommandHandler("start", handlers_v3.cmd_start_v3))
    application.add_handler(CommandHandler("analiz", handlers_v3.cmd_analiz_v3))
    application.add_handler(CommandHandler("analiz_detay", handlers_v3.cmd_analiz_detay))
    application.add_handler(CommandHandler("islemplani", handlers_v3.cmd_islemplani))
    application.add_handler(CommandHandler("kademe", handlers_v3.cmd_kademe))
    application.add_handler(CommandHandler("gunici", handlers_v3.cmd_gunici))
    application.add_handler(CommandHandler("anomali", handlers_v3.cmd_anomali))
    application.add_handler(CommandHandler("anomaliler", handlers_v3.cmd_anomaliler))
    application.add_handler(CommandHandler("zaman_dilimleri", handlers_v3.cmd_zaman_dilimleri))
    application.add_handler(CommandHandler("cokluzaman", handlers_v3.cmd_zaman_dilimleri))
    application.add_handler(CommandHandler("likidite", handlers_v3.cmd_likidite))
    application.add_handler(CommandHandler("seviyeler", handlers_v3.cmd_seviyeler))
    application.add_handler(CommandHandler("veri_durumu", handlers_v3.cmd_veri_durumu))
    application.add_handler(CommandHandler("senaryo", handlers_v3.cmd_senaryo))
    application.add_handler(CommandHandler("kirilsanaryo", handlers_v3.cmd_kirilsanaryo))
    application.add_handler(CommandHandler("uzunsenaryo", handlers_v3.cmd_uzunsenaryo))
    application.add_handler(CommandHandler("hedefkontrol", handlers_v3.cmd_hedefkontrol))
    application.add_handler(CommandHandler("hedefyolu", handlers_v3.cmd_hedefyolu))
    application.add_handler(CommandHandler("degerleme", handlers_v3.cmd_degerleme))
    application.add_handler(CommandHandler("uzungrafik", handlers_v3.cmd_uzungrafik))
    application.add_handler(CommandHandler("sermaye_islemleri", handlers_v3.cmd_sermaye_islemleri))
    application.add_handler(CommandHandler("hedefgecmisi", handlers_v3.cmd_hedefgecmisi))
    application.add_handler(CommandHandler("hedefbasari", handlers_v3.cmd_hedefbasari))
    # V3.2 (Asama 4): haber radari + haber etkisi + Groq AI aciklama
    application.add_handler(CommandHandler("haberler", handlers_v3.cmd_haberler))
    application.add_handler(CommandHandler("haber", handlers_v3.cmd_haber))
    application.add_handler(CommandHandler("haber_detay", handlers_v3.cmd_haber_detay))
    application.add_handler(CommandHandler("haber_radari", handlers_v3.cmd_haber_radari))
    application.add_handler(CommandHandler("ai_aciklama", handlers_v3.cmd_ai_aciklama))
    from app.telegram.stock_ai_handlers import cmd_ai_analiz
    application.add_handler(CommandHandler("ai_analiz", cmd_ai_analiz))
    application.add_handler(CommandHandler("hisse_ai", cmd_ai_analiz))
    application.add_handler(CommandHandler("sirket", handlers_v3.cmd_sirket))
    from app.telegram.fundamental_handlers import cmd_temelanaliz
    application.add_handler(CommandHandler("temelanaliz", cmd_temelanaliz))
    application.add_handler(CommandHandler("kap", handlers_v3.cmd_kap))
    application.add_handler(CommandHandler("skor_detay", handlers_v3.cmd_skor_detay))

    # ---- V3: Sektor ----
    application.add_handler(CommandHandler("sektor_ayarla", handlers_v3.cmd_sektor_ayarla))
    application.add_handler(CommandHandler("sektor", handlers_v3.cmd_sektor))
    application.add_handler(CommandHandler("guc", handlers_v3.cmd_guc))
    application.add_handler(CommandHandler("sektor_listesi", handlers_v3.cmd_sektor_listesi))

    # ---- V3: Tarama ----
    application.add_handler(CommandHandler("tara", handlers_v3.cmd_tara))
    application.add_handler(CommandHandler("tara_liste", handlers_v3.cmd_tara_liste))
    application.add_handler(CommandHandler("tarama_durumu", handlers_v3.cmd_tarama_durumu))
    from app.telegram.market_opportunity_handlers import cmd_firsatlar, cmd_gunluk5
    from app.telegram.baby_stock_handlers import cmd_bebekhisse, cmd_bebekhisse_ayar, cmd_bebekhisse_kontrol
    from app.telegram.basic_viop_handlers import cmd_basitalsat, cmd_viop, cmd_viopislem
    from app.telegram.borsa_copilot_handlers import cmd_borsa_copilot, cmd_varant, cmd_viop_copilot
    from app.telegram.scheduled_idea_handlers import cmd_oneriler, cmd_oneri_performans
    application.add_handler(CommandHandler("firsatlar", cmd_firsatlar))
    application.add_handler(CommandHandler("firsat", cmd_firsatlar))
    application.add_handler(CommandHandler("gunluk5", cmd_gunluk5))
    application.add_handler(CommandHandler("gunluk_ilk5", cmd_gunluk5))
    application.add_handler(CommandHandler("bebekhisse", cmd_bebekhisse))
    application.add_handler(CommandHandler("bebekhisse_kontrol", cmd_bebekhisse_kontrol))
    application.add_handler(CommandHandler("bebekhisse_ayar", cmd_bebekhisse_ayar))
    application.add_handler(CommandHandler("basitalsat", cmd_basitalsat))
    application.add_handler(CommandHandler("viop", cmd_viop))
    application.add_handler(CommandHandler("viopislem", cmd_viopislem))
    application.add_handler(CommandHandler("borsacopilot", cmd_borsa_copilot))
    application.add_handler(CommandHandler("viopcopilot", cmd_viop_copilot))
    application.add_handler(CommandHandler("varant", cmd_varant))
    application.add_handler(CommandHandler("oneriler", cmd_oneriler))
    application.add_handler(CommandHandler("oneri_performans", cmd_oneri_performans))
    application.add_handler(CommandHandler("aksam_raporu", smxm_report_handlers.cmd_smxm_aksam_raporu))
    application.add_handler(CommandHandler("tarama_ayarlari", handlers_v3.cmd_tarama_ayarlari))
    application.add_handler(CommandHandler("tum_hisseler", smxm_report_handlers.cmd_tum_hisseler))
    application.add_handler(CommandHandler("eniyi50", smxm_report_handlers.cmd_eniyi50))
    application.add_handler(CommandHandler("en_iyi_50", smxm_report_handlers.cmd_eniyi50))
    application.add_handler(CommandHandler("sabah_raporu", smxm_report_handlers.cmd_sabah_raporu))
    application.add_handler(CommandHandler("smxm_aksam_raporu", smxm_report_handlers.cmd_smxm_aksam_raporu))

    # ---- V3: Sinyal gecmisi / performans ----
    application.add_handler(CommandHandler("sinyaller", handlers_v3.cmd_sinyaller))
    application.add_handler(CommandHandler("aktif_sinyaller", handlers_v3.cmd_aktif_sinyaller))
    application.add_handler(CommandHandler("sinyal", handlers_v3.cmd_sinyal))
    application.add_handler(CommandHandler("sinyal_gecmisi", handlers_v3.cmd_sinyal_gecmisi))
    application.add_handler(CommandHandler("performans", handlers_v3.cmd_performans))
    ultra_signal_handlers.register_ultra_signal_handlers(application)

    # ---- V3: Portfoy genisletmeleri ----
    application.add_handler(CommandHandler("pozisyon_ekle", handlers_v3.cmd_pozisyon_ekle))
    application.add_handler(CommandHandler("pozisyon_sil", handlers_v3.cmd_pozisyon_sil))
    application.add_handler(CommandHandler("pozisyon_guncelle", handlers_v3.cmd_pozisyon_guncelle))
    application.add_handler(CommandHandler("portfoy_risk", handlers_v3.cmd_portfoy_risk))
    application.add_handler(CommandHandler("pozisyon_boyutu", handlers_v3.cmd_pozisyon_boyutu))
    application.add_handler(CommandHandler("maliyet", handlers_v3.cmd_maliyet))
    application.add_handler(CommandHandler("nakit_ayarla", handlers_v3.cmd_nakit_ayarla))
    application.add_handler(CommandHandler("sermaye_ayarla", handlers_v3.cmd_sermaye_ayarla))

    # ---- Kalici fiyat alarmlari (0008) ----
    # Yeni handler seti sahiplik, toplu ice aktarma, OCR, public referans ve
    # onay akisini tek yerde kaydeder. Ayni komutlarin eski handler'lara da
    # kaydolmasi python-telegram-bot grup sirasina bagli belirsizlik yaratirdi.
    from app.telegram.alarm_handlers import register_alarm_handlers

    register_alarm_handlers(application)
    from app.telegram.stock_ai_handlers import register_stock_ai_handlers
    register_stock_ai_handlers(application)
    # Geriye uyumlu, yeni handler setiyle cakismayan eski kisayollar.
    application.add_handler(CommandHandler("alarm_test", handlers_v3.cmd_alarm_test))
    application.add_handler(CommandHandler("alarm_ac", handlers_v3.cmd_alarm_ac))

    # ---- V3: Grafikler ----
    application.add_handler(CommandHandler("grafik", handlers_v3.cmd_grafik))
    application.add_handler(CommandHandler("rs_grafik", handlers_v3.cmd_rs_grafik))

    # ---- V3: Piyasa genisligi ----
    application.add_handler(CommandHandler("piyasa", handlers_v3.cmd_piyasa))
    application.add_handler(CommandHandler("genislik", handlers_v3.cmd_genislik))

    # ---- V3: Ayarlar ----
    application.add_handler(CommandHandler("ayarlar", handlers_v3.cmd_ayarlar))

    # ---- Asama 5g: ana komutlar; ayrintilar butonlarla acilir ----
    application.add_handler(CommandHandler("backtest_ozet", handlers_stage5g.cmd_backtest_ozet))
    application.add_handler(CommandHandler("sanal_portfoy", handlers_stage5g.cmd_sanal_portfoy))
    application.add_handler(CommandHandler("sanal_performans", handlers_stage5g.cmd_sanal_performans))
    application.add_handler(CommandHandler("sinyalbasari", handlers_stage5g.cmd_sinyalbasari))
    application.add_handler(CommandHandler("kalibrasyon", handlers_stage5g.cmd_kalibrasyon))
    application.add_handler(CommandHandler("neden", handlers_stage5g.cmd_neden))
    application.add_handler(CommandHandler("sanal_portfoy_olustur", smxm_report_handlers.cmd_sanal_portfoy_olustur))
    application.add_handler(CommandHandler("sanal_portfoyler", smxm_report_handlers.cmd_sanal_portfoyler))
    application.add_handler(CommandHandler("smxm_backtest", smxm_report_handlers.cmd_smxm_backtest))
    ultra_backtest_handlers.register_ultra_backtest_handlers(application)

    # ---- V3: Inline buton callback'leri ----
    application.add_handler(CallbackQueryHandler(handlers_v3.handle_detail_callback, pattern=r"^detay_"))
    application.add_handler(CallbackQueryHandler(handlers_v3.handle_stage5f_callback, pattern=r"^stage5f_"))
    application.add_handler(CallbackQueryHandler(handlers_v3.handle_menu_callback, pattern=r"^menu_"))
    application.add_handler(CallbackQueryHandler(handlers_stage5g.handle_stage5g_callback, pattern=r"^stage5g_"))
    application.add_handler(CallbackQueryHandler(smxm_report_handlers.handle_universe_callback, pattern=r"^universe_page_"))

    return application


def _build_evening_scan_scheduler(settings, application: Application | None = None) -> AsyncIOScheduler:
    """Kapanis sonrasi otomatik tarama icin APScheduler nesnesini olusturur.

    Onemli: Bu fonksiyon scheduler'i OLUSTURUR ama BASLATMAZ. AsyncIOScheduler.start()
    calisan bir asyncio event loop'u gerektirir; run_polling() cagrilmadan once
    (yani event loop baslamadan once) start() cagrilirsa
    "RuntimeError: no running event loop" hatasi olusur. Bu yuzden baslatma islemi
    application.post_init callback'ine tasinmistir (bkz. _register_scheduler_lifecycle),
    o an event loop kesin olarak calisir durumdadir.

    CLOSE_SCAN_ENABLED yalnizca kapanis taramasini acip kapatir. Fiyat alarmi,
    teslimat outbox'i ve diger bagimsiz zamanlanmis isler her durumda ayni
    scheduler uzerinde calismaya devam eder.

    `application` verilirse (gercek calisma zamaninda), aksam taramasi ve gun
    ici anomali taramasi sonuclarina gore kullanicilara OTOMATIK Telegram
    bildirimi + grafik gonderilir (bolum 8). Testlerde application=None
    gecilir; bu durumda taramalar sessizce calisir, hicbir mesaj gonderilmez.
    """
    scheduler = AsyncIOScheduler(timezone=settings.timezone_name)

    if not settings.close_scan_enabled:
        logger.info(
            "Otomatik aksam taramasi devre disi (CLOSE_SCAN_ENABLED=false); "
            "diger scheduler isleri calismaya devam edecek."
        )

    async def _job() -> None:
        from app.data.provider_factory import build_market_data_provider
        from app.models.database import get_session_factory
        from app.services.notification_service import notify_daily_top_candidates, scan_and_notify_anomalies
        from app.services.scan_service import ScanBlockedByKillSwitchError, get_distinct_watchlist_symbols, run_evening_scan
        from app.services.signal_lifecycle_service import update_open_signals

        db = get_session_factory()()
        try:
            provider = build_market_data_provider(settings)
            update_open_signals(db, provider, conservative_execution=settings.conservative_execution, expiry_trading_days=settings.signal_expiry_trading_days)
            symbols = get_distinct_watchlist_symbols(db)
            if symbols:
                summary = run_evening_scan(db, provider, settings, symbols=symbols)
                from app.services.health_service import mark_runtime_health
                mark_runtime_health("close_scan", "ok")
                logger.info("Otomatik aksam taramasi tamamlandi: %s sembol.", len(symbols))
                # V3.2 (Asama 3, bolum 7-8): gunluk otomatik grafik bildirimleri.
                try:
                    sent = await notify_daily_top_candidates(application, db, provider, summary)
                    if sent:
                        logger.info("Gunluk otomatik grafik bildirimi gonderildi: %s kullanici.", sent)
                except Exception as exc:  # noqa: BLE001 - bildirim hatasi taramayi geçersiz kilmamali
                    logger.error("Gunluk otomatik bildirim hata verdi: %s", exc)
                # V3.2 (Asama 3): anormal hareket taramasi + alarm bildirimleri.
                try:
                    notified = await scan_and_notify_anomalies(application, db, provider, symbols, timeframe="1d")
                    if notified:
                        logger.info("Anomali bildirimi gonderildi: %s kullanici.", notified)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Anomali taramasi/bildirimi hata verdi: %s", exc)
        except ScanBlockedByKillSwitchError:
            logger.info("Otomatik aksam taramasi kill switch aktif oldugu icin atlandi.")
        except Exception as exc:  # noqa: BLE001 - zamanlanmis is asla botu cokertmemeli
            logger.error("Otomatik aksam taramasi hata verdi: %s", exc)
        finally:
            db.close()

    if settings.close_scan_enabled:
        try:
            hour_str, minute_str = settings.close_scan_time.split(":")
            hour, minute = int(hour_str), int(minute_str)
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            logger.warning(
                "CLOSE_SCAN_TIME formati hatali (%s); yalnizca kapanis taramasi kurulmadi.",
                getattr(settings, "close_scan_time", None),
            )
        else:
            scheduler.add_job(
                _job,
                CronTrigger(hour=hour, minute=minute),
                id="evening_close_scan",
                coalesce=True,
                max_instances=1,
            )
            logger.info(
                "Otomatik aksam taramasi %s (%s) icin hazirlandi (event loop baslayinca aktif olacak).",
                settings.close_scan_time,
                settings.timezone_name,
            )

    if getattr(settings, "daily_brief_enabled", False) and not getattr(settings, "morning_report_enabled", False):
        async def _daily_brief_job() -> None:
            from app.data.provider_factory import build_market_data_provider
            from app.models.database import User, get_session_factory
            from app.services.daily_market_report_service import build_daily_market_report, format_daily_market_report
            from app.services.scheduled_delivery_dedup import claim_scheduled_delivery

            db = get_session_factory()()
            try:
                provider = build_market_data_provider(settings)
                report = await asyncio.to_thread(build_daily_market_report, provider, settings)
                text = format_daily_market_report(report)
                if application is not None:
                    local_date = datetime.now(timezone.utc).astimezone(
                        ZoneInfo(settings.timezone_name)
                    ).date()
                    delivery_key = f"scheduled:daily-brief:{local_date.isoformat()}"
                    for user in db.query(User).filter(User.kill_switch_active.is_(False)).all():
                        if not claim_scheduled_delivery(
                            db,
                            dedup_key=delivery_key,
                            chat_id=int(user.telegram_user_id),
                        ):
                            logger.info(
                                "Tekrarlanan günlük brifing atlandı chat=%s key=%s",
                                user.telegram_user_id,
                                delivery_key,
                            )
                            continue
                        try:
                            await application.bot.send_message(chat_id=user.telegram_user_id, text=text)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Günlük brifing gönderilemedi user=%s: %s", user.id, exc)
            except Exception as exc:  # noqa: BLE001
                logger.error("Günlük piyasa brifingi üretilemedi: %s", exc)
            finally:
                db.close()

        try:
            brief_hour, brief_minute = settings.daily_brief_time.split(":")
            scheduler.add_job(
                _daily_brief_job,
                CronTrigger(
                    day_of_week="mon-fri",
                    hour=int(brief_hour),
                    minute=int(brief_minute),
                    timezone=settings.timezone_name,
                ),
                id="daily_market_brief",
                coalesce=True,
                max_instances=1,
            )
        except (TypeError, ValueError):
            logger.warning("DAILY_BRIEF_TIME formatı geçersiz: %s", settings.daily_brief_time)

    async def _notify_report_error(job_label: str, exc: Exception) -> None:
        """Zamanlanmış rapor hatasını adminlere bildir; kendi hatasını yutkunur."""
        if application is None:
            return
        from app.models.database import User, get_session_factory

        db = get_session_factory()()
        try:
            recipients = {
                int(item.telegram_user_id)
                for item in db.query(User).filter(User.is_admin.is_(True)).all()
            }
            recipients.update(int(value) for value in getattr(settings, "admin_ids", ()))
            for chat_id in recipients:
                try:
                    await application.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"⚠️ {job_label} üretilemedi\n"
                            f"Hata: {type(exc).__name__}\n"
                            "Bot çalışmaya devam ediyor. /veri_durumu ile veri kaynağını kontrol et."
                        ),
                    )
                except Exception as delivery_exc:  # noqa: BLE001
                    logger.warning(
                        "Rapor hata bildirimi gönderilemedi chat=%s: %s", chat_id, delivery_exc
                    )
        finally:
            db.close()

    def _scheduled_recipients(db, *, configured_chat_id: int | None = None) -> set[int]:
        """Resolve recipients once, respecting an explicit broadcast chat first."""

        from app.models.database import User

        if configured_chat_id:
            return {int(configured_chat_id)}
        recipients = {
            int(user.telegram_user_id)
            for user in db.query(User).filter(User.kill_switch_active.is_(False)).all()
        }
        recipients.update(int(value) for value in getattr(settings, "admin_ids", ()))
        return recipients

    async def _deliver_scheduled_ideas(slot: str) -> None:
        """Run the top-two, retest-first scanner and store the exact plans."""

        def _scan_and_save():
            from app.analysis.screener_engine import run_daily_top_picks_scan
            from app.config.instruments import universe_symbols
            from app.data.provider_factory import build_market_data_provider
            from app.fundamentals.factory import build_fundamental_provider
            from app.models.database import get_session_factory
            from app.services.scheduled_idea_service import persist_scheduled_ideas

            scan_settings = settings.model_copy(
                update={
                    "daily_top_picks_max_results": int(settings.scheduled_ideas_max_results),
                    "daily_top_picks_require_fundamental": bool(settings.scheduled_ideas_require_fundamental),
                }
            )
            result = run_daily_top_picks_scan(
                symbols=universe_symbols(settings.bist_universe_json_path),
                provider_factory=lambda: build_market_data_provider(settings),
                fundamental_provider_factory=lambda: build_fundamental_provider(settings),
                settings=scan_settings,
            )
            db = get_session_factory()()
            try:
                persist_scheduled_ideas(
                    db,
                    report=result,
                    slot=slot,
                    maximum=settings.scheduled_ideas_max_results,
                )
            finally:
                db.close()
            return result

        result = await asyncio.to_thread(_scan_and_save)
        logger.info("Zamanlanmış iki aday tarandı slot=%s scanned=%s picks=%s", slot, result.scanned, len(result.picks))
        if application is None:
            return
        from app.models.database import get_session_factory
        from app.services.scheduled_delivery_dedup import claim_scheduled_delivery
        from app.services.scheduled_idea_service import format_scheduled_ideas_report

        text = format_scheduled_ideas_report(
            result,
            slot=slot,
            timezone_name=settings.timezone_name,
            maximum=settings.scheduled_ideas_max_results,
        )
        local = result.created_at.astimezone(ZoneInfo(settings.timezone_name))
        delivery_key = f"scheduled:two-ideas:{slot}:{local:%Y%m%d}"
        db = get_session_factory()()
        try:
            for chat_id in _scheduled_recipients(db):
                if not claim_scheduled_delivery(db, dedup_key=delivery_key, chat_id=chat_id):
                    continue
                try:
                    await application.bot.send_message(chat_id=chat_id, text=text)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("İki aday kartı gönderilemedi chat=%s: %s", chat_id, exc)
        finally:
            db.close()

    # Default false for legacy/test settings objects that predate this additive
    # feature; real Settings declares the feature enabled by default.
    if getattr(settings, "scheduled_ideas_enabled", False):
        def _add_idea_job(slot: str, at_time: str, job_id: str) -> None:
            try:
                hour_text, minute_text = at_time.split(":")

                async def _idea_job(current_slot: str = slot) -> None:
                    await _deliver_scheduled_ideas(current_slot)

                scheduler.add_job(
                    _idea_job,
                    CronTrigger(
                        day_of_week="mon-fri",
                        hour=int(hour_text),
                        minute=int(minute_text),
                        timezone="Europe/Istanbul",
                    ),
                    id=job_id,
                    coalesce=True,
                    max_instances=1,
                    replace_existing=True,
                )
            except (AttributeError, TypeError, ValueError):
                logger.warning("Zamanlanmış iki aday saati geçersiz slot=%s time=%s", slot, at_time)

        _add_idea_job("morning", settings.scheduled_ideas_morning_time, "scheduled_two_ideas_morning")
        _add_idea_job("afternoon", settings.scheduled_ideas_afternoon_time, "scheduled_two_ideas_afternoon")

    if getattr(settings, "scheduled_idea_performance_enabled", False):
        async def _scheduled_idea_performance_job() -> None:
            def _evaluate():
                from app.data.provider_factory import build_market_data_provider
                from app.models.database import get_session_factory
                from app.services.scheduled_idea_service import evaluate_due_ideas

                db = get_session_factory()()
                try:
                    return evaluate_due_ideas(
                        db,
                        provider=build_market_data_provider(settings),
                        minimum_age_days=settings.scheduled_idea_performance_days,
                    )
                finally:
                    db.close()

            items = await asyncio.to_thread(_evaluate)
            if application is None or not items:
                return
            from app.models.database import get_session_factory
            from app.services.scheduled_delivery_dedup import claim_scheduled_delivery
            from app.services.scheduled_idea_service import format_idea_performance_report

            local = datetime.now(timezone.utc).astimezone(ZoneInfo(settings.timezone_name))
            text = format_idea_performance_report(items, timezone_name=settings.timezone_name)
            delivery_key = f"scheduled:two-ideas-performance:{local:%G-W%V}"
            db = get_session_factory()()
            try:
                for chat_id in _scheduled_recipients(db):
                    if not claim_scheduled_delivery(db, dedup_key=delivery_key, chat_id=chat_id):
                        continue
                    try:
                        await application.bot.send_message(chat_id=chat_id, text=text)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("İki günlük plan takibi gönderilemedi chat=%s: %s", chat_id, exc)
            finally:
                db.close()

        try:
            perf_hour, perf_minute = settings.scheduled_idea_performance_time.split(":")
            scheduler.add_job(
                _scheduled_idea_performance_job,
                CronTrigger(
                    day_of_week="tue,thu",
                    hour=int(perf_hour),
                    minute=int(perf_minute),
                    timezone="Europe/Istanbul",
                ),
                id="scheduled_two_ideas_performance",
                coalesce=True,
                max_instances=1,
                replace_existing=True,
            )
        except (AttributeError, TypeError, ValueError):
            logger.warning("SCHEDULED_IDEA_PERFORMANCE_TIME geçersiz: %s", settings.scheduled_idea_performance_time)

    if getattr(settings, "kap_monitor_enabled", False):
        async def _kap_monitor_job() -> None:
            def _claim():
                from app.models.database import get_session_factory
                from app.services.kap_monitor_service import claim_new_impacting_headlines

                db = get_session_factory()()
                try:
                    return claim_new_impacting_headlines(
                        db,
                        url=settings.kap_monitor_url,
                        minimum_impact_score=settings.kap_monitor_minimum_impact_score,
                    )
                finally:
                    db.close()

            try:
                headlines = await asyncio.to_thread(_claim)
            except PermissionError as exc:
                logger.warning("KAP başlık monitörü robots politikası nedeniyle durdu: %s", exc)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("KAP başlık monitörü hata verdi: %s", type(exc).__name__)
                return
            if application is None or not headlines:
                return
            from app.models.database import get_session_factory
            from app.services.kap_monitor_service import format_kap_alert

            db = get_session_factory()()
            try:
                for headline in headlines[:8]:
                    for chat_id in _scheduled_recipients(
                        db,
                        configured_chat_id=getattr(settings, "kap_monitor_chat_id", None),
                    ):
                        try:
                            await application.bot.send_message(
                                chat_id=chat_id,
                                text=format_kap_alert(headline),
                                disable_web_page_preview=True,
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("KAP başlık bildirimi gönderilemedi chat=%s: %s", chat_id, exc)
            finally:
                db.close()

        scheduler.add_job(
            _kap_monitor_job,
            IntervalTrigger(minutes=int(settings.kap_monitor_interval_minutes), timezone="Europe/Istanbul"),
            id="midas_kap_headline_monitor",
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )

    async def _deliver_smxm_report(kind: str) -> None:
        """DB/provider işlemlerini worker thread'de, Telegram'ı event-loop'ta tutar."""
        from pathlib import Path

        def _build_payload():
            from app.config.instruments import resolve_report_instruments
            from app.data.provider_factory import build_market_data_provider
            from app.models.database import get_session_factory
            from app.modules.chart_engine import render_report_chart
            from app.modules.report_news_impact import build_report_news_impact
            from app.services.market_breadth_service import compute_market_breadth

            db = get_session_factory()()
            try:
                provider = build_market_data_provider(settings)
                instruments = resolve_report_instruments(settings)
                breadth = compute_market_breadth(
                    provider,
                    settings.bist_universe_json_path,
                    max_symbols=settings.universe_scan_max_symbols_per_run,
                    provider_factory=lambda: build_market_data_provider(settings),
                    max_workers=settings.universe_scan_workers,
                    minimum_signal_score=settings.universe_scan_minimum_score,
                    top_n=12,
                    cache_minutes=settings.universe_scan_cache_minutes,
                )
                news_impact = build_report_news_impact(settings, breadth)
                if kind == "morning":
                    from app.modules.morning_report import (
                        build_morning_chart_spec,
                        build_morning_report,
                        format_morning_report,
                    )

                    report = build_morning_report(
                        provider, settings, instruments, db=db, breadth=breadth, news_impact=news_impact
                    )
                    primary = report.index_analysis
                    spec = build_morning_chart_spec(report, primary.symbol)
                    text = format_morning_report(report)
                    caption = f"🌅 {primary.symbol} • 09:00 SMXM sabah görünümü"
                else:
                    from app.modules.evening_report import (
                        build_evening_chart_spec,
                        build_evening_report,
                        format_evening_report,
                    )

                    report = build_evening_report(
                        provider, settings, instruments, db=db, breadth=breadth, news_impact=news_impact
                    )
                    primary = report.index_analysis
                    spec = build_evening_chart_spec(report, primary.symbol)
                    text = format_evening_report(report)
                    caption = f"🌙 {primary.symbol} • 21:00 kapanış görünümü"
                end = datetime.now(timezone.utc)
                days = 520 if kind == "morning" else 180
                frame = provider.get_ohlcv(
                    primary.symbol, "1d", end - timedelta(days=days), end
                )
                chart_path = render_report_chart(
                    frame,
                    spec,
                    smart_money=primary.smart_money,
                    output_dir=settings.report_chart_output_dir,
                    dpi=settings.chart_dpi,
                )
                delivery_key = f"scheduled:smxm:{kind}:{report.report_date.isoformat()}"
                return text, caption, chart_path, delivery_key
            finally:
                db.close()

        chart_path = None
        try:
            text, caption, chart_path, delivery_key = await asyncio.to_thread(_build_payload)
            if application is None:
                return
            from app.models.database import User, get_session_factory
            from app.services.scheduled_delivery_dedup import claim_scheduled_delivery

            db = get_session_factory()()
            try:
                recipients = db.query(User).filter(User.kill_switch_active.is_(False)).all()
                for user in recipients:
                    if not claim_scheduled_delivery(
                        db,
                        dedup_key=delivery_key,
                        chat_id=int(user.telegram_user_id),
                    ):
                        logger.info("Tekrarlanan SMXM raporu atlandı chat=%s key=%s", user.telegram_user_id, delivery_key)
                        continue
                    try:
                        with open(chart_path, "rb") as image:
                            await application.bot.send_photo(
                                chat_id=user.telegram_user_id,
                                photo=image,
                                caption=caption,
                            )
                        await application.bot.send_message(
                            chat_id=user.telegram_user_id,
                            text=text,
                            disable_web_page_preview=True,
                        )
                    except Exception as exc:  # noqa: BLE001 - diğer alıcılara devam
                        logger.warning("SMXM raporu gönderilemedi user=%s: %s", user.id, exc)
            finally:
                db.close()
        finally:
            if chart_path:
                Path(chart_path).unlink(missing_ok=True)

    if getattr(settings, "morning_report_enabled", False):
        async def _smxm_morning_job() -> None:
            try:
                await _deliver_smxm_report("morning")
            except Exception as exc:  # noqa: BLE001 - scheduler ve bot yaşamaya devam eder
                logger.exception("09:00 SMXM sabah raporu üretilemedi")
                await _notify_report_error("09:00 SMXM sabah raporu", exc)

        try:
            morning_hour, morning_minute = settings.morning_report_time.split(":")
            scheduler.add_job(
                _smxm_morning_job,
                CronTrigger(
                    hour=int(morning_hour),
                    minute=int(morning_minute),
                    timezone="Europe/Istanbul",
                ),
                id="smxm_morning_report",
                coalesce=True,
                max_instances=1,
                replace_existing=True,
            )
        except (AttributeError, TypeError, ValueError):
            logger.warning(
                "MORNING_REPORT_TIME geçersiz: %s",
                getattr(settings, "morning_report_time", None),
            )

    if getattr(settings, "evening_market_report_enabled", False):
        async def _smxm_evening_job() -> None:
            try:
                await _deliver_smxm_report("evening")
            except Exception as exc:  # noqa: BLE001
                logger.exception("21:00 SMXM akşam raporu üretilemedi")
                await _notify_report_error("21:00 SMXM akşam raporu", exc)

        try:
            evening_hour, evening_minute = settings.evening_market_report_time.split(":")
            scheduler.add_job(
                _smxm_evening_job,
                CronTrigger(
                    hour=int(evening_hour),
                    minute=int(evening_minute),
                    timezone="Europe/Istanbul",
                ),
                id="smxm_evening_report",
                coalesce=True,
                max_instances=1,
                replace_existing=True,
            )
        except (AttributeError, TypeError, ValueError):
            logger.warning(
                "EVENING_MARKET_REPORT_TIME geçersiz: %s",
                getattr(settings, "evening_market_report_time", None),
            )

    # V3.2 (Asama 3, bolum 7): gun ici otomatik anomali taramasi + grafik.
    # Piyasa saatlerinde (Pzt-Cuma, 10:00-18:00) her 30 dakikada bir calisir.
    # getattr ile okunur ki bu ayar olmayan eski/test konfigurasyonlari BOZULMASIN.
    intraday_enabled = getattr(settings, "intraday_anomaly_scan_enabled", True)
    if intraday_enabled:
        async def _intraday_job() -> None:
            from app.data.provider_factory import build_market_data_provider
            from app.models.database import get_session_factory
            from app.services.notification_service import scan_and_notify_anomalies
            from app.services.scan_service import get_distinct_watchlist_symbols
            from app.services.watchlist_service import is_any_kill_switch_active

            db = get_session_factory()()
            try:
                if is_any_kill_switch_active(db):
                    return
                provider = build_market_data_provider(settings)
                symbols = get_distinct_watchlist_symbols(db)
                if symbols:
                    notified = await scan_and_notify_anomalies(application, db, provider, symbols, timeframe="15m")
                    if notified:
                        logger.info("Gun ici anomali bildirimi gonderildi: %s kullanici.", notified)
            except Exception as exc:  # noqa: BLE001 - zamanlanmis is asla botu cokertmemeli
                logger.error("Gun ici anomali taramasi hata verdi: %s", exc)
            finally:
                db.close()

        scheduler.add_job(
            _intraday_job, CronTrigger(day_of_week="mon-fri", hour="10-18", minute="*/30")
        )
        logger.info("Gun ici otomatik anomali taramasi hazirlandi (Pzt-Cuma 10:00-18:00, 30dk).")

    # The compact card is sent only for setups that pass a full ten-indicator
    # confluence gate. RSI remains an input, never a stand-alone alert.
    if getattr(settings, "trade_scenario_scan_enabled", True):
        async def _trade_scenario_scan_job() -> None:
            def _scan():
                from app.analysis.screener_engine import run_intraday_trade_scenario_scan
                from app.config.instruments import universe_symbols
                from app.data.provider_factory import build_market_data_provider

                return run_intraday_trade_scenario_scan(
                    symbols=universe_symbols(settings.bist_universe_json_path),
                    provider_factory=lambda: build_market_data_provider(settings),
                    settings=settings,
                )

            try:
                result = await asyncio.to_thread(_scan)
                logger.info(
                    "Coklu-gosterge firsat radari tamamlandi scanned=%s failed=%s selected=%s",
                    result.scanned,
                    result.failed,
                    len(result.scenarios),
                )
                if application is None or not result.scenarios:
                    return
                from app.analysis.screener_engine import format_trade_scenario_report
                from app.models.database import User, get_session_factory
                from app.services.scheduled_delivery_dedup import claim_scheduled_delivery

                db = get_session_factory()()
                try:
                    configured = getattr(settings, "technical_screener_chat_id", None)
                    recipients = {int(configured)} if configured else {
                        int(user.telegram_user_id)
                        for user in db.query(User).filter(User.is_admin.is_(True)).all()
                    }
                    recipients.update(int(value) for value in getattr(settings, "admin_ids", ()))
                    text = format_trade_scenario_report(result, timezone_name=settings.timezone_name)
                    local = result.created_at.astimezone(ZoneInfo(settings.timezone_name))
                    delivery_key = f"scheduled:trade-scenario:{local:%Y%m%d%H%M}"
                    for chat_id in recipients:
                        if not claim_scheduled_delivery(db, dedup_key=delivery_key, chat_id=chat_id):
                            logger.info("Tekrarlanan fırsat radarı atlandı chat=%s key=%s", chat_id, delivery_key)
                            continue
                        try:
                            await application.bot.send_message(chat_id=chat_id, text=text)
                        except Exception as exc:  # noqa: BLE001 - one recipient cannot stop the scheduler
                            logger.warning("Firsat radari karti gonderilemedi chat=%s: %s", chat_id, exc)
                finally:
                    db.close()
            except Exception as exc:  # noqa: BLE001 - scheduled jobs must survive
                logger.exception("Firsat radari hata verdi: %s", exc)
                await _notify_report_error("10 göstergeli fırsat radarı", exc)

        # This is the heavy full-universe scan.  It deliberately runs at
        # 10:00, 13:00 and 16:00 Istanbul time instead of every 15 minutes.
        scenario_step = 180
        if scenario_step >= 60 and scenario_step % 60 == 0:
            scenario_trigger = CronTrigger(
                day_of_week="mon-fri",
                hour=f"10-18/{scenario_step // 60}",
                minute=0,
                timezone="Europe/Istanbul",
            )
        else:
            scenario_trigger = CronTrigger(
                day_of_week="mon-fri",
                hour="10-18",
                minute=f"*/{scenario_step}",
                timezone="Europe/Istanbul",
            )
        scheduler.add_job(
            _trade_scenario_scan_job,
            scenario_trigger,
            id="full_universe_trade_scenario_scan",
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        logger.info("10 gostergeli firsat radari %s dakikada bir hazirlandi.", scenario_step)

    # Saat baslarinda, gunluk grafikte teknik olarak uyumlu ve temel verisi
    # dogrulanmis en fazla bes LONG adayi gonderilir.  Bir saat icinde ayni
    # sembolun tekrar gelmesi bilincli bir davranistir: yeni veri o sembolu
    # yine en ust siraya tasiyorsa yapay cesitlilik ugruna gizlenmez.
    if getattr(settings, "daily_top_picks_enabled", False):
        async def _daily_top_picks_job() -> None:
            def _scan():
                from app.analysis.screener_engine import run_daily_top_picks_scan
                from app.config.instruments import universe_symbols
                from app.data.provider_factory import build_market_data_provider
                from app.fundamentals.factory import build_fundamental_provider

                return run_daily_top_picks_scan(
                    symbols=universe_symbols(settings.bist_universe_json_path),
                    provider_factory=lambda: build_market_data_provider(settings),
                    fundamental_provider_factory=lambda: build_fundamental_provider(settings),
                    settings=settings,
                )

            try:
                result = await asyncio.to_thread(_scan)
                logger.info(
                    "Saatlik gunluk ilk 5 taramasi tamamlandi scanned=%s failed=%s picks=%s fundamentals=%s/%s",
                    result.scanned,
                    result.failed,
                    len(result.picks),
                    result.fundamental_verified,
                    result.fundamental_checked,
                )
                if application is None:
                    return
                from app.analysis.screener_engine import format_daily_top_picks_report
                from app.models.database import User, get_session_factory
                from app.services.scheduled_delivery_dedup import claim_scheduled_delivery

                db = get_session_factory()()
                try:
                    configured = getattr(settings, "technical_screener_chat_id", None)
                    recipients = {int(configured)} if configured else {
                        int(user.telegram_user_id)
                        for user in db.query(User).filter(User.is_admin.is_(True)).all()
                    }
                    recipients.update(int(value) for value in getattr(settings, "admin_ids", ()))
                    text = format_daily_top_picks_report(result, timezone_name=settings.timezone_name)
                    local = result.created_at.astimezone(ZoneInfo(settings.timezone_name))
                    delivery_key = f"scheduled:daily-top-picks:{local:%Y%m%d%H}"
                    for chat_id in recipients:
                        if not claim_scheduled_delivery(db, dedup_key=delivery_key, chat_id=chat_id):
                            logger.info("Tekrarlanan günlük ilk 5 atlandı chat=%s key=%s", chat_id, delivery_key)
                            continue
                        try:
                            await application.bot.send_message(chat_id=chat_id, text=text)
                        except Exception as exc:  # noqa: BLE001 - one recipient cannot stop the scheduler
                            logger.warning("Saatlik ilk 5 karti gonderilemedi chat=%s: %s", chat_id, exc)
                finally:
                    db.close()
            except Exception as exc:  # noqa: BLE001 - scheduled jobs must survive
                logger.exception("Saatlik gunluk ilk 5 taramasi hata verdi: %s", exc)
                await _notify_report_error("saatlik günlük ilk 5 radarı", exc)

        scheduler.add_job(
            _daily_top_picks_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour="10-17",
                minute=0,
                timezone="Europe/Istanbul",
            ),
            id="daily_top_five_long_scan",
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        logger.info("Gunluk ilk 5 kaliteli LONG radari her saat basi (10:00-17:00 Istanbul) hazirlandi.")

    # Compatibility mode only.  The previous EMA/RSI event stream remains
    # available when the new scenario radar is explicitly disabled, but it is
    # intentionally not run alongside it (no repetitive RSI alerts).
    # It uses the configured BIST JSON universe, never a hard-coded list.
    if getattr(settings, "technical_screener_enabled", False) and not getattr(settings, "trade_scenario_scan_enabled", True):
        async def _technical_screener_job() -> None:
            def _scan():
                from app.analysis.screener_engine import run_technical_screener
                from app.config.instruments import universe_symbols
                from app.data.provider_factory import build_market_data_provider
                from app.models.database import get_session_factory

                db = get_session_factory()()
                try:
                    return run_technical_screener(
                        db,
                        symbols=universe_symbols(settings.bist_universe_json_path),
                        provider_factory=lambda: build_market_data_provider(settings),
                        settings=settings,
                    )
                except Exception:
                    db.rollback()
                    raise
                finally:
                    db.close()

            try:
                result = await asyncio.to_thread(_scan)
                logger.info(
                    "EMA/RSI evren taramasi tamamlandi scanned=%s failed=%s alerts=%s",
                    result.scanned,
                    result.failed,
                    len(result.alerts),
                )
                if application is None or not result.alerts:
                    return
                from app.analysis.screener_engine import format_screener_alert
                from app.models.database import User, get_session_factory

                db = get_session_factory()()
                try:
                    configured = getattr(settings, "technical_screener_chat_id", None)
                    recipients = {int(configured)} if configured else {
                        int(user.telegram_user_id)
                        for user in db.query(User).filter(User.is_admin.is_(True)).all()
                    }
                    recipients.update(int(value) for value in getattr(settings, "admin_ids", ()))
                finally:
                    db.close()
                for alert in result.alerts:
                    text = format_screener_alert(alert)
                    for chat_id in recipients:
                        try:
                            await application.bot.send_message(chat_id=chat_id, text=text)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Teknik alarm gonderilemedi chat=%s: %s", chat_id, exc)
            except Exception as exc:  # noqa: BLE001 - scheduled jobs must survive
                logger.exception("EMA/RSI evren taramasi hata verdi: %s", exc)
                await _notify_report_error("EMA/RSI evren taraması", exc)

        scan_step = max(15, min(60, int(settings.technical_screener_interval_minutes)))
        scheduler.add_job(
            _technical_screener_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour="10-18",
                minute=f"*/{scan_step}",
                timezone="Europe/Istanbul",
            ),
            id="full_universe_ema_rsi_scan",
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        logger.info("Tam evren EMA/RSI taramasi %s dakikada bir hazirlandi.", scan_step)

    if getattr(settings, "intraday_vwap_scan_enabled", False) and not getattr(settings, "trade_scenario_scan_enabled", True):
        async def _intraday_vwap_scan_job() -> None:
            def _scan():
                from app.analysis.screener_engine import run_intraday_vwap_scan
                from app.config.instruments import universe_symbols
                from app.data.provider_factory import build_market_data_provider

                return run_intraday_vwap_scan(
                    symbols=universe_symbols(settings.bist_universe_json_path),
                    provider_factory=lambda: build_market_data_provider(settings),
                    settings=settings,
                )

            try:
                report = await asyncio.to_thread(_scan)
                if application is None:
                    return
                from app.analysis.screener_engine import format_intraday_scan_report
                from app.models.database import User, get_session_factory

                db = get_session_factory()()
                try:
                    configured = getattr(settings, "technical_screener_chat_id", None)
                    recipients = {int(configured)} if configured else {
                        int(user.telegram_user_id)
                        for user in db.query(User).filter(User.is_admin.is_(True)).all()
                    }
                    recipients.update(int(value) for value in getattr(settings, "admin_ids", ()))
                finally:
                    db.close()
                text = format_intraday_scan_report(report, timezone_name=settings.timezone_name)
                for chat_id in recipients:
                    try:
                        await application.bot.send_message(chat_id=chat_id, text=text)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("VWAP/VP tarama raporu gonderilemedi chat=%s: %s", chat_id, exc)
            except Exception as exc:  # noqa: BLE001
                logger.exception("VWAP/VP evren taramasi hata verdi: %s", exc)
                await _notify_report_error("30dk VWAP/Volume Profile taraması", exc)

        vwap_step = max(15, min(60, int(settings.intraday_vwap_scan_minute_step)))
        scheduler.add_job(
            _intraday_vwap_scan_job,
            CronTrigger(
                day_of_week="mon-fri",
                hour="10-17",
                minute=f"*/{vwap_step}",
                timezone="Europe/Istanbul",
            ),
            id="full_universe_vwap_volume_profile_scan",
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        logger.info("VWAP/Volume Profile evren taramasi %s dakikada bir hazirlandi.", vwap_step)

    if getattr(settings, "staged_entry_enabled", False):
        async def _staged_entry_monitor_job() -> None:
            def _monitor():
                from app.data.provider_factory import build_market_data_provider
                from app.models.database import get_session_factory
                from app.services.staged_entry_tracking_service import monitor_staged_entry_plans

                db = get_session_factory()()
                try:
                    provider = build_market_data_provider(settings)
                    return monitor_staged_entry_plans(db, provider, settings)
                except Exception:
                    db.rollback()
                    raise
                finally:
                    db.close()

            try:
                result = await asyncio.to_thread(_monitor)
                if any(result.get(key) for key in ("confirmed", "filled", "invalidated", "errors")):
                    logger.info("Kademeli entry monitor sonucu: %s", result)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Kademeli entry monitor hata verdi: %s", exc)

        async def _staged_entry_delivery_job() -> None:
            if application is None:
                return
            from app.models.database import get_session_factory
            from app.services.staged_entry_tracking_service import (
                mark_staged_event_failed,
                mark_staged_event_sent,
                pending_staged_entry_events,
            )

            db = get_session_factory()()
            try:
                for event in pending_staged_entry_events(db):
                    try:
                        await application.bot.send_message(
                            chat_id=event.telegram_chat_id,
                            text=event.message_text,
                        )
                        mark_staged_event_sent(db, event)
                    except Exception as exc:  # noqa: BLE001 - outbox retries next cycle
                        db.rollback()
                        event = db.get(type(event), event.id)
                        if event is not None:
                            mark_staged_event_failed(db, event)
                        logger.warning("Kademe bildirimi gonderilemedi event=%s: %s", getattr(event, "id", None), exc)
            finally:
                db.close()

        staged_poll = max(15, int(getattr(settings, "user_price_alert_poll_seconds", 30)))
        scheduler.add_job(
            _staged_entry_monitor_job,
            IntervalTrigger(seconds=staged_poll),
            id="staged_entry_monitor",
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        scheduler.add_job(
            _staged_entry_delivery_job,
            IntervalTrigger(seconds=5),
            id="staged_entry_delivery",
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        logger.info("Kademeli entry monitor/outbox isleri scheduler'a eklendi.")

    # Kullanici fiyat alarmlari kapanis taramasindan bagimsizdir. Monitor ag
    # erisimini worker thread'de yapar; teslimat ayri bir outbox isi oldugu icin
    # Telegram gecici olarak hata verse bile tetik olayi kaybolmaz.
    if getattr(settings, "user_price_alerts_enabled", True):
        async def _user_price_alert_monitor_job() -> None:
            def _run_monitor_cycle() -> dict:
                from app.alerts.monitor import run_alarm_monitor_cycle
                from app.data.provider_factory import build_market_data_provider
                from app.models.database import get_session_factory

                db = get_session_factory()()
                try:
                    provider = build_market_data_provider(settings)
                    return run_alarm_monitor_cycle(db, provider, settings)
                except Exception:
                    db.rollback()
                    raise
                finally:
                    db.close()

            try:
                result = await asyncio.to_thread(_run_monitor_cycle)
                if result.get("triggered") or result.get("repeats_queued"):
                    logger.info("Fiyat alarm monitor sonucu: %s", result)
            except Exception as exc:  # noqa: BLE001 - diger scheduler islerini durdurmamali
                logger.error("Fiyat alarm monitor dongusu hata verdi: %s", exc)

        async def _user_price_alert_delivery_job() -> None:
            if application is None:
                return
            from app.alerts.delivery import deliver_alarm_outbox
            from app.models.database import get_session_factory

            db = get_session_factory()()
            try:
                result = await deliver_alarm_outbox(application, db, settings)
                if result.get("sent") or result.get("retry") or result.get("failed"):
                    logger.info("Fiyat alarm teslimat sonucu: %s", result)
            except Exception as exc:  # noqa: BLE001 - outbox bir sonraki dongude tekrar denenir
                db.rollback()
                logger.error("Fiyat alarm teslimat dongusu hata verdi: %s", exc)
            finally:
                db.close()

        scheduler.add_job(
            _user_price_alert_monitor_job,
            IntervalTrigger(
                seconds=max(5, int(getattr(settings, "user_price_alert_poll_seconds", 30)))
            ),
            id="user_price_alert_monitor",
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        scheduler.add_job(
            _user_price_alert_delivery_job,
            IntervalTrigger(
                seconds=max(
                    1,
                    int(getattr(settings, "user_price_alert_delivery_poll_seconds", 5)),
                )
            ),
            id="user_price_alert_delivery",
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        logger.info("Kalici fiyat alarm monitor ve teslimat isleri scheduler'a eklendi.")

    # Veri kaynağından bağımsız, DB-backed BIST sinyal yaşam döngüsü. Quote
    # güncellik/işlem/timestamp alanlarından biri eksikse monitor fail-closed
    # davranır; gecikmeli kapanış fiyatıyla giriş, TP veya stop üretmez.
    if getattr(settings, "signal_monitor_enabled", False):
        async def _bist_signal_monitor_job() -> None:
            def _run_signal_cycle() -> dict:
                from app.data.provider_factory import build_market_data_provider
                from app.models.database import get_session_factory
                from app.services.bist_signal_monitor_service import run_signal_monitor_cycle

                db = get_session_factory()()
                try:
                    provider = build_market_data_provider(settings)
                    return run_signal_monitor_cycle(db, provider, settings)
                except Exception:
                    db.rollback()
                    raise
                finally:
                    db.close()

            try:
                result = await asyncio.to_thread(_run_signal_cycle)
                if result.get("updated") or result.get("queued") or result.get("errors"):
                    logger.info("BIST sinyal monitor sonucu: %s", result)
            except Exception as exc:  # noqa: BLE001 - scheduler yaşamaya devam eder
                logger.error("BIST sinyal monitor döngüsü hata verdi: %s", exc)

        async def _bist_signal_delivery_job() -> None:
            if application is None:
                return
            from app.models.database import get_session_factory
            from app.services.bist_signal_monitor_service import deliver_signal_event_outbox

            db = get_session_factory()()
            try:
                result = await deliver_signal_event_outbox(application, db, settings)
                if result.get("sent") or result.get("retry") or result.get("failed"):
                    logger.info("BIST sinyal teslimat sonucu: %s", result)
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                logger.error("BIST sinyal teslimat döngüsü hata verdi: %s", exc)
            finally:
                db.close()

        scheduler.add_job(
            _bist_signal_monitor_job,
            IntervalTrigger(
                seconds=max(1, int(getattr(settings, "signal_monitor_interval_seconds", 5)))
            ),
            id="bist_signal_monitor",
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        scheduler.add_job(
            _bist_signal_delivery_job,
            IntervalTrigger(seconds=5),
            id="bist_signal_delivery",
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        logger.info("BIST sinyal monitor ve olay teslimat işleri scheduler'a eklendi.")

    if getattr(settings, "enhanced_alarm_scan_enabled", False):
        async def _enhanced_alarm_job() -> None:
            from app.data.provider_factory import build_market_data_provider
            from app.models.database import get_session_factory
            from app.services.enhanced_alert_service import scan_enhanced_alarms

            db = get_session_factory()()
            try:
                provider = build_market_data_provider(settings)
                await scan_enhanced_alarms(application, db, provider, settings)
            except Exception as exc:  # noqa: BLE001
                logger.error("Gelişmiş alarm taraması hata verdi: %s", exc)
            finally:
                db.close()

        scheduler.add_job(
            _enhanced_alarm_job,
            IntervalTrigger(minutes=max(1, int(getattr(settings, "enhanced_alarm_scan_minutes", 15)))),
            id="enhanced_alarm_scan",
            coalesce=True,
            max_instances=1,
        )
        logger.info("Gelişmiş alarm taraması scheduler'a eklendi.")

    stage5g_scan_minutes = getattr(settings, "paper_trading_scan_minutes", None)
    if stage5g_scan_minutes is not None:
        async def _stage5g_tracking_job() -> None:
            def _scan_in_worker() -> None:
                from app.data.provider_factory import build_market_data_provider
                from app.execution.paper_trading_engine import run_paper_trade_scan
                from app.models.database import get_session_factory
                from app.services.signal_outcome_tracker import run_signal_outcome_scan
                from app.analysis.score_calibration_engine import run_calibration_training

                db = get_session_factory()()
                try:
                    provider = build_market_data_provider(settings)
                    run_paper_trade_scan(db, provider)
                    run_signal_outcome_scan(db, provider)
                    run_calibration_training(
                        db,
                        minimum_sample_size=int(getattr(settings, "calibration_minimum_sample_size", 30)),
                    )
                except Exception:
                    db.rollback()
                    raise
                finally:
                    db.close()

            try:
                await asyncio.to_thread(_scan_in_worker)
            except Exception as exc:  # takip hatasi botun diger ozelliklerini etkilemez
                logger.error("5g sanal islem/sinyal takip isi hata verdi: %s", exc)

        scheduler.add_job(
            _stage5g_tracking_job,
            IntervalTrigger(minutes=max(1, int(stage5g_scan_minutes))),
            id="stage5g_paper_and_signal_tracking",
            coalesce=True,
            max_instances=1,
        )
        logger.info("5g sanal islem ve sinyal sonuc takip isi scheduler'a eklendi.")

    return scheduler


def _register_scheduler_lifecycle(application: Application, settings) -> None:
    """Scheduler'in baslatilmasini/kapatilmasini Application yasam dongusune bagla.

    - post_init: event loop calismaya basladiktan HEMEN SONRA tetiklenir, scheduler
      burada guvenle start() edilebilir.
    - post_shutdown: bot kapanirken scheduler'i duzgunce durdurur.
    - Iki kere baslatmayi engellemek icin application.bot_data["scheduler_started"]
      bayragi kullanilir.
    """

    scheduler = _build_evening_scan_scheduler(settings, application=application)
    application.bot_data["scheduler"] = scheduler
    application.bot_data["scheduler_started"] = False

    if scheduler is None:
        return

    async def _on_post_init(app: Application) -> None:
        sched = app.bot_data.get("scheduler")
        if sched is None:
            return
        if app.bot_data.get("scheduler_started"):
            logger.info("Scheduler zaten baslatilmis, tekrar baslatilmiyor.")
            return
        from app.models.database import get_session_factory
        from app.services.backtest_job_service import BacktestJobService
        interrupted = BacktestJobService.mark_interrupted_runs(get_session_factory())
        if interrupted:
            logger.warning("Yeniden baslatmada %s yarim backtest INTERRUPTED yapildi.", interrupted)
        sched.start()
        app.bot_data["scheduler_started"] = True
        from app.services.health_service import mark_runtime_health
        mark_runtime_health("scheduler", "running")
        logger.info("Arka plan scheduler'i basariyla baslatildi.")

    async def _on_post_shutdown(app: Application) -> None:
        sched = app.bot_data.get("scheduler")
        if sched is not None and app.bot_data.get("scheduler_started"):
            sched.shutdown(wait=False)
            app.bot_data["scheduler_started"] = False
            from app.services.health_service import mark_runtime_health
            mark_runtime_health("scheduler", "stopped")
            logger.info("Scheduler bot kapanisinda guvenli sekilde durduruldu.")

    application.post_init = _on_post_init
    application.post_shutdown = _on_post_shutdown


def run_polling() -> None:
    """Yerel gelistirme icin polling modunda botu baslatir."""
    settings = get_settings()
    application = build_telegram_application()
    _register_scheduler_lifecycle(application, settings)
    logger.info("Telegram bot polling modunda basliyor...")
    application.run_polling(allowed_updates=["message", "callback_query"])
