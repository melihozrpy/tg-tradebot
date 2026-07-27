from __future__ import annotations

from types import SimpleNamespace

import httpx

from app.data.kap_rest_provider import LicensedKapDisclosureProvider
from app.data.provider_factory import build_kap_provider


def test_kap_rest_lists_and_classifies_verified_disclosures():
    observed = {}

    def handler(request):
        observed["key"] = request.headers["x-kap-key"]
        observed["symbol"] = request.url.params["stockCode"]
        return httpx.Response(200, json={"result": [{
            "disclosureIndex": 123,
            "title": "Yeni yatırım ve kapasite artışı kararı",
            "publishedAt": "2026-07-24T15:30:00+03:00",
        }]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = LicensedKapDisclosureProvider(
            base_url="https://kap-feed.example/api",
            api_key="secret",
            api_key_header="X-KAP-Key",
            symbol_query_param="stockCode",
            client=client,
        )
        rows = provider.get_latest_disclosures("THYAO.IS")
    assert observed == {"key": "secret", "symbol": "THYAO"}
    assert rows[0]["id"] == "123"
    assert rows[0]["classification"] == "POZITIF_OLABILIR"
    assert rows[0]["source_url"].endswith("/123")


def test_kap_rest_detail_path_and_factory_are_configurable():
    settings = SimpleNamespace(
        kap_provider="kap_rest",
        kap_rest_base_url="https://kap-feed.example/api",
        kap_rest_api_key="secret",
        kap_rest_api_key_header="X-Key",
        kap_rest_disclosures_path="/disclosures",
        kap_rest_disclosure_detail_path="/disclosureDetail/{id}",
        kap_rest_symbol_query_param="symbol",
        fundamental_timeout_seconds=12,
    )
    provider = build_kap_provider(settings)
    assert isinstance(provider, LicensedKapDisclosureProvider)
    assert provider.api_key_header == "X-Key"
