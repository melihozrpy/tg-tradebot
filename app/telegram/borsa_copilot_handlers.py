"""Telegram command surface for the confirmation-first Borsa Copilot."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

from app.analysis.viop_engine import find_viop_underlying, load_viop_universe, parse_viop_horizon
from app.analysis.viop_warrant_copilot import (
    BORSA_COPILOT_SYSTEM_PROMPT,
    NewsRiskCheck,
    analyze_live_copilot,
    format_copilot_scenario,
)
from app.config.settings import get_settings
from app.data.provider_factory import build_kap_provider, build_market_data_provider
from app.modules.morning_report import fetch_economic_calendar
from app.telegram.handlers import _reject_unauthorized

logger = logging.getLogger("mergen_quant.telegram.borsa_copilot")

_VIOP_MACRO_UNDERLYINGS = {"XU030", "USDTRY", "XAUTRY", "XAGTRY"}


def _parse_expiry(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _parse_delta(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return None
    return value if 0 < abs(value) <= 1 else None


def _published_recent(value, *, now: datetime) -> bool:
    if not isinstance(value, datetime):
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value >= now - timedelta(hours=48)


def _verified_news_risk(settings, symbol: str) -> NewsRiskCheck:
    """Use only configured KAP plus the existing economic calendar source."""

    calendar_events = fetch_economic_calendar(settings, [symbol])
    high_impact = [
        event for event in calendar_events
        if event.impact == "high" and symbol.upper().removesuffix(".IS") in event.affected_instruments
    ]
    if high_impact:
        return NewsRiskCheck(False, f"Yüksek etkili takvim olayı: {high_impact[0].title[:80]}")
    try:
        kap = build_kap_provider(settings)
    except Exception as exc:  # noqa: BLE001
        return NewsRiskCheck(False, f"KAP kaynağı kurulamadı ({type(exc).__name__})")
    if getattr(kap, "name", "disabled") == "disabled":
        return NewsRiskCheck(False, "KAP kaynağı kapalı; haber riski doğrulanmadı")
    try:
        now = datetime.now(timezone.utc)
        disclosures = kap.get_latest_disclosures(symbol)[:8]
        negatives = [
            item for item in disclosures
            if _published_recent(item.get("published_at"), now=now)
            and str(kap.classify_disclosure(item)).upper() == "NEGATIF_OLABILIR"
        ]
    except Exception as exc:  # noqa: BLE001
        return NewsRiskCheck(False, f"KAP kontrolü alınamadı ({type(exc).__name__})")
    if negatives:
        return NewsRiskCheck(False, f"Son KAP riski: {str(negatives[0].get('title') or 'bildirim')[:80]}")
    return NewsRiskCheck(True, "Yüksek etkili takvim olayı ve son 48s negatif KAP bildirimi bulunmadı")


async def _run_copilot(update: Update, context: ContextTypes.DEFAULT_TYPE, *, fixed_product: str | None = None) -> None:
    if await _reject_unauthorized(update) or update.message is None:
        return
    args = list(context.args or [])
    if fixed_product is None:
        if len(args) < 3:
            await update.message.reply_text(
                "Borsa Copilot için önce ürün, dayanak ve süreyi yaz.\n\n"
                "VİOP: /borsacopilot viop THYAO gunici\n"
                "Varant: /borsacopilot varant THYAO swing 2026-10-30 0.45\n\n"
                "Varantta son işlem günü ve delta zorunludur; bilinmiyorsa Copilot sinyal üretmez."
            )
            return
        product = args.pop(0).casefold()
    else:
        product = fixed_product
        if len(args) < 2:
            command = "/viopcopilot THYAO gunici" if product == "viop" else "/varant THYAO swing 2026-10-30 0.45"
            await update.message.reply_text(f"Kullanım: {command}")
            return
    if product not in {"viop", "varant"}:
        await update.message.reply_text("Ürün yalnızca viop veya varant olabilir.")
        return
    symbol = args.pop(0).upper().removesuffix(".IS")
    horizon = parse_viop_horizon(args.pop(0) if args else None)
    if horizon is None:
        await update.message.reply_text("Süre: gunici, haftalik veya aylik olmalı.")
        return
    expiry = _parse_expiry(args[0] if args else None) if product == "varant" else None
    delta = _parse_delta(args[1] if len(args) > 1 else None) if product == "varant" else None
    if product == "varant" and (expiry is None or delta is None):
        await update.message.reply_text(
            "Varant için dayanak, süre, son işlem günü ve delta gerekli.\n"
            "Örnek: /varant THYAO swing 2026-10-30 0.45\n"
            "Bunlar olmadan vade/delta riski doğrulanamaz; sinyal üretmiyorum."
        )
        return
    settings = get_settings()
    if product == "viop":
        try:
            universe = load_viop_universe(settings.viop_underlyings_json_path)
            listed = find_viop_underlying(universe, symbol) is not None or symbol in _VIOP_MACRO_UNDERLYINGS
        except Exception as exc:  # noqa: BLE001
            logger.warning("VIOP universe unavailable: %s", type(exc).__name__)
            listed = symbol in _VIOP_MACRO_UNDERLYINGS
        if not listed:
            await update.message.reply_text(
                f"{symbol}, doğrulanmış VİOP dayanak listesinde yok. Aktif sözleşmeyi ve vadesini aracı kurum ekranından doğrula; liste dışı dayanakta plan üretmiyorum."
            )
            return
    await update.message.reply_text(
        f"Borsa Copilot {symbol} için kapanmış HTF/LTF mumlarını ve haber riskini kontrol ediyor..."
    )
    try:
        news_check = await asyncio.to_thread(_verified_news_risk, settings, symbol)
        scenario = await asyncio.to_thread(
            analyze_live_copilot,
            product=product,
            symbol=symbol,
            horizon=horizon,
            provider=build_market_data_provider(settings),
            settings=settings,
            news_check=news_check,
            warrant_expiry=expiry,
            warrant_delta=delta,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Borsa Copilot analysis failed symbol=%s: %s", symbol, exc)
        await update.message.reply_text(
            "Kapanmış ve yeterli dayanak verisi doğrulanamadı; Copilot seviye uydurmadı. "
            "Sembol/veri kaynağı ve aktif sözleşmeyi kontrol edip yeniden dene."
        )
        return
    await update.message.reply_text(
        format_copilot_scenario(scenario, timezone_name=settings.timezone_name),
        disable_web_page_preview=True,
    )


async def cmd_borsa_copilot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_copilot(update, context)


async def cmd_viop_copilot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_copilot(update, context, fixed_product="viop")


async def cmd_varant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_copilot(update, context, fixed_product="varant")


def copilot_intro() -> str:
    """Short menu text; the full source prompt remains reviewable in code."""

    _ = BORSA_COPILOT_SYSTEM_PROMPT
    return (
        "Borsa Copilot · VİOP & Varant\n"
        "6 adım geçmeden sinyal vermez. Giriş, stop, üç hedef ve R/R yalnızca kapanmış veriden çıkar.\n"
        "Komutlar: /viopcopilot THYAO gunici · /varant THYAO swing 2026-10-30 0.45"
    )
