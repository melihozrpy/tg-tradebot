from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.fundamentals.base import (
    FundamentalDataProvider,
    ProviderConfigurationError,
    ProviderResponseError,
)
from app.fundamentals.models import FundamentalSnapshot, SourceTrust
from app.fundamentals.normalizer import normalize_symbol, snapshot_from_payload


class LicensedKapRestProvider(FundamentalDataProvider):
    """Adapter for a contracted KAP REST feed or its licensed gateway.

    KAP contracts can expose different gateway paths, so the path template is
    configurable. The endpoint must return the canonical JSON accepted by
    ``snapshot_from_payload``; undocumented public website endpoints are never used.
    """

    name = "kap_rest"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        endpoint_path_template: str = "/fundamentals/{symbol}",
        api_key_header: str = "X-API-Key",
        timeout_seconds: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
        if (parsed.scheme != "https" and not local_http) or not parsed.netloc:
            raise ProviderConfigurationError("KAP REST adresi geçerli bir HTTPS adresi olmalıdır.")
        if not api_key.strip():
            raise ProviderConfigurationError("KAP REST API anahtarı eksik.")
        if "{symbol}" not in endpoint_path_template:
            raise ProviderConfigurationError("KAP endpoint yolu {symbol} alanını içermelidir.")
        if not api_key_header.strip() or any(char in api_key_header for char in "\r\n"):
            raise ProviderConfigurationError("KAP API anahtar başlığı geçersiz.")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key.strip()
        self.endpoint_path_template = endpoint_path_template
        self.api_key_header = api_key_header.strip()
        self.timeout_seconds = timeout_seconds
        self._client = client

    def _url(self, symbol: str) -> str:
        path = self.endpoint_path_template.format(symbol=quote(symbol, safe=""))
        return f"{self.base_url}/{path.lstrip('/')}"

    def fetch(self, symbol: str) -> FundamentalSnapshot:
        normalized = normalize_symbol(symbol)
        url = self._url(normalized)
        headers = {self.api_key_header: self._api_key, "Accept": "application/json"}

        def execute(client: httpx.Client) -> Mapping[str, Any]:
            try:
                response = client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                raise ProviderResponseError("KAP REST bağlantısı kurulamadı.") from exc
            if response.status_code >= 400:
                raise ProviderResponseError(f"KAP REST HTTP {response.status_code} hatası verdi.")
            try:
                payload = response.json()
            except ValueError as exc:
                raise ProviderResponseError("KAP REST geçersiz JSON döndürdü.") from exc
            if not isinstance(payload, Mapping):
                raise ProviderResponseError("KAP REST JSON nesnesi döndürmedi.")
            result = payload.get("result")
            return result if isinstance(result, Mapping) else payload

        if self._client is not None:
            payload = execute(self._client)
        else:
            with httpx.Client(timeout=self.timeout_seconds, follow_redirects=False) as client:
                payload = execute(client)
        return snapshot_from_payload(
            payload,
            provider=self.name,
            trust=SourceTrust.PRIMARY,
            requested_symbol=normalized,
            default_source_url=url,
            notes=("Lisanslı KAP REST/gateway verisi.",),
        )
