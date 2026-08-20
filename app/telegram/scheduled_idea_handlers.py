from __future__ import annotations

"""Manual controls for the scheduled two-idea and follow-up cards."""

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from app.config.instruments import universe_symbols
from app.config.settings import get_settings
from app.data.provider_factory import build_market_data_provider
from app.fundamentals.factory import build_fundamental_provider
from app.models.database import get_session_factory
from app.services.scheduled_idea_service import (
    evaluate_due_ideas,
    format_idea_performance_report,
    format_scheduled_ideas_report,
    persist_scheduled_ideas,
)
from app.telegram.handlers import _reject_unauthorized


async def cmd_oneriler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the same two-plan scanner manually, retaining the audit record."""

    if await _reject_unauthorized(update) or update.message is None:
        return
    settings = get_settings()
    await update.message.reply_text(
        "📍 İki Aday Plan Radarı çalışıyor\n"
        "600 hisse üzerinde formasyon, çoklu teyit, retest bölgesi ve en az 1:2 RR kapıları kontrol ediliyor."
    )

    def scan_and_save():
        from app.analysis.screener_engine import run_daily_top_picks_scan

        report = run_daily_top_picks_scan(
            symbols=universe_symbols(settings.bist_universe_json_path),
            provider_factory=lambda: build_market_data_provider(settings),
            fundamental_provider_factory=lambda: build_fundamental_provider(settings),
            settings=settings,
        )
        db = get_session_factory()()
        try:
            persist_scheduled_ideas(
                db,
                report=report,
                slot="manual",
                maximum=settings.scheduled_ideas_max_results,
            )
        finally:
            db.close()
        return report

    try:
        report = await asyncio.to_thread(scan_and_save)
        await update.message.reply_text(
            format_scheduled_ideas_report(
                report,
                slot="afternoon",
                timezone_name=settings.timezone_name,
                maximum=settings.scheduled_ideas_max_results,
            )
        )
    except Exception:
        await update.message.reply_text(
            "⚠️ İki aday radarı şu an tamamlanamadı. Veri doğrulanamadığı için aday üretilmedi."
        )


async def cmd_oneri_performans(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update) or update.message is None:
        return
    settings = get_settings()

    def evaluate():
        db = get_session_factory()()
        try:
            return evaluate_due_ideas(
                db,
                provider=build_market_data_provider(settings),
                minimum_age_days=settings.scheduled_idea_performance_days,
            )
        finally:
            db.close()

    try:
        items = await asyncio.to_thread(evaluate)
        await update.message.reply_text(
            format_idea_performance_report(items, timezone_name=settings.timezone_name)
        )
    except Exception:
        await update.message.reply_text(
            "⚠️ Plan performansı doğrulanamadı; kapanmış-bar verisi gelmediği için sonuç yazılmadı."
        )
