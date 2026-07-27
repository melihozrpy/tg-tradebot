from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.data.base_provider import DataUnavailableError, KapProvider


class LicensedKapDisclosureProvider(KapProvider):
    """Contracted KAP REST disclosure adapter; never scrapes kap.org.tr pages."""

    name = "kap_rest"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_key_header: str = "X-API-Key",
        disclosures_path: str = "/disclosures",
        disclosure_detail_path: str = "/disclosureDetail/{id}",
        symbol_query_param: str = "symbol",
        timeout_seconds: float = 20.0,
        client: httpx.Client | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
        if (parsed.scheme != "https" and not local_http) or not parsed.netloc:
            raise ValueError("KAP REST adresi HTTPS olmalıdır.")
        if not api_key.strip():
            raise ValueError("KAP REST API anahtarı eksik.")
        if "{id}" not in disclosure_detail_path:
            raise ValueError("KAP detay yolu {id} alanını içermelidir.")
        if not api_key_header.strip() or any(char in api_key_header for char in "\r\n"):
            raise ValueError("KAP API anahtarı başlığı geçersiz.")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key.strip()
        self.api_key_header = api_key_header.strip()
        self.disclosures_path = disclosures_path
        self.disclosure_detail_path = disclosure_detail_path
        self.symbol_query_param = symbol_query_param.strip() or "symbol"
        self.timeout_seconds = timeout_seconds
        self._client = client

    def _get(self, path: str, *, params: dict[str, str] | None = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {self.api_key_header: self._api_key, "Accept": "application/json"}

        def execute(client: httpx.Client):
            try:
                response = client.get(url, headers=headers, params=params)
            except httpx.HTTPError as exc:
                raise DataUnavailableError("KAP REST bağlantısı kurulamadı.") from exc
            if response.status_code >= 400:
                raise DataUnavailableError(f"KAP REST HTTP {response.status_code} hatası verdi.")
            try:
                return response.json()
            except ValueError as exc:
                raise DataUnavailableError("KAP REST geçersiz JSON döndürdü.") from exc

        if self._client is not None:
            return execute(self._client)
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=False) as client:
            return execute(client)

    @staticmethod
    def _symbol(value: str) -> str:
        symbol = value.strip().upper().removesuffix(".IS")
        if not symbol.isalnum() or not 3 <= len(symbol) <= 12:
            raise DataUnavailableError("Geçersiz BIST sembolü.")
        return symbol

    @staticmethod
    def _items(payload: Any) -> list[Mapping[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, Mapping)]
        if isinstance(payload, Mapping):
            for key in ("result", "data", "items", "disclosures"):
                value = payload.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, Mapping)]
                if isinstance(value, Mapping):
                    nested = value.get("items") or value.get("disclosures")
                    if isinstance(nested, list):
                        return [item for item in nested if isinstance(item, Mapping)]
        raise DataUnavailableError("KAP bildirim listesi yanıtı doğrulanamadı.")

    @staticmethod
    def _time(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def classify_disclosure(disclosure: dict) -> str:
        text = " ".join(str(disclosure.get(key) or "") for key in ("title", "subject", "summary")).casefold()
        negative = ("iflas", "konkordato", "faaliyet durdur", "zarar", "ceza", "dava", "temerrüt")
        positive = ("ihale", "sözleşme", "sipariş", "yatırım", "temettü", "geri alım", "kapasite art")
        if any(word in text for word in negative):
            return "NEGATIF_OLABILIR"
        if any(word in text for word in positive):
            return "POZITIF_OLABILIR"
        return "BELIRSIZ"

    def get_latest_disclosures(self, symbol: str) -> list[dict]:
        normalized = self._symbol(symbol)
        raw = self._items(self._get(self.disclosures_path, params={self.symbol_query_param: normalized}))
        output: list[dict] = []
        for item in raw:
            disclosure_id = item.get("disclosureIndex") or item.get("disclosureId") or item.get("id")
            title = item.get("title") or item.get("subject") or item.get("header")
            if disclosure_id is None or not str(title or "").strip():
                continue
            source_url = item.get("sourceUrl") or item.get("url") or f"https://www.kap.org.tr/tr/Bildirim/{quote(str(disclosure_id), safe='')}"
            row = {
                "id": str(disclosure_id),
                "symbol": normalized,
                "title": str(title).strip()[:1000],
                "published_at": self._time(item.get("publishedAt") or item.get("publishDate") or item.get("date")),
                "source_url": str(source_url),
                "summary": str(item.get("summary") or "").strip()[:2000] or None,
            }
            row["classification"] = self.classify_disclosure(row)
            output.append(row)
        output.sort(key=lambda item: item["published_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return output

    def get_disclosure_detail(self, disclosure_id: str) -> dict:
        safe_id = quote(str(disclosure_id).strip(), safe="")
        payload = self._get(self.disclosure_detail_path.format(id=safe_id))
        if not isinstance(payload, Mapping):
            raise DataUnavailableError("KAP bildirim detayı doğrulanamadı.")
        result = payload.get("result")
        return dict(result if isinstance(result, Mapping) else payload)

    def get_upcoming_financial_dates(self, symbol: str) -> list:
        # Resmi hizmet listesinde takvim için ayrı, belgelenmiş servis yoktur.
        # Bilgi uydurmak yerine boş döner.
        self._symbol(symbol)
        return []
