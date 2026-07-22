from __future__ import annotations

import json

import httpx
import pytest

from app.config.settings import Settings
from app.models.database import GroqExplanation
from app.services.groq_service import (
    KIND_TECHNICAL,
    GroqExplainer,
    _contains_forbidden_content,
    compute_cache_key,
)


def _settings(**overrides) -> Settings:
    base = dict(groq_enabled=True, groq_api_key="fake-secret-key-123456", groq_model="test-model", groq_max_retries=0, groq_timeout_seconds=5)
    base.update(overrides)
    return Settings(**base)


def test_groq_disabled_uses_deterministic_fallback(db_session):
    settings = _settings(groq_enabled=False)
    explainer = GroqExplainer(settings)
    text, is_fallback = explainer.explain(db_session, "THYAO", KIND_TECHNICAL, {"skor": 70})
    assert is_fallback is True
    assert text
    # Deterministik sablon herhangi bir Groq API cagrisi yapilmadan uretilmis olmali.
    row = db_session.query(GroqExplanation).filter(GroqExplanation.symbol == "THYAO").first()
    assert row is not None
    assert row.is_fallback is True


def test_groq_analysis_still_works_without_groq_configured(db_session):
    """Groq kapaliyken bile /ai_aciklama akisi COKMEMELI, deterministik metin donmeli."""
    settings = _settings(groq_enabled=False, groq_api_key="")
    explainer = GroqExplainer(settings)
    text, is_fallback = explainer.explain(db_session, "ASELS", KIND_TECHNICAL, {"skor": 55})
    assert isinstance(text, str) and len(text) > 0
    assert is_fallback is True


def test_groq_error_falls_back_to_deterministic(db_session):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = _settings()
    explainer = GroqExplainer(settings, client=client)

    text, is_fallback = explainer.explain(db_session, "GARAN", KIND_TECHNICAL, {"skor": 40})
    assert is_fallback is True
    assert text


def test_groq_invalid_json_falls_back(db_session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not-json-at-all"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = _settings()
    explainer = GroqExplainer(settings, client=client)

    text, is_fallback = explainer.explain(db_session, "KCHOL", KIND_TECHNICAL, {"skor": 33})
    assert is_fallback is True


def test_groq_missing_explanation_field_fails_validation(db_session):
    def handler(request: httpx.Request) -> httpx.Response:
        content = json.dumps({"not_explanation": "oops"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = _settings()
    explainer = GroqExplainer(settings, client=client)

    text, is_fallback = explainer.explain(db_session, "EREGL", KIND_TECHNICAL, {"skor": 10})
    assert is_fallback is True


def test_groq_cannot_produce_price_target_stop_content():
    """Groq cevabinin fiyat/hedef/stop veya AL/SAT karari icermesi guvenlik
    kapisi tarafindan REDDEDILMELI (bolum 3 spesifikasyonu)."""
    assert _contains_forbidden_content("Hedef fiyat 120 TL olabilir") is True
    assert _contains_forbidden_content("Stop-loss seviyesi önemlidir") is True
    assert _contains_forbidden_content("AL sinyali güçlü görünüyor") is True
    assert _contains_forbidden_content("Fiyat 45,50 TL civarında") is True
    assert _contains_forbidden_content("Teknik göstergeler karışık bir görünüm sunuyor") is False


def test_groq_forbidden_content_triggers_fallback(db_session):
    def handler(request: httpx.Request) -> httpx.Response:
        content = json.dumps({"explanation": "Hedef fiyat 100 TL olarak görünüyor, AL sinyali güçlü."})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = _settings()
    explainer = GroqExplainer(settings, client=client)

    text, is_fallback = explainer.explain(db_session, "SASA", KIND_TECHNICAL, {"skor": 61})
    assert is_fallback is True
    assert "hedef fiyat" not in text.lower()
    assert "al sinyali" not in text.lower()


def test_groq_valid_response_is_accepted_and_cached(db_session):
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        content = json.dumps({"explanation": "Teknik göstergeler nötr bir görünüm sergiliyor."})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = _settings()
    explainer = GroqExplainer(settings, client=client)

    payload = {"skor": 50}
    text1, is_fallback1 = explainer.explain(db_session, "BIMAS", KIND_TECHNICAL, payload)
    assert is_fallback1 is False
    assert call_count["n"] == 1

    # Ayni sembol+kind+veri icin TEKRAR istek atilmamali (cache).
    text2, is_fallback2 = explainer.explain(db_session, "BIMAS", KIND_TECHNICAL, payload)
    assert text2 == text1
    assert call_count["n"] == 1


def test_groq_daily_request_limit_forces_fallback(db_session):
    def handler(request: httpx.Request) -> httpx.Response:
        content = json.dumps({"explanation": "Açıklama metni."})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = _settings(groq_daily_request_limit=1)
    explainer = GroqExplainer(settings, client=client)

    explainer.explain(db_session, "TUPRS", KIND_TECHNICAL, {"skor": 1})
    text, is_fallback = explainer.explain(db_session, "TUPRS", KIND_TECHNICAL, {"skor": 2})
    assert is_fallback is True


def test_api_key_never_appears_in_exception_logs(db_session, caplog):
    """API anahtari (secret) hata mesajlarinda/loglarda ASLA acikca gecmemeli."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = _settings(groq_api_key="super-secret-value-abcdef")
    explainer = GroqExplainer(settings, client=client)

    with caplog.at_level("WARNING"):
        explainer.explain(db_session, "AKBNK", KIND_TECHNICAL, {"skor": 5})

    for record in caplog.records:
        assert "super-secret-value-abcdef" not in record.getMessage()


def test_compute_cache_key_is_deterministic():
    key1 = compute_cache_key("THYAO", KIND_TECHNICAL, {"a": 1, "b": 2})
    key2 = compute_cache_key("THYAO", KIND_TECHNICAL, {"b": 2, "a": 1})
    assert key1 == key2
