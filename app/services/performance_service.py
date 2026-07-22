from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.database import Signal, SignalStateEnum

_RESOLVED_STATES = (
    SignalStateEnum.TARGET_1_HIT,
    SignalStateEnum.TARGET_2_HIT,
    SignalStateEnum.TARGET_3_HIT,
    SignalStateEnum.STOP_HIT,
)


@dataclass
class PerformanceReport:
    is_reliable: bool
    note: str
    period_days: int
    sample_size: int
    total_signals: int = 0
    active_signals: int = 0
    target_1_hit_rate: Optional[float] = None
    target_2_hit_rate: Optional[float] = None
    target_3_hit_rate: Optional[float] = None
    stop_hit_rate: Optional[float] = None
    average_return_percent: Optional[float] = None
    average_loss_percent: Optional[float] = None
    average_r_multiple: Optional[float] = None
    profit_factor: Optional[float] = None
    expected_value: Optional[float] = None
    average_duration_days: Optional[float] = None
    best_signal_type: Optional[str] = None
    worst_signal_type: Optional[str] = None
    by_regime: dict = field(default_factory=dict)


def compute_performance_report(db: Session, period_days: int = 90, minimum_sample_size: int = 20) -> PerformanceReport:
    """Sinyal basari metriklerini hesaplar. Yeterli ORNEK yoksa (spesifikasyon
    bolum 7) yanıltici yuzde uretmez; is_reliable=False ve aciklayici not doner.
    """
    since = datetime.now(timezone.utc) - timedelta(days=period_days)
    signals = db.query(Signal).filter(Signal.created_at >= since).all()

    total = len(signals)
    resolved = [s for s in signals if s.state in _RESOLVED_STATES]
    active = [s for s in signals if s.state not in _RESOLVED_STATES and s.state != SignalStateEnum.CANCELLED]

    if len(resolved) < minimum_sample_size:
        return PerformanceReport(
            is_reliable=False,
            note="Güvenilir performans değerlendirmesi için yeterli sinyal bulunmuyor.",
            period_days=period_days,
            sample_size=len(resolved),
            total_signals=total,
            active_signals=len(active),
        )

    target1_hits = sum(1 for s in resolved if s.state in (SignalStateEnum.TARGET_1_HIT, SignalStateEnum.TARGET_2_HIT, SignalStateEnum.TARGET_3_HIT))
    target2_hits = sum(1 for s in resolved if s.state in (SignalStateEnum.TARGET_2_HIT, SignalStateEnum.TARGET_3_HIT))
    target3_hits = sum(1 for s in resolved if s.state == SignalStateEnum.TARGET_3_HIT)
    stop_hits = sum(1 for s in resolved if s.state == SignalStateEnum.STOP_HIT)

    r_multiples: list[float] = []
    returns_pct: list[float] = []
    losses_pct: list[float] = []
    durations: list[float] = []
    regime_bucket: dict[str, list[float]] = {}
    type_bucket: dict[str, list[float]] = {}

    for s in resolved:
        if s.stop_price is None or s.entry_zone_high is None:
            continue
        entry_ref = s.entry_zone_high
        risk_per_share = entry_ref - s.stop_price
        if risk_per_share <= 0:
            continue

        if s.state == SignalStateEnum.STOP_HIT:
            exit_price = s.stop_price
        elif s.state == SignalStateEnum.TARGET_1_HIT:
            exit_price = s.target_1
        elif s.state == SignalStateEnum.TARGET_2_HIT:
            exit_price = s.target_2
        else:
            exit_price = s.target_3

        if exit_price is None:
            continue

        pnl_per_share = exit_price - entry_ref
        r_multiple = pnl_per_share / risk_per_share
        pct = (pnl_per_share / entry_ref) * 100

        r_multiples.append(r_multiple)
        if pct >= 0:
            returns_pct.append(pct)
        else:
            losses_pct.append(pct)

        duration_days = None
        last_event = (
            sorted(s.events, key=lambda e: e.created_at)[-1] if s.events else None
        )
        if last_event is not None:
            duration_days = (last_event.created_at - s.created_at).total_seconds() / 86400
            durations.append(duration_days)

        regime_bucket.setdefault(s.market_regime or "bilinmiyor", []).append(r_multiple)
        type_bucket.setdefault(s.signal_type.value, []).append(r_multiple)

    total_r = sum(r_multiples) if r_multiples else 0.0
    wins = [r for r in r_multiples if r > 0]
    losses = [r for r in r_multiples if r <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    win_rate = (len(wins) / len(r_multiples)) if r_multiples else 0.0
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    expected_value = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)

    by_regime = {
        regime: round(sum(vals) / len(vals), 2) for regime, vals in regime_bucket.items() if vals
    }
    type_avgs = {t: round(sum(vals) / len(vals), 2) for t, vals in type_bucket.items() if vals}
    best_type = max(type_avgs, key=type_avgs.get) if type_avgs else None
    worst_type = min(type_avgs, key=type_avgs.get) if type_avgs else None

    return PerformanceReport(
        is_reliable=True,
        note="",
        period_days=period_days,
        sample_size=len(resolved),
        total_signals=total,
        active_signals=len(active),
        target_1_hit_rate=round(target1_hits / len(resolved) * 100, 1),
        target_2_hit_rate=round(target2_hits / len(resolved) * 100, 1),
        target_3_hit_rate=round(target3_hits / len(resolved) * 100, 1),
        stop_hit_rate=round(stop_hits / len(resolved) * 100, 1),
        average_return_percent=round(sum(returns_pct) / len(returns_pct), 2) if returns_pct else None,
        average_loss_percent=round(sum(losses_pct) / len(losses_pct), 2) if losses_pct else None,
        average_r_multiple=round(total_r / len(r_multiples), 2) if r_multiples else None,
        profit_factor=round(profit_factor, 2) if profit_factor != float("inf") else profit_factor,
        expected_value=round(expected_value, 2),
        average_duration_days=round(sum(durations) / len(durations), 2) if durations else None,
        best_signal_type=best_type,
        worst_signal_type=worst_type,
        by_regime=by_regime,
    )
