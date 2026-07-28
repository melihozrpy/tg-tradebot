from __future__ import annotations

import base64
import json
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any, Optional

import httpx

from app.config.settings import Settings

logger = logging.getLogger("mergen_quant.openrouter")

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "hisse_analysis_system_prompt.txt"
_SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


class OpenRouterError(RuntimeError):
    """OpenRouter analiz katmaninin guvenli, kullaniciya gosterilebilir taban hatasi."""


class OpenRouterDisabledError(OpenRouterError):
    pass


class OpenRouterQuotaExceededError(OpenRouterError):
    pass


class OpenRouterAuthenticationError(OpenRouterError):
    pass


class OpenRouterUnavailableError(OpenRouterError):
    pass


class OpenRouterInvalidImageError(OpenRouterError):
    pass


@lru_cache(maxsize=1)
def load_stock_analysis_prompt() -> str:
    """Kullanicinin onayladigi sistem promptunu UTF-8 olarak, degistirmeden yukler."""

    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    if not prompt.strip():
        raise RuntimeError("Hisse analiz sistem promptu bos.")
    return prompt


def _response_text(payload: dict[str, Any]) -> str:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterUnavailableError("OpenRouter gecersiz yanit bicimi dondurdu.") from exc
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        text = "\n".join(
            str(item.get("text", "")).strip()
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
    else:
        text = ""
    if not text:
        raise OpenRouterUnavailableError("OpenRouter bos analiz dondurdu.")
    return text


def _json_context(value: Optional[dict[str, Any]]) -> str:
    if not value:
        return "Doğrulanmış ek veri sağlanmadı. Görselde olmayan verileri uydurma."
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


class OpenRouterStockAnalyst:
    """Metin ve grafik gorseli icin OpenRouter tabanli hisse analiz istemcisi.

    Sistem promptu ayri UTF-8 dosyasindan aynen okunur. Teknik ve temel
    rakamlar model tarafinda hesaplanmaz; handler'in sagladigi dogrulanmis
    baglam yalnizca kullanici mesaji olarak eklenir.
    """

    _usage_lock = Lock()
    _usage_times: deque[datetime] = deque()

    def __init__(self, settings: Settings, client: Optional[httpx.Client] = None):
        self.settings = settings
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.settings.openrouter_timeout_seconds,
                follow_redirects=False,
            )
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _check_configuration(self) -> None:
        if not self.settings.openrouter_enabled:
            raise OpenRouterDisabledError("OpenRouter AI henuz etkin degil.")
        if not self.settings.openrouter_api_key.strip():
            raise OpenRouterAuthenticationError("OPENROUTER_API_KEY tanimli degil.")
        base_url = self.settings.openrouter_base_url.strip()
        if not base_url.startswith("https://"):
            raise OpenRouterUnavailableError("OPENROUTER_BASE_URL HTTPS olmali.")

    def _reserve_daily_slot(self) -> None:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        with self._usage_lock:
            while self._usage_times and self._usage_times[0] < cutoff:
                self._usage_times.popleft()
            if len(self._usage_times) >= self.settings.openrouter_daily_request_limit:
                raise OpenRouterQuotaExceededError("OpenRouter gunluk bot limiti doldu.")
            self._usage_times.append(now)

    def _request(self, messages: list[dict[str, Any]], *, model: str) -> str:
        self._check_configuration()
        self._reserve_daily_slot()
        endpoint = f"{self.settings.openrouter_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key.strip()}",
            "Content-Type": "application/json",
            "X-Title": "Montana Melih Hisse Bot",
        }
        body = {
            "model": model,
            "messages": messages,
            "temperature": 0.15,
            "max_tokens": self.settings.openrouter_max_tokens,
            "stream": False,
        }

        last_error: Optional[Exception] = None
        for attempt in range(self.settings.openrouter_max_retries + 1):
            try:
                response = self._get_client().post(
                    endpoint,
                    headers=headers,
                    json=body,
                    timeout=self.settings.openrouter_timeout_seconds,
                )
                if response.status_code == 429:
                    raise OpenRouterQuotaExceededError("OpenRouter ucretsiz model kotasi doldu.")
                if response.status_code in {401, 403}:
                    raise OpenRouterAuthenticationError("OpenRouter API anahtari reddedildi.")
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"OpenRouter HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return _response_text(response.json())
            except (OpenRouterQuotaExceededError, OpenRouterAuthenticationError):
                raise
            except (httpx.HTTPError, ValueError, OpenRouterUnavailableError) as exc:
                last_error = exc
                logger.warning(
                    "OpenRouter istegi basarisiz (deneme %s/%s): %s",
                    attempt + 1,
                    self.settings.openrouter_max_retries + 1,
                    type(exc).__name__,
                )
                if attempt < self.settings.openrouter_max_retries:
                    time.sleep(min(2**attempt, 4))
        raise OpenRouterUnavailableError(
            f"OpenRouter analiz servisine ulasilamadi ({type(last_error).__name__})."
        )

    @staticmethod
    def _user_text(user_request: str, verified_context: Optional[dict[str, Any]]) -> str:
        prepared_at = datetime.now(timezone.utc).isoformat()
        request_text = " ".join(str(user_request or "").split())
        return (
            f"Analiz isteğinin hazırlanma zamanı (UTC): {prepared_at}\n"
            f"Kullanıcının verdiği bilgiler: {request_text or 'Ek bilgi verilmedi.'}\n\n"
            "Aşağıdaki JSON botun erişebildiği veri sağlayıcılarından alınmış doğrulanmış bağlamdır. "
            "Yalnızca mevcut alanları kullan; eksik veri için açıkça veri olmadığını belirt. "
            "Bu bağlam bağımsız internet taraması yerine geçmiyorsa bunu raporda söyle.\n\n"
            f"DOĞRULANMIŞ BAĞLAM:\n{_json_context(verified_context)}"
        )

    def analyze_text(
        self,
        user_request: str,
        *,
        verified_context: Optional[dict[str, Any]] = None,
    ) -> str:
        messages = [
            {"role": "system", "content": load_stock_analysis_prompt()},
            {"role": "user", "content": self._user_text(user_request, verified_context)},
        ]
        return self._request(messages, model=self.settings.openrouter_model)

    def analyze_image(
        self,
        image_bytes: bytes,
        mime_type: str,
        user_request: str,
        *,
        verified_context: Optional[dict[str, Any]] = None,
    ) -> str:
        normalized_type = str(mime_type or "").split(";", 1)[0].strip().casefold()
        if normalized_type not in _SUPPORTED_IMAGE_TYPES:
            raise OpenRouterInvalidImageError("Desteklenen gorsel turleri: JPEG, PNG, WEBP ve GIF.")
        if not image_bytes:
            raise OpenRouterInvalidImageError("Gorsel bos.")
        if len(image_bytes) > self.settings.openrouter_max_image_bytes:
            maximum_mb = self.settings.openrouter_max_image_bytes / (1024 * 1024)
            raise OpenRouterInvalidImageError(f"Gorsel {maximum_mb:.0f} MB sinirini asiyor.")

        encoded = base64.b64encode(image_bytes).decode("ascii")
        content = [
            {"type": "text", "text": self._user_text(user_request, verified_context)},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{normalized_type};base64,{encoded}"},
            },
        ]
        messages = [
            {"role": "system", "content": load_stock_analysis_prompt()},
            {"role": "user", "content": content},
        ]
        return self._request(messages, model=self.settings.openrouter_vision_model)

