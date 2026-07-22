from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.data.base_provider import DataUnavailableError
from app.data.csv_provider import CsvMarketDataProvider
from app.models.database import User
from app.services.scan_service import ScanBlockedByKillSwitchError, run_evening_scan


def _v3_settings(**overrides) -> Settings:
    base = dict(telegram_bot_token="x", xu100_symbol="XU100")
    base.update(overrides)
    return Settings(**base)


class _PartiallyFailingProvider(CsvMarketDataProvider):
    """THYAO ve ASELS icin normal calisir, 'BADSYM' icin her zaman hata verir."""

    def get_ohlcv(self, symbol, timeframe, start, end):
        if symbol == "BADSYM":
            raise DataUnavailableError("test: BADSYM verisi yok")
        return super().get_ohlcv(symbol, timeframe, start, end)

    def get_data_freshness(self, symbol, timeframe):
        if symbol == "BADSYM":
            from app.data.base_provider import DataFreshness
            return DataFreshness(symbol=symbol, timeframe=timeframe, last_timestamp=None, is_fresh=False, max_allowed_lag_minutes=0, provider=self.name)
        return super().get_data_freshness(symbol, timeframe)


def test_single_symbol_failure_does_not_stop_scan(db_session):
    provider = _PartiallyFailingProvider(csv_data_dir="data_csv")
    settings = _v3_settings()
    summary = run_evening_scan(db_session, provider, settings, symbols=["THYAO", "BADSYM", "ASELS"], persist=False)
    assert summary.symbols_scanned == 3
    assert summary.symbols_succeeded == 2
    assert summary.symbols_failed == 1
    assert any(sym == "BADSYM" for sym, _ in summary.failed_symbols)


def test_scan_candidates_sorted_by_score_descending(db_session):
    provider = CsvMarketDataProvider(csv_data_dir="data_csv")
    settings = _v3_settings()
    summary = run_evening_scan(db_session, provider, settings, symbols=["THYAO", "ASELS"], persist=False)
    scores = [outcome.advanced_score.total for _, outcome in summary.top_candidates]
    assert scores == sorted(scores, reverse=True)


def test_scan_blocked_when_kill_switch_active(db_session):
    user = User(telegram_user_id=555, kill_switch_active=True)
    db_session.add(user)
    db_session.commit()

    provider = CsvMarketDataProvider(csv_data_dir="data_csv")
    settings = _v3_settings()
    with pytest.raises(ScanBlockedByKillSwitchError):
        run_evening_scan(db_session, provider, settings, symbols=["THYAO"], persist=False)


def test_scan_persists_scan_record(db_session):
    from app.models.database import Scan

    provider = CsvMarketDataProvider(csv_data_dir="data_csv")
    settings = _v3_settings()
    run_evening_scan(db_session, provider, settings, symbols=["THYAO"], persist=True)
    assert db_session.query(Scan).count() == 1
