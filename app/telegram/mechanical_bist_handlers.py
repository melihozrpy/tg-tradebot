from __future__ import annotations

"""Telegram endpoints for the closed-candle mechanical BIST setup engine."""

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.analysis.mechanical_bist_engine import analyze_mechanical_bist_setup, format_mechanical_bist_report
from app.config.instruments import normalize_instrument
from app.config.settings import get_settings
from app.data.provider_factory import build_market_data_provider
from app.telegram.handlers import _reject_unauthorized

logger = logging.getLogger("mergen_quant.telegram.mechanical_bist")


async def cmd_mekanik_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return a readable closed-candle plan for one BIST share."""

    if await _reject_unauthorized(update) or update.message is None:
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /mekanik THYAO\nÇıktı: Daily→4H→1H/15dk, net seviye ve koşullu işlem planı.")
        return
    try:
        symbol = normalize_instrument(context.args[0])
    except ValueError:
        await update.message.reply_text("Geçerli bir BIST kodu yaz: /mekanik THYAO")
        return
    settings = get_settings()
    if not bool(getattr(settings, "mechanical_setup_enabled", True)):
        await update.message.reply_text("Mekanik setup motoru bu kurulumda kapalı.")
        return
    await update.message.reply_text("⚙️ Kapanmış mumlarla Daily → 4H → 1H/15dk mekanik yapı hesaplanıyor…")
    try:
        payload = await asyncio.to_thread(
            analyze_mechanical_bist_setup,
            symbol,
            provider=build_market_data_provider(settings),
            settings=settings,
        )
        rendered = format_mechanical_bist_report(payload, timezone_name=settings.timezone_name)
        await update.message.reply_text(rendered, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as exc:  # one unavailable timeframe must not crash polling
        logger.info("Mekanik setup üretilemedi symbol=%s error=%s", symbol, type(exc).__name__)
        await update.message.reply_text(
            "⚠️ Mekanik setup üretilemedi: Daily, 1H ve 15dk kapanmış OHLCV verisinin tamamı doğrulanamadı."
        )


async def cmd_mekanik_kurallar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update) or update.message is None:
        return
    await update.message.reply_text(
        "⚙️ MEKANİK BIST SETUP\n\n"
        "• Daily bias → 4H yapı → 1H seviye → 15dk tetik sırası zorunlu.\n"
        "• Önceki gün/hafta/ay seviyeleri, 20g tepe ve 5g dip JSON koordinatlarıyla gelir.\n"
        "• FVG/OB, yalnız kapanmış mumlardan çıkarılır.\n"
        "• Sinyal yalnız 15dk break-retest veya sweep teyidi + likidite kapısı + yeterli RR ile ACTIVE olur.\n"
        "• BIST spotta SELL açığa satış çağrısı değildir; azaltma/koruma çerçevesidir.\n"
        "• Günlük risk varsayılanı işlem başına %0,25; iki kayıpta gün sonlandırılmalıdır.\n\n"
        "Kullanım: /mekanik THYAO"
    )
