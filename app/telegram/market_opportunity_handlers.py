from __future__ import annotations

"""Telegram handlers for compact, universe-wide opportunity classifications."""

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.analysis.screener_engine import format_market_opportunity_report, run_market_opportunity_scan
from app.config.instruments import universe_symbols
from app.config.settings import get_settings
from app.data.provider_factory import build_market_data_provider
from app.telegram.handlers import _reject_unauthorized

logger = logging.getLogger("mergen_quant.telegram.market_opportunities")


async def cmd_firsatlar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List technical trade, momentum, long-watch and volatility-risk groups."""

    if await _reject_unauthorized(update) or update.message is None:
        return
    settings = get_settings()
    await update.message.reply_text(
        "🔎 Tam BIST evreni teknik filtreleniyor...\n"
        "Bu işlem yalnız 3+ teyitli adayları ve yüksek oynaklık riskini ayırır."
    )

    def scan():
        return run_market_opportunity_scan(
            symbols=universe_symbols(settings.bist_universe_json_path),
            provider_factory=lambda: build_market_data_provider(settings),
            settings=settings,
        )

    try:
        report = await asyncio.to_thread(scan)
        await update.message.reply_text(
            format_market_opportunity_report(report, timezone_name=settings.timezone_name),
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001 - a source failure cannot crash the bot
        logger.exception("Firsatlar komutu taramasi hata verdi: %s", exc)
        await update.message.reply_text(
            "⚠️ Fırsat taraması şu an tamamlanamadı. Veri kaynağı veya piyasa verisi tekrar denenecek."
        )
