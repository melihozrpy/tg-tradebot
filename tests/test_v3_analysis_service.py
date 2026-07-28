from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from app.data.csv_provider import CsvMarketDataProvider
from app.config.settings import Settings
from app.data.base_provider import BaseMarketDataProvider, DataFreshness, DataUnavailableError
from app.data.mock_provider import MockMarketDataProvider
from app.models.database import Signal
from app.services.analysis_service_v3 import AnalysisUnavailableErrorV3, run_symbol_analysis_v3


def _v3_settings(**overrides) -> Settings:
    base = dict(telegram_bot_token="x", xu100_symbol="XU100", market_data_provider="mock")
    base.update(overrides)
    return Settings(**base)


def test_confirmed_close_persists_signal(db_session):
    # CSV saglayicinin ornek verisi gecmis bir tarihte biter (gercek "simdi"
    # zamanina bagli olmadan her zaman kesinlesmis kapanis testi icin uygundur).
    provider = CsvMarketDataProvider(csv_data_dir="data_csv")
    settings = _v3_settings()
    outcome = run_symbol_analysis_v3(db_session, provider, "THYAO", settings)
    assert outcome.mode == "confirmed_close"
    assert outcome.is_new_signal is True
    count = db_session.query(Signal).filter(Signal.symbol == "THYAO").count()
    assert count == 1


def test_duplicate_same_day_signal_not_persisted_twice(db_session):
    provider = CsvMarketDataProvider(csv_data_dir="data_csv")
    settings = _v3_settings()
    run_symbol_analysis_v3(db_session, provider, "THYAO", settings)
    outcome2 = run_symbol_analysis_v3(db_session, provider, "THYAO", settings)
    assert outcome2.is_duplicate_or_cooldown is True
    count = db_session.query(Signal).filter(Signal.symbol == "THYAO").count()
    assert count == 1


class _XU100FailingProvider(MockMarketDataProvider):
    """XU100 istegi her zaman basarisiz olan, ama sembol verisi normal calisan test saglayicisi."""

    def get_ohlcv(self, symbol, timeframe, start, end):
        if symbol == "XU100":
            raise DataUnavailableError("XU100 test hatasi")
        return super().get_ohlcv(symbol, timeframe, start, end)

    def get_index_data(self, index_symbol, timeframe):
        raise DataUnavailableError("XU100 test hatasi")


def test_xu100_unavailable_does_not_crash_analysis(db_session):
    provider = _XU100FailingProvider()
    settings = _v3_settings()
    outcome = run_symbol_analysis_v3(db_session, provider, "THYAO", settings)
    assert outcome.signal.market_regime == "veri_yetersiz"
    assert outcome.xu100_relative_strength.available is False
    assert any("XU100" in w for w in outcome.warnings)


class _AlwaysFailingProvider(BaseMarketDataProvider):
    name = "always_failing"

    def get_quote(self, symbol):
        raise DataUnavailableError("yok")

    def get_ohlcv(self, symbol, timeframe, start, end):
        raise DataUnavailableError(f"'{symbol}' verisi yok")

    def get_index_data(self, index_symbol, timeframe):
        raise DataUnavailableError("yok")

    def is_market_open(self):
        return False

    def get_data_freshness(self, symbol, timeframe):
        return DataFreshness(symbol=symbol, timeframe=timeframe, last_timestamp=None, is_fresh=False, max_allowed_lag_minutes=0, provider=self.name)

    def health_check(self):
        return {"provider": self.name, "status": "down"}


def test_data_unavailable_does_not_fallback_to_mock_v3(db_session):
    provider = _AlwaysFailingProvider()
    settings = _v3_settings()
    with pytest.raises(AnalysisUnavailableErrorV3) as exc_info:
        run_symbol_analysis_v3(db_session, provider, "SVGYO", settings)
    assert "güncel veri alınamadı" in str(exc_info.value).lower() or "guncel veri alinamadi" in str(exc_info.value).lower()
    assert db_session.query(Signal).count() == 0


class _BadQualityProvider(BaseMarketDataProvider):
    """Negatif fiyat iceren bozuk veri donen test saglayicisi (veri kalitesi testi icin)."""

    name = "bad_quality"

    def get_quote(self, symbol):
        return {"symbol": symbol, "price": 10.0, "timestamp": datetime.now(timezone.utc), "provider": self.name}

    def get_ohlcv(self, symbol, timeframe, start, end):
        dates = pd.date_range(end=datetime.now(timezone.utc), periods=100, freq="1B", tz="UTC")
        closes = [10.0] * 100
        closes[50] = -5.0  # bozuk: negatif fiyat
        return pd.DataFrame({
            "timestamp": dates, "open": [10.0] * 100, "high": [10.5] * 100,
            "low": [9.5] * 100, "close": closes, "volume": [1000.0] * 100,
        })

    def get_index_data(self, index_symbol, timeframe):
        return self.get_ohlcv(index_symbol, timeframe, None, None)

    def is_market_open(self):
        return True

    def get_data_freshness(self, symbol, timeframe):
        return DataFreshness(symbol=symbol, timeframe=timeframe, last_timestamp=datetime.now(timezone.utc), is_fresh=True, max_allowed_lag_minutes=99999, provider=self.name)

    def health_check(self):
        return {"provider": self.name, "status": "ok"}


def test_bad_quality_data_blocks_signal_creation(db_session):
    provider = _BadQualityProvider()
    settings = _v3_settings()
    with pytest.raises(AnalysisUnavailableErrorV3) as exc_info:
        run_symbol_analysis_v3(db_session, provider, "SVGYO", settings)
    assert "kalite" in str(exc_info.value).lower()
    assert db_session.query(Signal).count() == 0
