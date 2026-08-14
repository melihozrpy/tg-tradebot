from __future__ import annotations

"""Telegram endpoints for the strict two-candidate BIST spot radar."""

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from app.analysis.baby_stock_engine import (
    format_baby_stock_report,
    format_baby_stock_settings,
    risk_profile_from_settings,
    run_baby_stock_scan,
)
from app.config.instruments import normalize_instrument, universe_symbols
from app.config.settings import get_settings
from app.data.provider_factory import build_market_data_provider
from app.fundamentals.factory import build_fundamental_provider
from app.telegram.handlers import _reject_unauthorized

logger = logging.getLogger("mergen_quant.telegram.baby_stock")


def parse_baby_stock_capital(args: list[str], *, default: float) -> float:
    """Accept `200000`, `200.000`, `200,000` and `200k` without guessing."""

    if not args:
        return default
    raw = str(args[0]).strip().casefold().replace("tl", "").replace("₺", "").strip()
    multiplier = 1.0
    if raw.endswith("k"):
        multiplier = 1_000.0
        raw = raw[:-1]
    compact = raw.replace(".", "").replace(",", "")
    try:
        value = float(compact) * multiplier
    except ValueError as exc:
        raise ValueError("Sermayeyi örneğin 200000 veya 200k biçiminde yaz.") from exc
    if not 1_000 <= value <= 100_000_000:
        raise ValueError("Sermaye 1.000 TL ile 100.000.000 TL arasında olmalı.")
    return value


async def _send_baby_scan(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    symbol: str | None = None,
    capital_args: list[str] | None = None,
) -> None:
    if await _reject_unauthorized(update) or update.message is None:
        return
    settings = get_settings()
    if not bool(getattr(settings, "baby_stock_enabled", True)):
        await update.message.reply_text("Bebek Hisse radarı bu kurulumda kapalı.")
        return
    try:
        capital = parse_baby_stock_capital(
            list(capital_args or []),
            default=float(getattr(settings, "baby_stock_default_capital", 200_000)),
        )
    except ValueError as exc:
        await update.message.reply_text(f"⚠️ {exc}")
        return
    scope = f"{symbol} için" if symbol else "tam BIST evreninde"
    await update.message.reply_text(
        "🧸 Bebek Hisse Pro Radarı başlıyor\n"
        f"{scope} önce 1 saatlik filtre, ardından günlük + 15dk + 5dk teyit ve likidite kontrolü yapılıyor.\n"
        "Yalnız koşullu spot LONG planı geçerse en fazla iki aday dönecek; uygun aday yoksa zorla hisse seçilmeyecek."
    )

    def scan():
        return run_baby_stock_scan(
            symbols=[symbol] if symbol else universe_symbols(settings.bist_universe_json_path),
            provider_factory=lambda: build_market_data_provider(settings),
            settings=settings,
            capital=capital,
            requested_symbol=symbol,
            fundamental_provider_factory=lambda: build_fundamental_provider(settings),
        )

    try:
        report = await asyncio.to_thread(scan)
        await update.message.reply_text(
            format_baby_stock_report(report, timezone_name=settings.timezone_name),
            disable_web_page_preview=True,
        )
    except Exception as exc:  # a provider failure must not take down the bot
        logger.exception("Bebek Hisse taraması tamamlanamadı: %s", exc)
        await update.message.reply_text(
            "⚠️ Bebek Hisse taraması şu an tamamlanamadı. Veri sağlayıcısı yeniden denenecek; doğrulanmamış aday gönderilmedi."
        )


async def cmd_bebekhisse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scan the full active BIST universe for at most two strict spot plans."""

    await _send_baby_scan(update, context, capital_args=list(context.args or []))


async def cmd_bebekhisse_kontrol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the same gates for one user-specified BIST ticker."""

    if not context.args:
        if update.message is not None:
            await update.message.reply_text("Kullanım: /bebekhisse_kontrol THYAO 200000")
        return
    try:
        symbol = normalize_instrument(context.args[0])
    except ValueError:
        if update.message is not None:
            await update.message.reply_text("Geçerli bir BIST hisse kodu yaz: /bebekhisse_kontrol THYAO 200000")
        return
    await _send_baby_scan(update, context, symbol=symbol, capital_args=list(context.args[1:]))


async def cmd_bebekhisse_ayar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explain the guardrails before a user starts a professional workflow."""

    if await _reject_unauthorized(update) or update.message is None:
        return
    settings = get_settings()
    try:
        profile = risk_profile_from_settings(settings)
    except ValueError as exc:
        await update.message.reply_text(f"⚠️ Risk ayarı geçersiz: {exc}")
        return
    await update.message.reply_text(format_baby_stock_settings(profile))
