"""Conservative daily screen and VIOP information commands."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

from app.analysis.screener_engine import format_daily_top_picks_report, run_daily_top_picks_scan
from app.config.instruments import universe_symbols
from app.config.settings import get_settings
from app.data.provider_factory import build_market_data_provider
from app.fundamentals.factory import build_fundamental_provider
from app.telegram.handlers import _reject_unauthorized

logger = logging.getLogger("mergen_quant.telegram.basic_viop")


async def cmd_basitalsat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the existing daily technical + verified-fundamental quality screen."""

    if await _reject_unauthorized(update) or update.message is None:
        return
    settings = get_settings()
    strict_settings = settings.model_copy(update={"daily_top_picks_minimum_confirmations": 8})
    await update.message.reply_text(
        "📊 GÜNLÜK TEKNİK + TEMEL TARAMA başlıyor.\n"
        "Yalnız güçlü teknik teyit, gerçek direnç potansiyeli ve doğrulanabilir temel veriyle uyumlu adaylar gösterilir; kriteri geçmeyen hisse zorla eklenmez."
    )

    def scan():
        return run_daily_top_picks_scan(
            symbols=universe_symbols(settings.bist_universe_json_path),
            provider_factory=lambda: build_market_data_provider(settings),
            fundamental_provider_factory=lambda: build_fundamental_provider(settings),
            settings=strict_settings,
        )

    try:
        report = await asyncio.to_thread(scan)
        await update.message.reply_text(
            format_daily_top_picks_report(report, timezone_name=settings.timezone_name),
            disable_web_page_preview=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Basit al-sat taraması tamamlanamadı: %s", exc)
        await update.message.reply_text(
            "⚠️ Günlük tarama doğrulanabilir veriyle tamamlanamadı; tahmini aday gönderilmedi."
        )


async def cmd_viop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Explain VIOP safely; never fabricate live contract/OI/margin data."""

    if await _reject_unauthorized(update) or update.message is None:
        return
    settings = get_settings()
    reference = "Doğrulanmış anlık VİOP kontrat verisi yapılandırılmadı."
    try:
        provider = build_market_data_provider(settings)
        symbol = str(settings.xu100_symbol).upper().removesuffix(".IS").removeprefix("^")
        frame = await asyncio.to_thread(
            provider.get_ohlcv, symbol, "1d", datetime.now(timezone.utc) - timedelta(days=7), datetime.now(timezone.utc)
        )
        if len(frame) >= 2:
            close = float(frame.iloc[-1]["close"])
            previous = float(frame.iloc[-2]["close"])
            change = ((close / previous) - 1) * 100 if previous else 0.0
            reference = f"XU100 spot referansı: {close:,.2f} (%{change:+.2f}); bu VİOP kontrat fiyatı değildir."
    except Exception as exc:  # noqa: BLE001
        logger.info("VİOP spot referansı alınamadı: %s", type(exc).__name__)

    await update.message.reply_text(
        "📉 VİOP — BİLGİ VE RİSK ÇERÇEVESİ\n\n"
        "VİOP, Borsa İstanbul'un vadeli işlem ve opsiyon piyasasıdır; kaldıraç kârı da zararı da büyütür.\n"
        f"• {reference}\n\n"
        "İşlem öncesi kontrol: vade tarihi, kontrat çarpanı, güncel teminat, açık pozisyon, baz ve günlük fiyat limiti.\n"
        "Pay vadeli kontrat listesi ve sözleşme özellikleri değişebilir; güncel doğrulama için Borsa İstanbul VİOP sözleşme sayfasını kullan.\n"
        "Resmî kaynak: https://www.borsaistanbul.com/tr/sayfa/442/vadeli-islem-ve-opsiyon-piyasasi\n\n"
        "⚠️ Canlı VİOP fiyatı/açık pozisyon/teminat verisi bağlı değilken bunlar uydurulmaz. Senaryo dili kullan; stop ve teminat riskini işlemden önce hesapla.",
        disable_web_page_preview=True,
    )
