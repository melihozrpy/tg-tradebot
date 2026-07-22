from __future__ import annotations

import logging

from app.utils.logging_filters import SensitiveDataFilter, mask_sensitive_text


def test_telegram_bot_token_masked_in_url():
    token = "123456789" + ":" + "ABCDefGhIJKlmNoPQRstuVWXyz123456789"
    text = f"GET https://api.telegram.org/bot{token}/getUpdates"
    masked = mask_sensitive_text(text)
    assert token not in masked
    assert "bot***MASKED***" in masked


def test_authorization_header_masked():
    secret = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
    text = f"Authorization: Bearer {secret}"
    masked = mask_sensitive_text(text)
    assert secret not in masked


def test_api_key_query_param_masked():
    secret = "supersecret" + "value123"
    text = f"https://example.com/data?api_key={secret}&other=1"
    masked = mask_sensitive_text(text)
    assert secret not in masked


def test_normal_text_unaffected():
    text = "THYAO analizi tamamlandi, skor 78.5"
    assert mask_sensitive_text(text) == text


def test_logging_filter_masks_log_records(caplog):
    logger = logging.getLogger("test_mergen_mask")
    logger.addFilter(SensitiveDataFilter())
    token = "987654321" + ":" + "XyzAbCdEfGhIjKlMnOpQrStUv"

    with caplog.at_level(logging.INFO, logger="test_mergen_mask"):
        logger.info("Bot baslatiliyor: https://api.telegram.org/bot%s/getMe", token)

    assert token.split(":", 1)[1] not in caplog.text
    assert "MASKED" in caplog.text


def test_empty_and_none_text_handled():
    assert mask_sensitive_text("") == ""
