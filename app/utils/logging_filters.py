from __future__ import annotations

import logging
import re

# Telegram bot token URL formatinda gecer: https://api.telegram.org/bot<TOKEN>/...
_TELEGRAM_BOT_TOKEN_RE = re.compile(r"(bot)\d+:[A-Za-z0-9_-]{20,}")
_AUTHORIZATION_HEADER_RE = re.compile(r"(Authorization[\"']?\s*[:=]\s*[\"']?)(Bearer\s+)?[A-Za-z0-9_\-\.]{10,}", re.IGNORECASE)
_API_KEY_QUERY_RE = re.compile(r"((?:api[_-]?key|apikey|token|secret)=)[^&\s\"']{4,}", re.IGNORECASE)
_GENERIC_SECRET_LABEL_RE = re.compile(
    r"((?:bot_token|api_key|webhook_secret|access_token)[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9_\-\.:]{6,}",
    re.IGNORECASE,
)

MASK = "***MASKED***"


def mask_sensitive_text(text: str) -> str:
    """Metin icindeki Telegram bot token, Authorization header, API key ve
    webhook secret gibi hassas degerleri maskeler.

    Ornek: 'https://api.telegram.org/bot123456:ABCdef/getUpdates'
        -> 'https://api.telegram.org/bot***MASKED***/getUpdates'
    """
    if not text:
        return text
    text = _TELEGRAM_BOT_TOKEN_RE.sub(r"\1" + MASK, text)
    text = _AUTHORIZATION_HEADER_RE.sub(r"\1" + MASK, text)
    text = _API_KEY_QUERY_RE.sub(r"\1" + MASK, text)
    text = _GENERIC_SECRET_LABEL_RE.sub(r"\1" + MASK, text)
    return text


class SensitiveDataFilter(logging.Filter):
    """Tum log kayitlarindaki hassas verileri (bot token, API anahtari,
    webhook secret, Authorization header) maskeleyen logging filtresi.

    Kullanim: logger.addFilter(SensitiveDataFilter()) veya
    logging.getLogger().addFilter(SensitiveDataFilter()) (root logger'a
    eklenirse tum uygulama genelinde calisir).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - formatlanamayan kayitlari oldugu gibi birak
            return True

        masked = mask_sensitive_text(message)
        if masked != message:
            record.msg = masked
            record.args = ()
        return True


def install_sensitive_data_filter() -> None:
    """Root logger'a hassas veri maskeleme filtresini ekler.

    Uygulama baslangicinda (app/main.py, run_bot.py) bir kez cagrilmalidir.
    httpx/telegram kutuphanelerinin ürettigi loglar da root logger uzerinden
    gectigi icin bu, bot tokeninin URL icinde loglara sizmasini engeller.
    """
    root_logger = logging.getLogger()
    for existing_filter in root_logger.filters:
        if isinstance(existing_filter, SensitiveDataFilter):
            return  # zaten eklenmis
    root_logger.addFilter(SensitiveDataFilter())

    # Bazi kutuphaneler (httpx, telegram) kendi handler/logger'larina sahip
    # olabilir; guvenlik icin en yaygin olanlara da aciktan filtre eklenir.
    for logger_name in ("httpx", "httpcore", "telegram", "telegram.ext"):
        lib_logger = logging.getLogger(logger_name)
        already = any(isinstance(f, SensitiveDataFilter) for f in lib_logger.filters)
        if not already:
            lib_logger.addFilter(SensitiveDataFilter())
