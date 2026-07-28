from __future__ import annotations

from app.config.settings import Settings
from app.data.base_provider import (
    BaseMarketDataProvider,
    BrokerFlowProvider,
    DisabledBrokerFlowProvider,
    DisabledKapProvider,
    FundamentalProvider,
    KapProvider,
)
from app.data.csv_provider import CsvMarketDataProvider
from app.data.licensed_rest_provider import LicensedRestMarketDataProvider
from app.data.kap_rest_provider import LicensedKapDisclosureProvider
from app.data.mock_provider import MockMarketDataProvider
from app.data.reliable_provider import FileDataCache, ReliableMarketDataProvider, RetryPolicy
from app.data.yahoo_chart_provider import YahooChartMarketDataProvider
from app.data.yfinance_provider import YFinanceMarketDataProvider


def build_market_data_provider(settings: Settings) -> BaseMarketDataProvider:
    if settings.market_data_provider == "mock":
        return MockMarketDataProvider()
    if settings.market_data_provider == "csv":
        return CsvMarketDataProvider(csv_data_dir=settings.csv_data_dir)
    if settings.market_data_provider == "licensed_rest":
        return LicensedRestMarketDataProvider(
            base_url=settings.licensed_market_data_base_url,
            api_key=settings.licensed_market_data_api_key,
            api_key_header=settings.licensed_market_data_api_key_header,
            quote_path=settings.licensed_market_data_quote_path,
            ohlcv_path=settings.licensed_market_data_ohlcv_path,
            market_state_path=settings.licensed_market_data_market_state_path,
            provider_name=settings.licensed_market_data_provider_name,
            timeout_seconds=settings.licensed_market_data_timeout_seconds,
        )
    if settings.market_data_provider == "yfinance":
        technical_price_mode = (
            "adjusted" if settings.price_adjustment_mode == "adjusted" else "unadjusted"
        )
        primary: BaseMarketDataProvider = YFinanceMarketDataProvider(
            timeout_seconds=settings.yfinance_timeout_seconds,
            max_retries=settings.yfinance_max_retries,
            request_delay_seconds=settings.yfinance_request_delay_seconds,
            price_mode=technical_price_mode,
        )
        fallback = (
            YahooChartMarketDataProvider(
                timeout_seconds=settings.yfinance_timeout_seconds,
                price_mode=technical_price_mode,
            )
            if settings.yahoo_chart_fallback_enabled
            else None
        )
        # Cache kapalı olsa bile ReliableMarketDataProvider aynı arayüzü korur;
        # çok küçük bir maksimum yaşla eski verinin kullanılmasını engeller.
        cache = FileDataCache(settings.data_cache_dir)
        return ReliableMarketDataProvider(
            primary,
            fallback,
            cache,
            retry_policy=RetryPolicy(
                max_attempts=settings.provider_retry_max_attempts,
                base_delay_seconds=settings.provider_retry_base_seconds,
            ),
            circuit_failure_threshold=settings.provider_circuit_failure_threshold,
            circuit_recovery_seconds=settings.provider_circuit_recovery_seconds,
            cache_max_age_daily_minutes=(settings.data_cache_max_age_daily_minutes if settings.cache_enabled else 0),
            cache_max_age_intraday_minutes=(settings.data_cache_max_age_intraday_minutes if settings.cache_enabled else 0),
            price_mode=technical_price_mode,
        )
    raise ValueError(
        f"Bilinmeyen MARKET_DATA_PROVIDER: {settings.market_data_provider} "
        "(desteklenenler: 'mock' [sadece test/gelistirme], 'csv', 'yfinance', 'licensed_rest')"
    )


def build_kap_provider(settings: Settings) -> KapProvider:
    if str(settings.kap_provider).casefold() == "kap_rest":
        return LicensedKapDisclosureProvider(
            base_url=settings.kap_rest_base_url,
            api_key=settings.kap_rest_api_key,
            api_key_header=settings.kap_rest_api_key_header,
            disclosures_path=settings.kap_rest_disclosures_path,
            disclosure_detail_path=settings.kap_rest_disclosure_detail_path,
            symbol_query_param=settings.kap_rest_symbol_query_param,
            timeout_seconds=settings.fundamental_timeout_seconds,
        )
    return DisabledKapProvider()


def build_broker_flow_provider(settings: Settings) -> BrokerFlowProvider:
    # FAZ 1'de gercek/lisansli kurum verisi yok; her zaman disabled doner.
    return DisabledBrokerFlowProvider()


def build_fundamental_provider(settings: Settings) -> FundamentalProvider:
    from app.fundamentals.factory import build_fundamental_provider as build_normalized_provider
    from app.fundamentals.legacy_adapter import LegacyFundamentalProviderAdapter

    return LegacyFundamentalProviderAdapter(build_normalized_provider(settings))
