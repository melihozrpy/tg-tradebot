from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.analysis.baby_stock_engine import (
    BabyStockReport,
    BabyStockRiskProfile,
    calculate_baby_position_plan,
    format_baby_stock_report,
)
from app.telegram.baby_stock_handlers import parse_baby_stock_capital


def _profile() -> BabyStockRiskProfile:
    return BabyStockRiskProfile(
        capital=200_000,
        risk_per_trade_percent=0.5,
        daily_loss_limit_percent=1.5,
        max_open_positions=2,
        max_position_percent=20,
        no_overnight=True,
    )


def test_position_plan_respects_risk_and_notional_caps() -> None:
    plan = calculate_baby_position_plan(_profile(), entry_low=100, entry_high=102, stop=99)

    # Risk budget is 1,000 TL.  At 101 entry and 2 TL stop distance it permits
    # 500 units, but the 20% notional cap allows only floor(40,000 / 101) = 396.
    assert plan.maximum_units == 396
    assert plan.maximum_position_value <= 40_000
    assert plan.planned_risk <= plan.risk_budget
    assert plan.daily_loss_limit == 3_000


@pytest.mark.parametrize(
    ("args", "expected"),
    [([], 200_000), (["200000"], 200_000), (["200.000"], 200_000), (["200k"], 200_000)],
)
def test_capital_parser_accepts_common_turkish_inputs(args: list[str], expected: float) -> None:
    assert parse_baby_stock_capital(args, default=200_000) == expected


def test_empty_report_never_invents_two_candidates() -> None:
    report = BabyStockReport(
        scanned=571,
        failed=0,
        candidates=(),
        created_at=datetime(2026, 8, 14, 9, tzinfo=timezone.utc),
        risk_profile=_profile(),
        shortlist_size=4,
    )

    text = format_baby_stock_report(report)
    assert "BUGÜN ONAYLI ADAY YOK" in text
    assert "zorla iki hisse" in text.casefold()
