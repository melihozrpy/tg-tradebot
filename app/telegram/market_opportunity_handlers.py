from __future__ import annotations

"""Telegram handlers for compact, universe-wide opportunity classifications."""

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.analysis.screener_engine import (
    format_daily_top_picks_report,
    format_market_opportunity_report,
    run_daily_top_picks_scan,
    run_market_opportunity_scan,
)
from app.config.instruments import universe_symbols
from app.config.settings import get_settings
from app.data.provider_factory import build_market_data_provider
from app.fundamentals.factory import build_fundamental_provider
from app.telegram.handlers import _reject_unauthorized

logger = logging.getLogger("mergen_quant.telegram.market_opportunities")


_TIMEFRAME_ALIASES = {
    "5": "5m",
    "5m": "5m",
    "5dk": "5m",
    "5dakika": "5m",
    "5dakikalik": "5m",
    "1h": "1h",
    "1s": "1h",
    "1saat": "1h",
    "1saatlik": "1h",
    "saatlik": "1h",
    "4h": "4h",
    "4s": "4h",
    "4saat": "4h",
    "4saatlik": "4h",
}
_TIMEFRAME_LABELS = {"5m": "5 dakika", "1h": "1 saat", "4h": "4 saat"}


def parse_firsatlar_timeframe(args: list[str]) -> str | None:
    """Parse user-friendly `/firsatlar 5dk|1s|4s` timeframe arguments."""

    if not args:
        return "1h"
    raw = "".join(args).strip().casefold().replace(" ", "")
    return _TIMEFRAME_ALIASES.get(raw)


async def cmd_firsatlar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List strict ten-indicator opportunities for one selected timeframe."""

    if await _reject_unauthorized(update) or update.message is None:
        return
    timeframe = parse_firsatlar_timeframe(list(context.args or []))
    if timeframe is None:
        await update.message.reply_text(
            "Kullanım: /firsatlar 5dk  |  /firsatlar 1s  |  /firsatlar 4s\n"
            "Varsayılan zaman dilimi: 1 saat. Her tarama 10 göstergenin tamamını kontrol eder."
        )
        return
    settings = get_settings()
    await update.message.reply_text(
        f"🔎 Tam BIST evreni { _TIMEFRAME_LABELS[timeframe] } için 10 göstergeden geçiriliyor...\n"
        f"Yalnız en az {getattr(settings, 'market_opportunity_minimum_confluence', 5)}/10 bağımsız teyitli adaylar listelenecek."
    )

    def scan():
        return run_market_opportunity_scan(
            symbols=universe_symbols(settings.bist_universe_json_path),
            provider_factory=lambda: build_market_data_provider(settings),
            settings=settings,
            timeframe=timeframe,
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


async def cmd_gunluk5(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the same hourly daily top-five screen on demand."""

    if await _reject_unauthorized(update) or update.message is None:
        return
    settings = get_settings()
    await update.message.reply_text(
        "🏆 Günlük İlk 5 radarı tüm BIST evrenini tarıyor.\n"
        "Sadece doğrulanmış formasyon, teknik teyit, ≥%3 hedef potansiyeli ve temel kalite koşullarını birlikte geçenler dönecek; zorla 5 hisse yazılmaz."
    )

    def scan():
        return run_daily_top_picks_scan(
            symbols=universe_symbols(settings.bist_universe_json_path),
            provider_factory=lambda: build_market_data_provider(settings),
            fundamental_provider_factory=lambda: build_fundamental_provider(settings),
            settings=settings,
        )

    try:
        report = await asyncio.to_thread(scan)
        text = format_daily_top_picks_report(report, timezone_name=settings.timezone_name)
        if text:
            await update.message.reply_text(text, disable_web_page_preview=True)
    except Exception as exc:  # noqa: BLE001 - a command failure cannot crash the bot
        logger.exception("Gunluk5 komutu taramasi hata verdi: %s", exc)
        await update.message.reply_text(
            "⚠️ Günlük İlk 5 taraması tamamlanamadı. Veri kaynağı tekrar denenecek; boş veya tahmini liste gönderilmedi."
        )
