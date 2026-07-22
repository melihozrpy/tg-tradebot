from __future__ import annotations

from datetime import datetime, timezone
from itertools import count

from app.models.database import Signal, SignalStateEnum, SignalTypeEnum
from app.services.performance_service import compute_performance_report


_signal_counter = count(1)


def _add_resolved_signal(db, state, stop=45.0, entry_high=51.0, target_1=55.0):
    now = datetime.now(timezone.utc)
    sig = Signal(
        symbol="TESTX", timeframe="1d", signal_type=SignalTypeEnum.BUY_CANDIDATE, state=state,
        score=75.0, confidence="orta", entry_zone_low=49.0, entry_zone_high=entry_high, entry_trigger=51.0,
        stop_price=stop, target_1=target_1, target_2=target_1 + 5, target_3=target_1 + 10, risk_reward=2.0,
        market_regime="zayif_yukselis", strategy_version="1.0.0", data_timestamp=now, provider="test",
        idempotency_key=f"perf-test-{state.value}-{now.timestamp()}-{next(_signal_counter)}",
    )
    db.add(sig)
    db.commit()
    return sig


def test_insufficient_sample_does_not_report_misleading_percentage(db_session):
    for _ in range(5):
        _add_resolved_signal(db_session, SignalStateEnum.TARGET_1_HIT)
    report = compute_performance_report(db_session, period_days=90, minimum_sample_size=20)
    assert report.is_reliable is False
    assert "yeterli sinyal bulunmuyor" in report.note.lower()
    assert report.target_1_hit_rate is None


def test_sufficient_sample_computes_hit_rates(db_session):
    for _ in range(15):
        _add_resolved_signal(db_session, SignalStateEnum.TARGET_1_HIT)
    for _ in range(5):
        _add_resolved_signal(db_session, SignalStateEnum.STOP_HIT)
    report = compute_performance_report(db_session, period_days=90, minimum_sample_size=20)
    assert report.is_reliable is True
    assert report.sample_size == 20
    assert report.target_1_hit_rate == 75.0
    assert report.stop_hit_rate == 25.0
    assert report.profit_factor is not None
