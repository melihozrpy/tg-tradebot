"""Telegram botunu yerel gelistirmede polling modunda calistirmak icin giris noktasi.

Kullanim:
    python run_bot.py

Not: FastAPI servisi (app/main.py) ayri bir process olarak `uvicorn app.main:app`
ile calistirilir. Bu ikisi FAZ 1'de birbirinden bagimsiz calisir.
"""
from __future__ import annotations

import logging

from alembic import command
from alembic.config import Config

from app.config.settings import get_settings, get_strategy_config
from app.models.database import init_db
from app.telegram.bot import run_polling
from app.utils.logging_filters import install_sensitive_data_filter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
install_sensitive_data_filter()
logger = logging.getLogger("mergen_quant.run_bot")


def main() -> None:
    settings = get_settings()
    get_strategy_config()  # hatali config ile baslama
    # Yerel masaustu calistirmasinda da Docker entrypoint ile ayni additive
    # migration guvencesi. Eski migration dosyalari degismez; hata olursa bot
    # eksik semayla acilmaz.
    command.upgrade(Config("alembic.ini"), "head")
    init_db()
    logger.info("Veritabani hazir, Telegram botu baslatiliyor (mode=%s)...", settings.telegram_mode)

    if settings.telegram_mode == "webhook":
        raise NotImplementedError(
            "Webhook modu FAZ 4'te production Docker kurulumu ile birlikte eklenecektir. "
            "FAZ 1'de yalnizca polling modu desteklenir. TELEGRAM_MODE=polling kullanin."
        )

    run_polling()


if __name__ == "__main__":
    main()
