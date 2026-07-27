from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from app.fundamentals.base import (
    FundamentalDataProvider,
    ProviderConfigurationError,
    ProviderResponseError,
)
from app.fundamentals.models import FundamentalSnapshot, SourceTrust
from app.fundamentals.normalizer import normalize_symbol, snapshot_from_payload


class FintablesMcpProvider(FundamentalDataProvider):
    """Authorized Fintables MCP adapter; it never accesses or scrapes web pages.

    A bearer token must be supplied by the user/OAuth flow. The token is sent only
    in the Authorization header and is never included in errors or provenance.
    """

    name = "fintables_mcp"

    def __init__(
        self,
        *,
        endpoint: str,
        bearer_token: str,
        tool_name: str,
        symbol_argument: str = "symbol",
        tool_arguments: Mapping[str, Any] | None = None,
        protocol_version: str = "2025-03-26",
        timeout_seconds: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ProviderConfigurationError("Fintables MCP adresi geçerli bir HTTPS adresi olmalıdır.")
        if not bearer_token.strip():
            raise ProviderConfigurationError("Fintables OAuth bearer token eksik.")
        if not tool_name.strip():
            raise ProviderConfigurationError("Fintables MCP araç adı eksik.")
        if not symbol_argument.strip():
            raise ProviderConfigurationError("Fintables MCP sembol argümanı eksik.")
        self.endpoint = endpoint
        self._bearer_token = bearer_token.strip()
        self.tool_name = tool_name.strip()
        self.symbol_argument = symbol_argument.strip()
        self.tool_arguments = dict(tool_arguments or {})
        self.protocol_version = protocol_version
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._session_id: str | None = None
        self._initialized = False
        self._request_id = 0
        self._lock = threading.Lock()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._bearer_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": self.protocol_version,
        }
        if self._session_id:
            headers["MCP-Session-Id"] = self._session_id
        return headers

    @staticmethod
    def _decode_response(response: httpx.Response, *, allow_empty: bool = False) -> Mapping[str, Any]:
        if response.status_code >= 400:
            raise ProviderResponseError(f"Fintables MCP HTTP {response.status_code} hatası verdi.")
        if not response.content:
            if allow_empty:
                return {}
            raise ProviderResponseError("Fintables MCP boş yanıt döndürdü.")
        content_type = response.headers.get("content-type", "").lower()
        try:
            if "text/event-stream" in content_type:
                events: list[Any] = []
                for line in response.text.splitlines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    events.append(json.loads(data))
                payload = next(
                    (
                        item
                        for item in reversed(events)
                        if isinstance(item, Mapping) and ("result" in item or "error" in item)
                    ),
                    next((item for item in reversed(events) if isinstance(item, Mapping)), None),
                )
            else:
                payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderResponseError("Fintables MCP geçersiz JSON döndürdü.") from exc
        if not isinstance(payload, Mapping):
            raise ProviderResponseError("Fintables MCP JSON-RPC nesnesi döndürmedi.")
        if payload.get("error"):
            error = payload["error"]
            code = error.get("code") if isinstance(error, Mapping) else "unknown"
            raise ProviderResponseError(f"Fintables MCP JSON-RPC hatası ({code}).")
        return payload

    def _post(self, payload: Mapping[str, Any], *, allow_empty: bool = False) -> Mapping[str, Any]:
        def execute(client: httpx.Client) -> Mapping[str, Any]:
            try:
                response = client.post(self.endpoint, headers=self._headers(), json=dict(payload))
            except httpx.HTTPError as exc:
                raise ProviderResponseError("Fintables MCP bağlantısı kurulamadı.") from exc
            session_id = response.headers.get("MCP-Session-Id")
            if session_id:
                self._session_id = session_id
            return self._decode_response(response, allow_empty=allow_empty)

        if self._client is not None:
            return execute(self._client)
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=False) as client:
            return execute(client)

    def _initialize(self) -> None:
        if self._initialized:
            return
        response = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {},
                    "clientInfo": {"name": "montana-melih-hisse-bot", "version": "1.0"},
                },
            }
        )
        result = response.get("result")
        if not isinstance(result, Mapping) or not result.get("protocolVersion"):
            raise ProviderResponseError("Fintables MCP başlatma yanıtı doğrulanamadı.")
        self.protocol_version = str(result["protocolVersion"])
        self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            allow_empty=True,
        )
        self._initialized = True

    @staticmethod
    def _tool_payload(response: Mapping[str, Any]) -> Mapping[str, Any]:
        result = response.get("result")
        if not isinstance(result, Mapping) or result.get("isError") is True:
            raise ProviderResponseError("Fintables MCP aracı veri üretemedi.")
        structured = result.get("structuredContent")
        if isinstance(structured, Mapping):
            inner = structured.get("result")
            return inner if isinstance(inner, Mapping) else structured
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, Mapping) or item.get("type") != "text":
                    continue
                try:
                    parsed = json.loads(str(item.get("text", "")))
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, Mapping):
                    return parsed
        # Arbitrary prose is deliberately not parsed into financial values.
        raise ProviderResponseError("Fintables MCP aracı yapılandırılmış finansal JSON döndürmedi.")

    def fetch(self, symbol: str) -> FundamentalSnapshot:
        normalized = normalize_symbol(symbol)
        with self._lock:
            self._initialize()
            arguments = {**self.tool_arguments, self.symbol_argument: normalized}
            response = self._post(
                {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/call",
                    "params": {"name": self.tool_name, "arguments": arguments},
                }
            )
        return snapshot_from_payload(
            self._tool_payload(response),
            provider=self.name,
            trust=SourceTrust.LICENSED,
            requested_symbol=normalized,
            default_source_url=self.endpoint,
            notes=("Kullanıcı OAuth yetkisiyle Fintables MCP üzerinden alındı.",),
        )
