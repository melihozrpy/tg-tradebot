from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.data.base_provider import BaseMarketDataProvider
from app.models.database import PriceAlert, User, WatchlistItem
from app.services.alert_service import evaluate_alert
from app.services.anomaly_service import run_symbol_anomaly_scan

logger = logging.getLogger("mergen_quant.notifications")

MAX_DAILY_CHART_NOTIFICATIONS_PER_RUN = 15
MAX_INTRADAY_CHART_NOTIFICATIONS_PER_RUN = 10


async def _send_chart_for_symbol(bot, chat_id: int, provider: BaseMarketDataProvider, symbol: str, outcome_like, caption: str, period_days: int = 250) -> None:
    """Grafik uretip Telegram'a gonderir; grafik uretilemezse yalnizca metin gonderilir
    (bildirim akisi asla grafik yuzunden tamamen durmaz)."""
    from app.services.chart_service import delete_chart_file, generate_price_chart

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=period_days)
    chart_path = None
    try:
        df = provider.get_ohlcv(symbol, "1d", start, end)
        chart_path = await asyncio.to_thread(
            generate_price_chart,
            df, symbol,
            sr=getattr(outcome_like, "support_resistance", None),
            entry_zone=getattr(outcome_like, "entry_zone", None),
            stop_price=getattr(outcome_like, "stop_price", None),
            targets=[getattr(outcome_like, "target_1", None), getattr(outcome_like, "target_2", None), getattr(outcome_like, "target_3", None)],
        )
        with open(chart_path, "rb") as f:
            await bot.send_photo(chat_id=chat_id, photo=f, caption=caption)
    except Exception as exc:  # noqa: BLE001 - bildirim gonderimi asla botu cokertmemeli
        logger.warning("Otomatik grafik gonderilemedi symbol=%s chat_id=%s: %s", symbol, chat_id, exc)
        try:
            await bot.send_message(chat_id=chat_id, text=caption)
        except Exception:  # noqa: BLE001
            pass
    finally:
        if chart_path:
            delete_chart_file(chart_path)


async def notify_daily_top_candidates(application, db: Session, provider: BaseMarketDataProvider, summary) -> int:
    """Aksam taramasi sonrasi, izleme listesinde en iyi/en riskli aday bulunan
    kullanicilara otomatik olarak GUNLUK grafik + kisa ozet gonderir (bolum 8:
    gunluk otomatik grafikler). Uygulama (application) yoksa (test/CLI ortami)
    sessizce atlanir."""
    if application is None:
        return 0

    candidate_map = {sym: outcome for sym, outcome in summary.top_candidates}
    if not candidate_map:
        return 0

    symbols = list(candidate_map.keys())
    rows = (
        db.query(WatchlistItem.user_id, WatchlistItem.symbol)
        .filter(WatchlistItem.symbol.in_(symbols), WatchlistItem.is_muted.is_(False))
        .all()
    )
    if not rows:
        return 0

    user_ids = {r[0] for r in rows}
    users = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}

    sent = 0
    for user_id, symbol in rows:
        if sent >= MAX_DAILY_CHART_NOTIFICATIONS_PER_RUN:
            break
        user = users.get(user_id)
        if user is None or user.kill_switch_active:
            continue
        outcome = candidate_map[symbol]
        caption = (
            f"🌙 GÜNLÜK OTOMATİK GRAFİK — {symbol}\n"
            f"Sinyal: {outcome.signal.signal_type} — Skor: {outcome.advanced_score.total}/100\n"
            "Bu mesaj yatırım tavsiyesi değildir."
        )
        try:
            await _send_chart_for_symbol(application.bot, user.telegram_user_id, provider, symbol, outcome.signal, caption)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gunluk otomatik bildirim gonderilemedi user=%s symbol=%s: %s", user_id, symbol, exc)

    return sent


async def scan_and_notify_anomalies(
    application,
    db: Session,
    provider: BaseMarketDataProvider,
    symbols: list[str],
    timeframe: str = "1d",
    send_charts: bool = True,
) -> int:
    """Verilen semboller icin anomali taramasi calistirir; YENI tespit edilen
    her anomali icin, o sembolu izleyen ve aktif 'anomali' alarmi olan
    kullanicilara bildirim (mumkunse grafikle birlikte) gonderir."""
    total_notified = 0
    chart_budget = MAX_INTRADAY_CHART_NOTIFICATIONS_PER_RUN

    for symbol in symbols:
        try:
            outcome = run_symbol_anomaly_scan(db, provider, symbol, timeframe=timeframe)
        except Exception as exc:  # noqa: BLE001 - bir sembol hatasi tum taramayi durdurmamali
            logger.warning("Anomali taramasi basarisiz symbol=%s: %s", symbol, exc)
            continue

        if not outcome.new_anomalies:
            continue

        watchers = (
            db.query(WatchlistItem.user_id)
            .filter(WatchlistItem.symbol == symbol, WatchlistItem.is_muted.is_(False))
            .all()
        )
        watcher_ids = {w[0] for w in watchers}
        if not watcher_ids:
            continue

        for anomaly in outcome.new_anomalies:
            alerts = (
                db.query(PriceAlert)
                .filter(
                    PriceAlert.symbol == symbol,
                    PriceAlert.alert_type == "anomali",
                    PriceAlert.is_active.is_(True),
                    PriceAlert.user_id.in_(watcher_ids),
                )
                .all()
            )
            for alert in alerts:
                message = evaluate_alert(
                    db, alert, anomaly_type=anomaly.anomaly_type, anomaly_description=anomaly.description
                )
                if message is None:
                    continue
                user = db.query(User).filter(User.id == alert.user_id).first()
                if user is None:
                    continue
                total_notified += 1
                if application is None:
                    continue
                if send_charts and chart_budget > 0:
                    chart_budget -= 1
                    await _send_chart_for_symbol(
                        application.bot, user.telegram_user_id, provider, symbol, outcome.result,
                        caption=f"🚨 {message}", period_days=90,
                    )
                else:
                    try:
                        await application.bot.send_message(chat_id=user.telegram_user_id, text=f"🚨 {message}")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Anomali bildirimi gonderilemedi user=%s: %s", user.id, exc)

    return total_notified
