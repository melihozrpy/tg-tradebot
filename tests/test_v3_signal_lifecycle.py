from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from app.data.base_provider import BaseMarketDataProvider, DataFreshness
from app.models.database import Signal, SignalStateEnum, SignalTypeEnum
from app.services.signal_lifecycle_service import update_open_signals


class _FixedFutureProvider(BaseMarketDataProvider):
    """Sinyal olusturma tarihinden SONRAKI belirli bar'lari doner (test icin)."""

    name = "fixed_future"

    def __init__(self, future_bars: pd.DataFrame):
        self._future_bars = future_bars

    def get_quote(self, symbol):
        return {"symbol": symbol, "price": float(self._future_bars.iloc[-1]["close"]), "timestamp": datetime.now(timezone.utc), "provider": self.name}

    def get_ohlcv(self, symbol, timeframe, start, end):
        return self._future_bars

    def get_index_data(self, index_symbol, timeframe):
        return self._future_bars

    def is_market_open(self):
        return True

    def get_data_freshness(self, symbol, timeframe):
        return DataFreshness(symbol=symbol, timeframe=timeframe, last_timestamp=datetime.now(timezone.utc), is_fresh=True, max_allowed_lag_minutes=99999, provider=self.name)

    def health_check(self):
        return {"provider": self.name, "status": "ok"}


def _make_signal(db, stop=45.0, target_1=55.0, target_2=60.0, target_3=65.0, data_ts=None) -> Signal:
    data_ts = data_ts or (datetime.now(timezone.utc) - timedelta(days=5))
    sig = Signal(
        symbol="TESTX", timeframe="1d", signal_type=SignalTypeEnum.BUY_CANDIDATE, state=SignalStateEnum.WAITING_TRIGGER,
        score=80.0, confidence="yuksek", entry_zone_low=49.0, entry_zone_high=51.0, entry_trigger=51.0,
        stop_price=stop, target_1=target_1, target_2=target_2, target_3=target_3, risk_reward=2.5,
        market_regime="zayif_yukselis", strategy_version="1.0.0", data_timestamp=data_ts, provider="test",
        idempotency_key=f"test-key-{stop}-{target_1}-{datetime.now(timezone.utc).timestamp()}",
    )
    db.add(sig)
    db.commit()
    db.refresh(sig)
    return sig


def _bars(rows: list[dict], start_offset_days: int = 1) -> pd.DataFrame:
    base = datetime.now(timezone.utc) - timedelta(days=4)
    for i, r in enumerate(rows):
        r["timestamp"] = base + timedelta(days=i + start_offset_days)
    return pd.DataFrame(rows)


def test_stop_hit_changes_signal_state(db_session):
    sig = _make_signal(db_session, stop=45.0, target_1=55.0)
    future = _bars([{"open": 50, "high": 51, "low": 44.0, "close": 44.5, "volume": 1000}])
    provider = _FixedFutureProvider(future)
    result = update_open_signals(db_session, provider, conservative_execution=True)
    db_session.refresh(sig)
    assert sig.state == SignalStateEnum.STOP_HIT
    assert result["updated"] == 1


def test_target_1_hit_changes_signal_state(db_session):
    sig = _make_signal(db_session, stop=45.0, target_1=55.0)
    future = _bars([{"open": 50, "high": 56.0, "low": 49.5, "close": 55.5, "volume": 1000}])
    provider = _FixedFutureProvider(future)
    update_open_signals(db_session, provider, conservative_execution=True)
    db_session.refresh(sig)
    assert sig.state == SignalStateEnum.TARGET_1_HIT


def test_same_bar_stop_and_target_uses_conservative_stop(db_session):
    sig = _make_signal(db_session, stop=45.0, target_1=55.0)
    # Ayni barda hem stop hem hedef seviyesine deginiliyor (yuksek volatilite gunu)
    future = _bars([{"open": 50, "high": 56.0, "low": 44.0, "close": 50.0, "volume": 1000}])
    provider = _FixedFutureProvider(future)
    update_open_signals(db_session, provider, conservative_execution=True)
    db_session.refresh(sig)
    assert sig.state == SignalStateEnum.STOP_HIT  # muhafazakar: stop kazanir


def test_same_bar_conflict_non_conservative_picks_target(db_session):
    sig = _make_signal(db_session, stop=45.0, target_1=55.0)
    future = _bars([{"open": 50, "high": 56.0, "low": 44.0, "close": 50.0, "volume": 1000}])
    provider = _FixedFutureProvider(future)
    update_open_signals(db_session, provider, conservative_execution=False)
    db_session.refresh(sig)
    assert sig.state == SignalStateEnum.TARGET_1_HIT


def test_no_hit_leaves_signal_open(db_session):
    sig = _make_signal(db_session, stop=45.0, target_1=55.0)
    future = _bars([{"open": 50, "high": 52.0, "low": 49.0, "close": 51.0, "volume": 1000}])
    provider = _FixedFutureProvider(future)
    update_open_signals(db_session, provider)
    db_session.refresh(sig)
    assert sig.state == SignalStateEnum.WAITING_TRIGGER


def test_already_hit_target_1_does_not_re_trigger(db_session):
    sig = _make_signal(db_session, stop=45.0, target_1=55.0, target_2=60.0)
    sig.state = SignalStateEnum.TARGET_1_HIT
    db_session.commit()

    # Ayni tarihsel bar tekrar taranirsa (target_1 zaten gecilmis oldugu icin)
    # yeniden TARGET_1_HIT olarak tespit edilmemeli; sadece target_2/3 veya stop aranir.
    future = _bars([{"open": 55, "high": 56.0, "low": 54.5, "close": 55.5, "volume": 1000}])
    provider = _FixedFutureProvider(future)
    update_open_signals(db_session, provider)
    db_session.refresh(sig)
    assert sig.state == SignalStateEnum.TARGET_1_HIT  # degismedi (henuz target_2/stop yok)
