from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from app.config.settings import Settings
from app.services.openrouter_service import (
    OpenRouterDisabledError,
    OpenRouterQuotaExceededError,
    OpenRouterStockAnalyst,
    load_stock_analysis_prompt,
)
from app.telegram.stock_ai_handlers import extract_requested_symbol


def _settings(**overrides) -> Settings:
    values = {
        "openrouter_enabled": True,
        "openrouter_api_key": "test-openrouter-secret",
        "openrouter_base_url": "https://openrouter.ai/api/v1",
        "openrouter_model": "nvidia/nemotron-3-ultra-550b-a55b:free",
        "openrouter_vision_model": "openrouter/free",
        "openrouter_timeout_seconds": 5,
        "openrouter_max_tokens": 1200,
        "openrouter_max_retries": 0,
        "openrouter_daily_request_limit": 50,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def _reset_openrouter_usage():
    OpenRouterStockAnalyst._usage_times.clear()
    yield
    OpenRouterStockAnalyst._usage_times.clear()


def test_approved_prompt_is_versioned_without_silent_changes():
    prompt = load_stock_analysis_prompt()
    assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() == (
        "9f7caa26e4d1424fdb9e6d6c01aa2c63d72c5ba6aaab8b64b5fad248daf5d363"
    )
    assert prompt.startswith("Sen; teknik analiz, temel analiz, risk yönetimi")
    assert "11. SONUÇ TABLOSU" in prompt


def test_text_analysis_uses_exact_system_prompt_and_verified_context():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Koşullu analiz hazır."}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    analyst = OpenRouterStockAnalyst(_settings(), client=client)
    result = analyst.analyze_text(
        "THYAO orta risk",
        verified_context={"symbol": "THYAO", "technical": {"price": 325.0}},
    )

    assert result == "Koşullu analiz hazır."
    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["authorization"] == "Bearer test-openrouter-secret"
    assert captured["body"]["model"] == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert captured["body"]["messages"][0] == {
        "role": "system",
        "content": load_stock_analysis_prompt(),
    }
    assert '"symbol": "THYAO"' in captured["body"]["messages"][1]["content"]


def test_image_analysis_uses_free_vision_router_and_base64_data_url():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Grafik analizi."}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    analyst = OpenRouterStockAnalyst(_settings(), client=client)
    result = analyst.analyze_image(b"fake-jpeg", "image/jpeg", "Hisse: THYAO / BIST")

    assert result == "Grafik analizi."
    body = captured["body"]
    assert body["model"] == "openrouter/free"
    content = body["messages"][1]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_disabled_openrouter_never_calls_network():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    analyst = OpenRouterStockAnalyst(_settings(openrouter_enabled=False), client=client)
    with pytest.raises(OpenRouterDisabledError):
        analyst.analyze_text("THYAO")
    assert called is False


def test_local_daily_limit_stops_extra_free_request():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    analyst = OpenRouterStockAnalyst(_settings(openrouter_daily_request_limit=1), client=client)
    assert analyst.analyze_text("THYAO") == "ok"
    with pytest.raises(OpenRouterQuotaExceededError):
        analyst.analyze_text("ASELS")
    assert calls == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("THYAO", "THYAO"),
        ("thyao analiz", "THYAO"),
        ("Hisse: ASELS / BIST", "ASELS"),
        ("Sembol: GARAN.IS", "GARAN"),
        ("orta risk", None),
        ("selam", None),
    ],
)
def test_symbol_extraction_is_deliberately_conservative(text, expected):
    assert extract_requested_symbol(text) == expected

