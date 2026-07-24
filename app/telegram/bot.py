from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from app.config.settings import get_settings
from app.telegram import handlers, handlers_stage5g, handlers_v3

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
    application.add_handler(CommandHandler("haber_detay", handlers_v3.cmd_haber_detay))
    application.add_handler(CommandHandler("haber_radari", handlers_v3.cmd_haber_radari))
    application.add_handler(CommandHandler("ai_aciklama", handlers_v3.cmd_ai_aciklama))
    application.add_handler(CommandHandler("sirket", handlers_v3.cmd_sirket))
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
    application.add_handler(CommandHandler("aksam_raporu", handlers_v3.cmd_aksam_raporu))
    application.add_handler(CommandHandler("tarama_ayarlari", handlers_v3.cmd_tarama_ayarlari))

    # ---- V3: Sinyal gecmisi / performans ----
    application.add_handler(CommandHandler("sinyaller", handlers_v3.cmd_sinyaller))
    application.add_handler(CommandHandler("aktif_sinyaller", handlers_v3.cmd_aktif_sinyaller))
    application.add_handler(CommandHandler("sinyal", handlers_v3.cmd_sinyal))
    application.add_handler(CommandHandler("sinyal_gecmisi", handlers_v3.cmd_sinyal_gecmisi))
    application.add_handler(CommandHandler("performans", handlers_v3.cmd_performans))

    # ---- V3: Portfoy genisletmeleri ----
    application.add_handler(CommandHandler("pozisyon_ekle", handlers_v3.cmd_pozisyon_ekle))
    application.add_handler(CommandHandler("pozisyon_sil", handlers_v3.cmd_pozisyon_sil))
    application.add_handler(CommandHandler("pozisyon_guncelle", handlers_v3.cmd_pozisyon_guncelle))
    application.add_handler(CommandHandler("portfoy_risk", handlers_v3.cmd_portfoy_risk))
    application.add_handler(CommandHandler("pozisyon_boyutu", handlers_v3.cmd_pozisyon_boyutu))
    application.add_handler(CommandHandler("maliyet", handlers_v3.cmd_maliyet))
    application.add_handler(CommandHandler("nakit_ayarla", handlers_v3.cmd_nakit_ayarla))
    application.add_handler(CommandHandler("sermaye_ayarla", handlers_v3.cmd_sermaye_ayarla))

    # ---- V3: Alarmlar ----
    application.add_handler(CommandHandler("alarm_kur", handlers_v3.cmd_alarm_kur))
    application.add_handler(CommandHandler("alarm", handlers_v3.cmd_alarm))
    application.add_handler(CommandHandler("alarm_test", handlers_v3.cmd_alarm_test))
    application.add_handler(CommandHandler("alarmlar", handlers_v3.cmd_alarmlar))
    application.add_handler(CommandHandler("alarm_sil", handlers_v3.cmd_alarm_sil))
    application.add_handler(CommandHandler("alarm_durdur", handlers_v3.cmd_alarm_durdur))
    application.add_handler(CommandHandler("alarm_ac", handlers_v3.cmd_alarm_ac))
    application.add_handler(CommandHandler("alarm_detay", handlers_v3.cmd_alarm_detay))

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

    # ---- V3: Inline buton callback'leri ----
    application.add_handler(CallbackQueryHandler(handlers_v3.handle_detail_callback, pattern=r"^detay_"))
    application.add_handler(CallbackQueryHandler(handlers_v3.handle_stage5f_callback, pattern=r"^stage5f_"))
    application.add_handler(CallbackQueryHandler(handlers_v3.handle_menu_callback, pattern=r"^menu_"))
    application.add_handler(CallbackQueryHandler(handlers_stage5g.handle_stage5g_callback, pattern=r"^stage5g_"))

    return application


def _build_evening_scan_scheduler(settings, application: Application | None = None) -> AsyncIOScheduler | None:
    """Kapanis sonrasi otomatik tarama icin APScheduler nesnesini olusturur.

    Onemli: Bu fonksiyon scheduler'i OLUSTURUR ama BASLATMAZ. AsyncIOScheduler.start()
    calisan bir asyncio event loop'u gerektirir; run_polling() cagrilmadan once
    (yani event loop baslamadan once) start() cagrilirsa
    "RuntimeError: no running event loop" hatasi olusur. Bu yuzden baslatma islemi
    application.post_init callback'ine tasinmistir (bkz. _register_scheduler_lifecycle),
    o an event loop kesin olarak calisir durumdadir.

    Config uzerinden acilip kapatilabilir (CLOSE_SCAN_ENABLED=false ile
    devre disi birakilabilir).

    `application` verilirse (gercek calisma zamaninda), aksam taramasi ve gun
    ici anomali taramasi sonuclarina gore kullanicilara OTOMATIK Telegram
    bildirimi + grafik gonderilir (bolum 8). Testlerde application=None
    gecilir; bu durumda taramalar sessizce calisir, hicbir mesaj gonderilmez.
    """
    if not settings.close_scan_enabled:
        logger.info("Otomatik aksam taramasi devre disi (CLOSE_SCAN_ENABLED=false).")
        return None

    try:
        hour_str, minute_str = settings.close_scan_time.split(":")
    except ValueError:
        logger.warning("CLOSE_SCAN_TIME formati hatali (%s); otomatik tarama kurulmadi.", settings.close_scan_time)
        return None

    scheduler = AsyncIOScheduler(timezone=settings.timezone_name)

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

    scheduler.add_job(_job, CronTrigger(hour=int(hour_str), minute=int(minute_str)))
    logger.info(
        "Otomatik aksam taramasi %s (%s) icin hazirlandi (event loop baslayinca aktif olacak).",
        settings.close_scan_time,
        settings.timezone_name,
    )

    if getattr(settings, "daily_brief_enabled", False):
        async def _daily_brief_job() -> None:
            from app.data.provider_factory import build_market_data_provider
            from app.models.database import User, get_session_factory
            from app.services.daily_market_report_service import build_daily_market_report, format_daily_market_report

            db = get_session_factory()()
            try:
                provider = build_market_data_provider(settings)
                report = await asyncio.to_thread(build_daily_market_report, provider, settings)
                text = format_daily_market_report(report)
                if application is not None:
                    for user in db.query(User).filter(User.kill_switch_active.is_(False)).all():
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
                CronTrigger(day_of_week="mon-fri", hour=int(brief_hour), minute=int(brief_minute)),
                id="daily_market_brief",
                coalesce=True,
                max_instances=1,
            )
        except (TypeError, ValueError):
            logger.warning("DAILY_BRIEF_TIME formatı geçersiz: %s", settings.daily_brief_time)

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
        logger.info("Otomatik aksam taramasi scheduler'i basariyla baslatildi.")

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
