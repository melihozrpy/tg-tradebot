from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from app.analysis.pattern_engine import ChartPattern
from app.analysis.screener_engine import DailyTopPick, DailyTopPicksRunResult
from app.models.database import ScheduledTradeIdea
from app.services.kap_monitor_service import KapHeadline, format_kap_alert
from app.services.scheduled_idea_service import (
    evaluate_due_ideas,
    format_idea_performance_report,
    format_scheduled_ideas_report,
    persist_scheduled_ideas,
)


def _pick(symbol: str = "THYAO") -> DailyTopPick:
    return DailyTopPick(
        symbol=symbol,
        score=92,
        technical_confirmations=8,
        price=320.0,
        entry_low=300.0,
        entry_high=302.0,
        stop=297.0,
        tp1=310.0,
        tp2=315.0,
        rr=2.3,
        target_potential_percent=3.0,
        pattern=ChartPattern("Yükselen Üçgen", "continuation", "bullish", True, 90, 306.0, 315.0, "Kırılım kapanışla doğrulandı."),
        reasons=("EMA20/50/100", "MACD", "ADX"),
        confirmation_instruction="Bölgeye dönüşte 15dk yeşil kapanış beklenir.",
        fundamental_score=75,
        fundamental_status="GÜÇLÜ",
        fundamental_source="test",
    )


def test_scheduled_idea_card_is_retest_first_and_never_promises_return() -> None:
    report = DailyTopPicksRunResult(
        scanned=571,
        failed=0,
        picks=(_pick(),),
        fundamental_checked=1,
        fundamental_verified=1,
        created_at=datetime(2026, 8, 20, 6, 30, tzinfo=timezone.utc),
    )

    text = format_scheduled_ideas_report(report, slot="morning")

    assert "Giriş bölgesi: 300.00–302.00" in text
    assert "Son fiyattan giriş yok" in text
    assert "getiri ya da yön garantisi değildir" in text


def test_scheduled_idea_card_is_empty_when_no_plan_passes() -> None:
    report = DailyTopPicksRunResult(
        scanned=571,
        failed=0,
        picks=(),
        fundamental_checked=0,
        fundamental_verified=0,
        created_at=datetime(2026, 8, 20, 6, 30, tzinfo=timezone.utc),
    )

    assert format_scheduled_ideas_report(report, slot="morning") == ""


def test_performance_does_not_count_untouched_entry_zone(db_session) -> None:
    issued = datetime.now(timezone.utc) - timedelta(days=3)
    row = ScheduledTradeIdea(
        run_key="test:morning:20260817",
        slot="morning",
        symbol="THYAO",
        score=90,
        observed_price=320.0,
        entry_low=300.0,
        entry_high=302.0,
        stop_price=297.0,
        tp1_price=310.0,
        tp2_price=315.0,
        planned_rr=2.3,
        issued_at=issued,
    )
    db_session.add(row)
    db_session.commit()

    class Provider:
        def get_ohlcv(self, *_args):
            index = pd.DatetimeIndex([issued + timedelta(days=1)], tz="UTC")
            return pd.DataFrame({"open": [320.0], "high": [325.0], "low": [315.0], "close": [322.0]}, index=index)

    items = evaluate_due_ideas(db_session, provider=Provider(), minimum_age_days=2)

    assert items[0].status == "TETIKLENMEDI"
    assert items[0].return_percent is None
    assert "başarı oranı uydurulmadı" in format_idea_performance_report(items).casefold()


def test_persisted_ideas_are_idempotent_per_slot_day(db_session) -> None:
    report = DailyTopPicksRunResult(
        scanned=10,
        failed=0,
        picks=(_pick(),),
        fundamental_checked=1,
        fundamental_verified=1,
        created_at=datetime(2026, 8, 20, 6, 30, tzinfo=timezone.utc),
    )
    persist_scheduled_ideas(db_session, report=report, slot="morning")
    persist_scheduled_ideas(db_session, report=report, slot="morning")
    assert db_session.query(ScheduledTradeIdea).count() == 1


def test_kap_monitor_card_keeps_source_and_requires_official_check() -> None:
    text = format_kap_alert(
        KapHeadline(
            title="Örnek şirket yeni sözleşme imzaladığını duyurdu.",
            source="Midas KAP başlık akışı",
            source_url="https://www.getmidas.com/kap-haberleri/",
            relative_time="5 Dakika Önce",
            impact_score=30,
            category="Yeni iş / ihale",
        )
    )
    assert "Midas KAP başlık akışı" in text
    assert "resmî KAP açıklamasını doğrula" in text
