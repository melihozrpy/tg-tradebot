from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pandas as pd
import pytest

from app.fundamentals import (
    CrossCheckMismatchError,
    DisabledFundamentalDataProvider,
    FallbackFundamentalDataProvider,
    FintablesMcpProvider,
    FundamentalCrossCheckService,
    FundamentalDataProvider,
    FundamentalSnapshot,
    LicensedKapRestProvider,
    ProviderConfigurationError,
    ProviderResponseError,
    ProviderUnavailableError,
    SourceTrust,
    YahooFundamentalProvider,
    build_fundamental_provider,
)
from app.fundamentals.normalizer import snapshot_from_payload
from app.fundamentals.legacy_adapter import LegacyFundamentalProviderAdapter
from app.services.company_analysis_service import analyze_company, format_company_analysis


def _payload(*, latest_net_income: int = 12_000_000) -> dict:
    return {
        "symbol": "THYAO",
        "company": {
            "name": "Türk Hava Yolları",
            "sector": "Ulaştırma",
            "industry": "Havayolu",
            "summary": "Yolcu ve kargo taşımacılığı yapar.",
        },
        "source_url": "https://www.kap.org.tr/",
        "periods": [
            {
                "period_end": "2025-12-31",
                "period_type": "annual",
                "revision": "R2",
                "consolidated": True,
                "currency": "TRY",
                "published_at": "2026-03-01T10:00:00+03:00",
                "values": {
                    "revenue": 120_000_000,
                    "net_income": latest_net_income,
                    "gross_profit": 48_000_000,
                    "operating_income": 20_000_000,
                    "ebitda": 20_000_000,
                    "operating_cash_flow": 18_000_000,
                    "capital_expenditure": 5_000_000,
                    "total_assets": 180_000_000,
                    "total_liabilities": 100_000_000,
                    "total_equity": 80_000_000,
                    "total_debt": 40_000_000,
                    "cash_and_equivalents": 10_000_000,
                    "current_assets": 60_000_000,
                    "current_liabilities": 30_000_000,
                    "market_cap": 120_000_000,
                },
            },
            {
                "period_end": "2024-12-31",
                "period_type": "annual",
                "revision": "R1",
                "consolidated": True,
                "currency": "TRY",
                "values": {
                    "revenue": 100_000_000,
                    "net_income": 8_000_000,
                    "total_equity": 70_000_000,
                },
            },
        ],
    }


def _snapshot(payload: dict | None = None, *, provider: str = "kap_rest", trust=SourceTrust.PRIMARY):
    return snapshot_from_payload(
        payload or _payload(),
        provider=provider,
        trust=trust,
        requested_symbol="THYAO",
        default_source_url="https://source.example/THYAO",
    )


def test_normalizer_preserves_provenance_and_calculates_ratios_deterministically():
    result = _snapshot()
    latest = result.latest_period
    assert latest.source == "kap_rest"
    assert latest.period_end.isoformat() == "2025-12-31"
    assert latest.revision == "R2"
    assert latest.consolidated is True
    assert latest.currency == "TRY"
    assert result.provenance.trust is SourceTrust.PRIMARY
    assert result.ratios.revenue_growth == Decimal("0.2")
    assert result.ratios.earnings_growth == Decimal("0.5")
    assert result.ratios.profit_margin == Decimal("0.1")
    assert result.ratios.debt_to_equity == Decimal("50.0")
    assert result.ratios.current_ratio == Decimal("2")
    assert result.ratios.free_cash_flow == Decimal("13000000")
    assert result.ratios.return_on_equity == Decimal("0.16")
    assert result.ratios.trailing_pe == Decimal("10")
    assert result.ratios.price_to_book == Decimal("1.5")
    assert result.ratios.enterprise_to_ebitda == Decimal("7.5")
    assert result.ratios.net_debt == Decimal("30000000")


def test_quarterly_cumulative_growth_uses_same_duration_prior_year_only():
    payload = {
        "symbol": "THYAO",
        "periods": [
            {
                "period_end": "2026-06-30",
                "period_type": "quarterly",
                "duration_months": 6,
                "flow_basis": "cumulative_ytd",
                "currency": "TRY",
                "values": {"revenue": 150, "net_income": 30},
            },
            {
                "period_end": "2026-03-31",
                "period_type": "quarterly",
                "duration_months": 3,
                "flow_basis": "cumulative_ytd",
                "currency": "TRY",
                "values": {"revenue": 80, "net_income": 20},
            },
            {
                "period_end": "2025-06-30",
                "period_type": "quarterly",
                "duration_months": 6,
                "flow_basis": "cumulative_ytd",
                "currency": "TRY",
                "values": {"revenue": 100, "net_income": 20},
            },
        ],
    }
    snapshot = snapshot_from_payload(
        payload,
        provider="kap_rest",
        trust=SourceTrust.PRIMARY,
        requested_symbol="THYAO",
        default_source_url="https://kap.org.tr/",
    )
    assert snapshot.ratios.revenue_growth == Decimal("0.5")
    assert snapshot.ratios.earnings_growth == Decimal("0.5")

    payload["periods"] = payload["periods"][:2]
    unknown = snapshot_from_payload(
        payload,
        provider="kap_rest",
        trust=SourceTrust.PRIMARY,
        requested_symbol="THYAO",
        default_source_url="https://kap.org.tr/",
    )
    assert unknown.ratios.revenue_growth is None
    assert unknown.ratios.earnings_growth is None


def test_fundamental_market_cap_share_price_scale_mismatch_is_rejected():
    payload = _payload()
    payload["periods"][0]["values"].update(
        {
            "market_cap": 120_000_000,
            "shares_outstanding": 1_000,
            "last_price": 10,
        }
    )
    with pytest.raises(ProviderResponseError, match="ölçek"):
        _snapshot(payload)


def test_fintables_mcp_uses_oauth_and_configured_tool_with_streamable_http():
    calls: list[dict] = []
    auth_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        auth_headers.append(request.headers["authorization"])
        if body["method"] == "initialize":
            message = {
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {}},
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream", "MCP-Session-Id": "session-1"},
                text=f"event: message\ndata: {json.dumps(message)}\n\n",
            )
        if body["method"] == "notifications/initialized":
            assert request.headers["mcp-session-id"] == "session-1"
            return httpx.Response(202)
        message = {
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {"structuredContent": _payload()},
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=f"data: {json.dumps(message)}\n\n",
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = FintablesMcpProvider(
            endpoint="https://evo.fintables.com/mcp",
            bearer_token="user-oauth-token",
            tool_name="bist_financials",
            symbol_argument="ticker",
            tool_arguments={"periods": 8},
            client=client,
        )
        result = provider.fetch("THYAO.IS")

    assert result.symbol == "THYAO"
    assert result.provenance.provider == "fintables_mcp"
    assert all(header == "Bearer user-oauth-token" for header in auth_headers)
    tool_call = calls[-1]
    assert tool_call["method"] == "tools/call"
    assert tool_call["params"] == {
        "name": "bist_financials",
        "arguments": {"periods": 8, "ticker": "THYAO"},
    }


def test_fintables_rejects_missing_token_and_non_https_endpoint():
    with pytest.raises(ProviderConfigurationError):
        FintablesMcpProvider(endpoint="https://evo.fintables.com/mcp", bearer_token="", tool_name="x")
    with pytest.raises(ProviderConfigurationError):
        FintablesMcpProvider(endpoint="http://evo.fintables.com/mcp", bearer_token="secret", tool_name="x")


def test_fintables_can_discover_financial_tool_and_symbol_argument():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        method = body["method"]
        if method == "initialize":
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"protocolVersion": "2025-03-26"},
            })
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": body["id"],
                "result": {"tools": [{
                    "name": "company_financial_statements",
                    "description": "BIST company financial statements",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"ticker": {"type": "string"}},
                        "required": ["ticker"],
                    },
                }]},
            })
        return httpx.Response(200, json={
            "jsonrpc": "2.0", "id": body["id"],
            "result": {"structuredContent": _payload()},
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = FintablesMcpProvider(
            endpoint="https://evo.fintables.com/mcp",
            bearer_token="oauth",
            tool_name="",
            client=client,
        )
        result = provider.fetch("THYAO")
    assert result.symbol == "THYAO"
    tool_call = next(item for item in calls if item["method"] == "tools/call")
    assert tool_call["params"] == {
        "name": "company_financial_statements",
        "arguments": {"ticker": "THYAO"},
    }


def test_licensed_kap_adapter_uses_api_key_and_contract_path():
    observed: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["key"] = request.headers["x-kap-key"]
        return httpx.Response(200, json={"result": _payload()})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = LicensedKapRestProvider(
            base_url="https://licensed-kap.example/api",
            api_key="kap-secret",
            endpoint_path_template="/company/{symbol}/financials",
            api_key_header="X-KAP-Key",
            client=client,
        )
        result = provider.fetch("thyao")

    assert observed == {
        "url": "https://licensed-kap.example/api/company/THYAO/financials",
        "key": "kap-secret",
    }
    assert result.provenance.trust is SourceTrust.PRIMARY
    assert result.latest_period.revision == "R2"


def test_yahoo_fallback_normalizes_statements_and_marks_secondary_source():
    class FakeTicker:
        info = {
            "longName": "Örnek Sanayi",
            "sector": "Industrials",
            "industry": "Machinery",
            "longBusinessSummary": "Makine üretir.",
            "financialCurrency": "TRY",
            "marketCap": 120_000_000,
            "enterpriseValue": 150_000_000,
            "totalRevenue": 120_000_000,
            "netIncomeToCommon": 12_000_000,
            "ebitda": 20_000_000,
        }
        columns = pd.to_datetime(["2024-12-31", "2025-12-31"])
        quarterly_income_stmt = pd.DataFrame(
            [[100_000_000, 120_000_000], [8_000_000, 12_000_000], [16_000_000, 20_000_000]],
            index=["Total Revenue", "Net Income", "EBITDA"],
            columns=columns,
        )
        quarterly_balance_sheet = pd.DataFrame(
            [[70_000_000, 80_000_000], [35_000_000, 40_000_000], [8_000_000, 10_000_000]],
            index=["Stockholders Equity", "Total Debt", "Cash And Cash Equivalents"],
            columns=columns,
        )
        quarterly_cashflow = pd.DataFrame(
            [[15_000_000, 18_000_000], [-4_000_000, -5_000_000]],
            index=["Operating Cash Flow", "Capital Expenditure"],
            columns=columns,
        )

    provider = YahooFundamentalProvider(ticker_factory=lambda _symbol: FakeTicker())
    result = provider.fetch("ORNEK")
    assert result.provenance.trust is SourceTrust.SECONDARY
    assert "KAP ile doğrulanmalıdır" in result.provenance.notes[0]
    assert result.latest_period.currency == "TRY"
    assert result.latest_period.value("revenue") == Decimal("120000000")
    assert result.ratios.free_cash_flow == Decimal("13000000")
    assert result.ratios.trailing_pe == Decimal("10")


def test_yahoo_uses_financial_statements_when_profile_endpoint_fails():
    class StatementOnlyTicker:
        @property
        def info(self):
            raise RuntimeError("profile unavailable")

        fast_info = {"currency": "TRY", "last_price": 100}
        columns = pd.to_datetime(["2025-03-31", "2026-03-31"])
        quarterly_income_stmt = pd.DataFrame(
            [[100, 120], [10, 14]],
            index=["Total Revenue", "Net Income"],
            columns=columns,
        )
        quarterly_balance_sheet = pd.DataFrame()
        quarterly_cashflow = pd.DataFrame()

    snapshot = YahooFundamentalProvider(ticker_factory=lambda _symbol: StatementOnlyTicker()).fetch("THYAO")
    assert snapshot.latest_period.value("revenue") == Decimal("120")
    assert any("profil" in note.casefold() for note in snapshot.provenance.notes)


def test_yahoo_never_mixes_quote_and_statement_currencies_in_valuation():
    class MixedCurrencyTicker:
        info = {
            "financialCurrency": "USD",
            "currency": "TRY",
            "marketCap": 1_000_000_000,
            "enterpriseValue": 1_200_000_000,
        }
        columns = pd.to_datetime(["2025-03-31", "2026-03-31"])
        quarterly_income_stmt = pd.DataFrame(
            [[100, 120], [10, 12]],
            index=["Total Revenue", "Net Income"],
            columns=columns,
        )
        quarterly_balance_sheet = pd.DataFrame()
        quarterly_cashflow = pd.DataFrame()

    snapshot = YahooFundamentalProvider(ticker_factory=lambda _symbol: MixedCurrencyTicker()).fetch("THYAO")
    assert snapshot.latest_period.currency == "USD"
    assert snapshot.latest_period.value("market_cap") is None
    assert snapshot.ratios.trailing_pe is None
    assert any("kur dönüşümü olmadan" in note for note in snapshot.provenance.notes)


def test_fallback_chain_uses_secondary_source_without_relabeling_it():
    unavailable = _StaticProvider("fintables", error=ProviderUnavailableError("down"))
    yahoo_snapshot = _snapshot(provider="yahoo_finance", trust=SourceTrust.SECONDARY)
    fallback = _StaticProvider("yahoo_finance", snapshot=yahoo_snapshot)
    result = FallbackFundamentalDataProvider(unavailable, fallback).fetch("THYAO")
    assert result.provenance.provider == "yahoo_finance"
    assert result.provenance.trust is SourceTrust.SECONDARY


class _StaticProvider(FundamentalDataProvider):
    def __init__(self, name: str, snapshot: FundamentalSnapshot | None = None, error: Exception | None = None):
        self.name = name
        self.snapshot = snapshot
        self.error = error

    def fetch(self, symbol: str) -> FundamentalSnapshot:
        if self.error:
            raise self.error
        if self.snapshot is None:
            raise ProviderUnavailableError(f"{self.name} snapshot sağlamadı")
        return self.snapshot


def test_cross_check_is_strict_and_does_not_silently_fallback():
    primary_failure = _StaticProvider("kap", error=ProviderUnavailableError("kap down"))
    secondary_snapshot = _snapshot(provider="yahoo", trust=SourceTrust.SECONDARY)
    secondary = _StaticProvider("yahoo", snapshot=secondary_snapshot)
    service = FundamentalCrossCheckService(primary_failure, secondary)
    with pytest.raises(ProviderUnavailableError, match="kap down"):
        service.resolve("THYAO")

    explicit_fallback = FundamentalCrossCheckService(
        primary_failure,
        secondary,
        allow_secondary_fallback=True,
    ).resolve("THYAO")
    assert explicit_fallback.used_fallback is True
    assert explicit_fallback.verified is False
    assert explicit_fallback.snapshot.provenance.trust is SourceTrust.SECONDARY


def test_cross_check_detects_material_statement_mismatch():
    primary = _StaticProvider("kap", snapshot=_snapshot())
    secondary_snapshot = _snapshot(
        _payload(latest_net_income=9_000_000),
        provider="licensed_secondary",
        trust=SourceTrust.LICENSED,
    )
    secondary = _StaticProvider("licensed_secondary", snapshot=secondary_snapshot)
    with pytest.raises(CrossCheckMismatchError, match="net_income"):
        FundamentalCrossCheckService(primary, secondary).resolve("THYAO")

    report = FundamentalCrossCheckService(primary, secondary, strict=False).resolve("THYAO")
    assert report.verified is False
    assert any(item.field == "net_income" and not item.matches for item in report.comparisons)
    assert "Uyuşmayan alanlar: net_income" in report.warnings


def test_disabled_provider_fails_closed():
    with pytest.raises(ProviderUnavailableError):
        DisabledFundamentalDataProvider().fetch("THYAO")


def test_factory_prefers_authorized_fintables_and_never_scrapes():
    settings = SimpleNamespace(
        fundamental_provider="auto",
        fintables_mcp_url="https://evo.fintables.com/mcp",
        fintables_mcp_bearer_token="oauth-token",
        fintables_mcp_tool_name="financials",
        fintables_mcp_symbol_argument="symbol",
        kap_rest_base_url="",
        kap_rest_api_key="",
        fundamental_allow_yahoo_fallback=False,
        fundamental_cross_check_enabled=False,
        fundamental_timeout_seconds=20,
    )
    provider = build_fundamental_provider(settings)
    assert isinstance(provider, FintablesMcpProvider)
    assert provider.endpoint == "https://evo.fintables.com/mcp"


def test_factory_accepts_legacy_fintables_oauth_env_name():
    settings = SimpleNamespace(
        fundamental_provider="fintables_mcp",
        fintables_mcp_url="https://evo.fintables.com/mcp",
        fintables_mcp_bearer_token="",
        fintables_oauth_bearer_token="legacy-oauth-token",
        fintables_mcp_tool_name="financials",
        fintables_mcp_symbol_argument="symbol",
        kap_rest_base_url="",
        kap_rest_api_key="",
        fundamental_allow_yahoo_fallback=False,
        fundamental_cross_check_enabled=False,
        fundamental_timeout_seconds=20,
    )
    provider = build_fundamental_provider(settings)
    assert isinstance(provider, FintablesMcpProvider)


def test_explicit_cross_check_source_must_be_configured_and_independent():
    settings = SimpleNamespace(
        fundamental_provider="fintables_mcp",
        fundamental_secondary_provider="kap_rest",
        fintables_mcp_url="https://evo.fintables.com/mcp",
        fintables_mcp_bearer_token="oauth-token",
        fintables_mcp_tool_name="financials",
        fintables_mcp_symbol_argument="symbol",
        kap_rest_base_url="",
        kap_rest_api_key="",
        fundamental_allow_yahoo_fallback=False,
        fundamental_cross_check_enabled=True,
        fundamental_timeout_seconds=20,
    )
    provider = build_fundamental_provider(settings)
    assert isinstance(provider, DisabledFundamentalDataProvider)
    with pytest.raises(ProviderUnavailableError, match="İkincil"):
        provider.fetch("THYAO")


def test_factory_fails_closed_without_licensed_credentials_or_explicit_fallback():
    settings = SimpleNamespace(
        fundamental_provider="auto",
        fintables_mcp_url="https://evo.fintables.com/mcp",
        fintables_mcp_bearer_token="",
        fintables_mcp_tool_name="",
        kap_rest_base_url="",
        kap_rest_api_key="",
        fundamental_allow_yahoo_fallback=False,
        fundamental_cross_check_enabled=False,
    )
    provider = build_fundamental_provider(settings)
    assert isinstance(provider, DisabledFundamentalDataProvider)
    with pytest.raises(ProviderUnavailableError):
        provider.fetch("THYAO")


def test_legacy_disabled_mode_uses_explicitly_allowed_yahoo_fallback():
    settings = SimpleNamespace(
        fundamental_provider="disabled",
        fintables_mcp_url="https://evo.fintables.com/mcp",
        fintables_mcp_bearer_token="",
        fintables_mcp_tool_name="",
        kap_rest_base_url="",
        kap_rest_api_key="",
        fundamental_allow_yahoo_fallback=True,
        fundamental_cross_check_enabled=False,
    )
    provider = build_fundamental_provider(settings)
    assert isinstance(provider, YahooFundamentalProvider)


def test_company_analysis_accepts_normalized_provider_and_displays_provenance():
    provider = _StaticProvider("kap", snapshot=_snapshot())
    analysis = analyze_company("THYAO", fundamental_provider=provider)
    rendered = format_company_analysis(analysis)
    assert analysis.financial_period == "2025-12-31"
    assert analysis.metrics["trailing_pe"] == 10.0
    assert analysis.status == "GÜÇLÜ"
    assert "kap_rest • primary • 2025-12-31 • R2 • konsolide • TRY" in rendered
    assert "Ciro: 120.0 mn TL • dönemsel %+20.0" in rendered
    assert "NE ANLAMA GELİYOR?" in rendered
    assert "tek başına AL sinyali değildir" in rendered


def test_normalized_fundamentals_feed_existing_valuation_interface_once():
    provider = _StaticProvider("kap", snapshot=_snapshot())
    adapter = LegacyFundamentalProviderAdapter(provider)
    balance = adapter.get_balance_sheet("THYAO")
    income = adapter.get_income_statement("THYAO")
    ratios = adapter.get_ratios("THYAO")
    assert balance["total_equity"] == Decimal("80000000")
    assert balance["source"] == "kap_rest"
    assert income["revenue"] == Decimal("120000000")
    assert ratios["trailing_pe"] == Decimal("10")


def test_legacy_adapter_cache_expires_and_never_caches_provider_outage():
    class RecoveringProvider(FundamentalDataProvider):
        name = "recovering"

        def __init__(self):
            self.calls = 0

        def fetch(self, symbol: str) -> FundamentalSnapshot:
            self.calls += 1
            if self.calls == 1:
                raise ProviderUnavailableError("temporary")
            return _snapshot()

    provider = RecoveringProvider()
    clock = [100.0]
    adapter = LegacyFundamentalProviderAdapter(
        provider,
        cache_ttl_seconds=10,
        clock=lambda: clock[0],
    )
    assert adapter.get_ratios("THYAO")["status"] == "unavailable"
    assert adapter.get_ratios("THYAO")["status"] == "available"
    assert provider.calls == 2
    adapter.get_income_statement("THYAO")
    assert provider.calls == 2
    clock[0] = 111.0
    adapter.get_balance_sheet("THYAO")
    assert provider.calls == 3
