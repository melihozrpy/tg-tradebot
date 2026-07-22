from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.data.base_provider import BaseMarketDataProvider, DataFreshness, DataUnavailableError
from app.services.analysis_service import AnalysisUnavailableError, run_symbol_analysis


class _AlwaysFailingProvider(BaseMarketDataProvider):
    """Gercek veri bulunamama durumunu simule eden test saglayicisi.

    Bu sahte saglayici KESINLIKLE mock veriye donmez; sadece hata firlatir.
    Boylece run_symbol_analysis'in gercekten fail-closed davrandigini ve
    hicbir zaman arka planda sessizce mock/sahte veriye gecmedigini test ederiz.
    """

    name = "always_failing_test_provider"

    def get_quote(self, symbol: str) -> dict:
        raise DataUnavailableError("test: veri yok")

    def get_ohlcv(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        raise DataUnavailableError(f"'{symbol}' icin veri bulunamadi (test provider).")

    def get_index_data(self, index_symbol: str, timeframe: str) -> pd.DataFrame:
        raise DataUnavailableError("test: endeks verisi yok")

    def is_market_open(self) -> bool:
        return False

    def get_data_freshness(self, symbol: str, timeframe: str) -> DataFreshness:
        return DataFreshness(
            symbol=symbol, timeframe=timeframe, last_timestamp=None,
            is_fresh=False, max_allowed_lag_minutes=0, provider=self.name,
        )

    def health_check(self) -> dict:
        return {"provider": self.name, "status": "down", "detail": "test"}


class _TinyDataProvider(BaseMarketDataProvider):
    """Yetersiz (cok az barli) veri donen test saglayicisi."""

    name = "tiny_data_test_provider"

    def get_quote(self, symbol: str) -> dict:
        return {"symbol": symbol, "price": 10.0, "timestamp": datetime.now(timezone.utc), "provider": self.name}

    def get_ohlcv(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        dates = pd.date_range(end=datetime.now(timezone.utc), periods=5, freq="1B", tz="UTC")
        return pd.DataFrame(
            {
                "timestamp": dates,
                "open": [10.0] * 5,
                "high": [10.5] * 5,
                "low": [9.5] * 5,
                "close": [10.2] * 5,
                "volume": [1000.0] * 5,
            }
        )

    def get_index_data(self, index_symbol: str, timeframe: str) -> pd.DataFrame:
        return self.get_ohlcv(index_symbol, timeframe, datetime.now(timezone.utc) - timedelta(days=10), datetime.now(timezone.utc))

    def is_market_open(self) -> bool:
        return True

    def get_data_freshness(self, symbol: str, timeframe: str) -> DataFreshness:
        return DataFreshness(
            symbol=symbol, timeframe=timeframe, last_timestamp=datetime.now(timezone.utc),
            is_fresh=True, max_allowed_lag_minutes=99999, provider=self.name,
        )

    def health_check(self) -> dict:
        return {"provider": self.name, "status": "ok", "detail": "test"}


def test_no_fallback_to_mock_when_data_unavailable(db_session, strategy_config):
    provider = _AlwaysFailingProvider()
    with pytest.raises(AnalysisUnavailableError) as exc_info:
        run_symbol_analysis(db_session, provider, "SVGYO", "1d", strategy_config)
    assert "güncel veri alınamadı" in str(exc_info.value) or "guncel veri alinamadi" in str(exc_info.value).lower()


def test_no_signal_created_when_data_unavailable(db_session, strategy_config):
    from app.models.database import Signal

    provider = _AlwaysFailingProvider()
    with pytest.raises(AnalysisUnavailableError):
        run_symbol_analysis(db_session, provider, "SVGYO", "1d", strategy_config)

    count = db_session.query(Signal).count()
    assert count == 0


def test_no_signal_created_when_bars_insufficient(db_session, strategy_config):
    from app.models.database import Signal

    provider = _TinyDataProvider()
    with pytest.raises(AnalysisUnavailableError):
        run_symbol_analysis(db_session, provider, "SVGYO", "1d", strategy_config)

    count = db_session.query(Signal).count()
    assert count == 0
