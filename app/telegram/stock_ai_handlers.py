from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import re
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes, MessageHandler, filters

from app.alerts.ocr import extract_alarm_text
from app.analysis.gyo_valuation_engine import collect_fundamental_payload
from app.config.instruments import load_universe, parse_instruments_env
from app.config.settings import Settings, get_settings
from app.data.gdelt_provider import build_gdelt_provider
from app.data.provider_factory import (
    build_fundamental_provider,
    build_kap_provider,
    build_market_data_provider,
)
from app.services.analysis_service_v3 import AnalysisUnavailableErrorV3, run_symbol_analysis_v3
from app.services.news_service import get_company_alias_info, get_recent_articles
from app.services.openrouter_service import (
    OpenRouterAuthenticationError,
    OpenRouterDisabledError,
    OpenRouterError,
    OpenRouterInvalidImageError,
    OpenRouterQuotaExceededError,
    OpenRouterStockAnalyst,
)
from app.telegram.handlers import _get_db, _reject_unauthorized
from app.telegram.message_templates_v3 import split_long_message

logger = logging.getLogger("mergen_quant.telegram.stock_ai")

_EXPLICIT_SYMBOL_RE = re.compile(
    r"(?:hisse|sembol)\s*:\s*([A-Z0-9.^=-]{3,16})(?:\.IS)?\b",
    re.IGNORECASE,
)
_SHORT_SYMBOL_RE = re.compile(
    r"^([A-Z0-9.^=-]{3,16})(?:\.IS)?(?:\s+(?:analiz|incele))?$",
    re.IGNORECASE,
)
_SYMBOL_BLACKLIST = {
    "ANALIZ", "BIST", "GRAFIK", "HISSE", "INCELE", "LONG", "MERHABA",
    "ORTA", "SHORT", "SWING", "SELAM",
}


def extract_requested_symbol(text: str) -> Optional[str]:
    """Serbest metinden yalniz acik veya tek basina yazilmis sembolu alir.

    Belirsiz cumlelerden sembol tahmin edilmez. Boylece "orta risk" gibi bir
    ifade yanlislikla hisse kodu kabul edilmez.
    """

    raw = " ".join(str(text or "").strip().split())
    if not raw:
        return None
    match = _EXPLICIT_SYMBOL_RE.search(raw) or _SHORT_SYMBOL_RE.fullmatch(raw)
    if match is None:
        return None
    symbol = match.group(1).strip().upper().removesuffix(".IS")
    if symbol in _SYMBOL_BLACKLIST:
        return None
    return symbol


def extract_chart_symbol_from_ocr_text(
    text: str,
    known_symbols: set[str],
) -> Optional[str]:
    """OCR metninden yalnızca yapılandırılmış evrende bulunan kodu seçer."""

    normalized_symbols = {
        str(symbol).strip().upper().removesuffix(".IS")
        for symbol in known_symbols
        if str(symbol).strip()
    }
    matches: list[str] = []
    for match in re.finditer(r"(?<![A-Z0-9])([A-Z][A-Z0-9]{2,15})(?:\.IS)?(?![A-Z0-9])", str(text or "").upper()):
        symbol = match.group(1).removesuffix(".IS")
        if symbol in normalized_symbols and symbol not in matches:
            matches.append(symbol)
    non_index_matches = [symbol for symbol in matches if symbol not in {"BIST", "XU100"}]
    if non_index_matches:
        return non_index_matches[0]
    return matches[0] if matches else None


def _known_chart_symbols(settings: Settings) -> set[str]:
    symbols = {"EURUSD", "XAUUSD", "XAGUSD", "US100", "XU100"}
    try:
        symbols.update(item.symbol for item in load_universe(settings.bist_universe_json_path))
    except Exception as exc:  # noqa: BLE001 - OCR yardımcı katmanı botu durdurmaz
        logger.warning("AI OCR sembol evreni okunamadi error=%s", type(exc).__name__)
    try:
        symbols.update(parse_instruments_env(settings.instruments))
    except (TypeError, ValueError):
        pass
    return symbols


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _jsonable(model_dump())
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


def _resolve_exchange(settings: Settings, symbol: str) -> str:
    known_non_bist = {
        "EURUSD": "FX",
        "XAUUSD": "Emtia / spot altın",
        "XAGUSD": "Emtia / spot gümüş",
        "US100": "ABD Nasdaq 100 türevi",
    }
    if symbol in known_non_bist:
        return known_non_bist[symbol]
    if symbol in {"XU100", "XU100.IS"}:
        return "Borsa Istanbul"
    try:
        for item in load_universe(settings.bist_universe_json_path):
            if item.symbol == symbol:
                return item.exchange or "Borsa Istanbul"
    except Exception as exc:  # noqa: BLE001 - borsa bilinmiyorsa tahmin edilmez
        logger.warning("AI enstruman evreni okunamadi error=%s", type(exc).__name__)
    return "Belirsiz; tahmin edilmedi"


def build_verified_stock_context(settings: Settings, symbol: str) -> dict[str, Any]:
    """Botun erisebildigi teknik/temel/haber kaynaklarini tek baglamda toplar.

    Her kaynak hatasi ayri kaydedilir; bir katmanin kesilmesi AI komutunu veya
    Telegram botunu cokertmez. Eksik veri modele eksik olarak aktarilir.
    """

    normalized = symbol.strip().upper().removesuffix(".IS")
    now_utc = datetime.now(timezone.utc)
    context: dict[str, Any] = {
        "symbol": normalized,
        "exchange": _resolve_exchange(settings, normalized),
        "prepared_at_utc": now_utc.isoformat(),
        "prepared_at_istanbul": now_utc.astimezone(ZoneInfo(settings.timezone_name)).isoformat(),
        "source_scope": {
            "independent_web_browser": False,
            "market_data_provider": settings.market_data_provider,
            "news_provider": "GDELT" if settings.gdelt_enabled else "disabled",
            "fundamental_provider": settings.fundamental_provider,
            "kap_provider": settings.kap_provider,
            "investor_forum_feed": "unavailable",
        },
        "warnings": [],
    }
    db = _get_db()
    try:
        try:
            market_provider = build_market_data_provider(settings)
            outcome = run_symbol_analysis_v3(
                db,
                market_provider,
                normalized,
                settings,
                news_provider=build_gdelt_provider(settings),
            )
            context["technical"] = {
                "signal": _jsonable(outcome.signal),
                "advanced_score": _jsonable(outcome.advanced_score),
                "decision": _jsonable(outcome.decision),
                "multi_timeframe": _jsonable(outcome.multi_timeframe),
                "liquidity": _jsonable(outcome.liquidity),
                "data_quality": _jsonable(outcome.data_quality),
                "xu100_relative_strength": _jsonable(outcome.xu100_relative_strength),
                "sector_relative_strength": _jsonable(outcome.sector_relative_strength),
                "analysis_mode": outcome.mode,
                "warnings": _jsonable(outcome.warnings),
            }
        except AnalysisUnavailableErrorV3 as exc:
            context["technical"] = {"status": "unavailable", "reason": str(exc)}
            context["warnings"].append("Teknik analiz için doğrulanmış veri alınamadı.")
        except Exception as exc:  # noqa: BLE001 - AI katmani teknik hata nedeniyle botu dusurmez
            logger.warning("AI teknik baglami olusturulamadi symbol=%s error=%s", normalized, type(exc).__name__)
            context["technical"] = {"status": "unavailable", "reason": type(exc).__name__}
            context["warnings"].append("Teknik veri sağlayıcısı geçici hata verdi.")

        try:
            fundamental_provider = build_fundamental_provider(settings)
            context["fundamentals"] = _jsonable(
                collect_fundamental_payload(fundamental_provider, normalized)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI temel baglami olusturulamadi symbol=%s error=%s", normalized, type(exc).__name__)
            context["fundamentals"] = {"status": "unavailable", "reason": type(exc).__name__}
            context["warnings"].append("Doğrulanmış temel analiz kaynağı kullanılamadı.")

        try:
            kap_provider = build_kap_provider(settings)
            disclosures = kap_provider.get_latest_disclosures(normalized)[:8]
            context["kap_disclosures"] = _jsonable(disclosures)
            if not disclosures:
                context["warnings"].append("Yapılandırılmış KAP bildirimi bulunamadı veya KAP sağlayıcısı kapalı.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI KAP baglami olusturulamadi symbol=%s error=%s", normalized, type(exc).__name__)
            context["kap_disclosures"] = []
            context["warnings"].append("KAP sağlayıcısı geçici olarak kullanılamadı.")

        try:
            articles = get_recent_articles(db, normalized, limit=8)
            context["recent_news"] = [
                {
                    "title": item.title,
                    "source": item.source,
                    "published_at": _jsonable(item.published_at),
                    "url": item.url,
                    "provider": item.provider,
                }
                for item in articles
            ]
            if not articles:
                context["warnings"].append("Doğrulanmış güncel haber kaydı bulunamadı.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI haber baglami okunamadi symbol=%s error=%s", normalized, type(exc).__name__)
            context["recent_news"] = []
            context["warnings"].append("Haber kayıtları geçici olarak okunamadı.")

        alias_info = get_company_alias_info(normalized, settings.company_aliases_path)
        context["company_identity"] = _jsonable(alias_info) if alias_info else {"status": "unavailable"}
        return context
    finally:
        db.close()


async def _deliver_analysis(update: Update, status_message, analysis: str) -> None:
    chunks = split_long_message(analysis)
    if not chunks:
        chunks = ["AI analiz servisi boş yanıt döndürdü."]
    await status_message.edit_text(chunks[0])
    for chunk in chunks[1:]:
        await update.effective_message.reply_text(chunk)


async def _run_text_analysis(update: Update, user_request: str, symbol: str) -> None:
    settings = get_settings()
    status = await update.effective_message.reply_text(
        f"🧠 {symbol} için doğrulanmış veriler hazırlanıyor ve AI analizi oluşturuluyor…"
    )
    analyst = OpenRouterStockAnalyst(settings)
    try:
        verified_context = await asyncio.to_thread(build_verified_stock_context, settings, symbol)
        analysis = await asyncio.to_thread(
            analyst.analyze_text,
            user_request,
            verified_context=verified_context,
        )
        await _deliver_analysis(update, status, analysis)
    except OpenRouterDisabledError:
        await status.edit_text("⚙️ OpenRouter henüz etkin değil. API anahtarı eklenip OPENROUTER_ENABLED=true yapılmalı.")
    except OpenRouterAuthenticationError:
        await status.edit_text("🔐 OpenRouter API anahtarı eksik veya reddedildi. Anahtarı Coolify secret alanında kontrol et.")
    except OpenRouterQuotaExceededError:
        await status.edit_text("⏳ Ücretsiz OpenRouter günlük kotası doldu. Teknik komutlar AI olmadan çalışmaya devam eder.")
    except OpenRouterError as exc:
        logger.warning("OpenRouter metin analizi basarisiz symbol=%s error=%s", symbol, type(exc).__name__)
        await status.edit_text(f"⚠️ AI analizi şu anda üretilemedi: {exc}")
    except Exception as exc:  # noqa: BLE001 - handler botu dusurmez
        logger.exception("AI metin handler beklenmeyen hata symbol=%s", symbol)
        await status.edit_text(f"⚠️ AI analizi geçici olarak üretilemedi ({type(exc).__name__}).")
    finally:
        analyst.close()


async def cmd_ai_analiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Kullanım: /ai_analiz THYAO | Gün içi + 2-4 haftalık swing | Orta risk"
        )
        return
    symbol = extract_requested_symbol(context.args[0])
    if symbol is None:
        await update.effective_message.reply_text("⚠️ Hisse kodunu doğrulayamadım. Örnek: /ai_analiz THYAO")
        return
    await _run_text_analysis(update, " ".join(context.args), symbol)


async def handle_stock_symbol_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("alarm_flow"):
        return
    text = update.effective_message.text or ""
    symbol = extract_requested_symbol(text)
    if symbol is None:
        return
    if await _reject_unauthorized(update):
        return
    await _run_text_analysis(update, text, symbol)


async def handle_stock_chart_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.photo:
        return
    skip_message_id = context.user_data.pop("skip_stock_ai_photo_message_id", None)
    if skip_message_id == message.message_id or context.user_data.get("alarm_flow"):
        return
    if await _reject_unauthorized(update):
        return

    settings = get_settings()
    status = await message.reply_text("🖼️ Grafik okunuyor, doğrulanmış veriler kontrol ediliyor…")
    analyst = OpenRouterStockAnalyst(settings)
    try:
        telegram_file = await message.photo[-1].get_file()
        content = bytes(await telegram_file.download_as_bytearray())
        user_request = message.caption or "Grafiği prompttaki kurallara göre incele."
        symbol = extract_requested_symbol(user_request)
        image_evidence: dict[str, Any] = {
            "telegram_image_bytes": len(content),
            "local_ocr_status": "unavailable",
        }
        try:
            ocr_result = await asyncio.to_thread(
                extract_alarm_text,
                content,
                language=settings.user_price_alert_ocr_language,
                maximum_bytes=settings.openrouter_max_image_bytes,
            )
            image_evidence = {
                "local_ocr_status": "completed",
                "local_ocr_confidence": ocr_result.confidence,
                "visible_text_excerpt": ocr_result.text[:2000],
                "warning": "OCR metni görsel kanıtıdır; finansal veri olarak tek başına doğrulanmış değildir.",
            }
            if symbol is None:
                symbol = extract_chart_symbol_from_ocr_text(
                    ocr_result.text,
                    _known_chart_symbols(settings),
                )
        except Exception as exc:  # noqa: BLE001 - görsel model OCR olmadan da çalışır
            logger.info("AI grafik yerel OCR kullanilamadi error=%s", type(exc).__name__)
            image_evidence["local_ocr_error"] = type(exc).__name__

        verified_context: dict[str, Any] = {"image_evidence": image_evidence}
        if symbol is not None:
            verified_context = await asyncio.to_thread(build_verified_stock_context, settings, symbol)
            verified_context["image_evidence"] = image_evidence
            verified_context["symbol_detection_source"] = (
                "caption" if extract_requested_symbol(user_request) else "local_ocr"
            )
        analysis = await asyncio.to_thread(
            analyst.analyze_image,
            content,
            "image/jpeg",
            user_request,
            verified_context=verified_context,
        )
        await _deliver_analysis(update, status, analysis)
    except OpenRouterDisabledError:
        await status.edit_text("⚙️ Grafik AI analizi henüz etkin değil. OpenRouter anahtarı ve deploy gerekiyor.")
    except OpenRouterAuthenticationError:
        await status.edit_text("🔐 OpenRouter API anahtarı eksik veya reddedildi.")
    except OpenRouterQuotaExceededError:
        await status.edit_text("⏳ Ücretsiz grafik analiz kotası bugün için doldu.")
    except OpenRouterInvalidImageError as exc:
        await status.edit_text(f"⚠️ Grafik işlenemedi: {exc}")
    except OpenRouterError as exc:
        logger.warning("OpenRouter gorsel analizi basarisiz error=%s", type(exc).__name__)
        await status.edit_text(f"⚠️ Grafik AI analizi şu anda üretilemedi: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI gorsel handler beklenmeyen hata")
        await status.edit_text(f"⚠️ Grafik analizi geçici olarak üretilemedi ({type(exc).__name__}).")
    finally:
        analyst.close()


def register_stock_ai_handlers(application) -> None:
    # Alarm fotograflari group=1'de ele alinir. AI fotograflari group=2'de
    # calisir ve alarm handler'inin biraktigi tek-kullanimlik bayragi denetler.
    application.add_handler(MessageHandler(filters.PHOTO, handle_stock_chart_photo), group=2)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_stock_symbol_text),
        group=2,
    )
