from __future__ import annotations

from app.config.settings import Settings
from app.data.base_provider import (
    BaseMarketDataProvider,
    BrokerFlowProvider,
    DisabledBrokerFlowProvider,
    DisabledFundamentalProvider,
    DisabledKapProvider,
    FundamentalProvider,
    KapProvider,
)
from app.data.csv_provider import CsvMarketDataProvider
from app.data.mock_provider import MockMarketDataProvider
from app.data.reliable_provider import FileDataCache, ReliableMarketDataProvider, RetryPolicy
from app.data.yahoo_chart_provider import YahooChartMarketDataProvider
from app.data.yfinance_provider import YFinanceMarketDataProvider


def build_market_data_provider(settings: Settings) -> BaseMarketDataProvider:
    if settings.market_data_provider == "mock":
        return MockMarketDataProvider()
    if settings.market_data_provider == "csv":
        return CsvMarketDataProvider(csv_data_dir=settings.csv_data_dir)
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
        "(desteklenenler: 'mock' [sadece test/gelistirme], 'csv', 'yfinance')"
    )


def build_kap_provider(settings: Settings) -> KapProvider:
    # FAZ 1'de gercek/lisansli KAP entegrasyonu yok; her zaman disabled doner.
    return DisabledKapProvider()


def build_broker_flow_provider(settings: Settings) -> BrokerFlowProvider:
    # FAZ 1'de gercek/lisansli kurum verisi yok; her zaman disabled doner.
    return DisabledBrokerFlowProvider()


def build_fundamental_provider(settings: Settings) -> FundamentalProvider:
    return DisabledFundamentalProvider()
