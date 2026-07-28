from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from app.analysis.smart_money_engine import detect_smart_money
from app.modules.chart_engine import (
    ChecklistVisual,
    NewsTimelineItem,
    ReportChartSpec,
    render_equity_curve,
    render_report_chart,
)


def sample_bars() -> pd.DataFrame:
    count = 150
    dates = pd.date_range("2026-01-02", periods=count, freq="B", tz="UTC")
    close = np.linspace(255, 329, count) + np.sin(np.arange(count) / 5.0) * 6
    opened = close - np.sin(np.arange(count) / 3.0) * 2.2
    return pd.DataFrame(
        {
            "timestamp": dates,
            "open": opened,
            "high": np.maximum(opened, close) + 3.1,
            "low": np.minimum(opened, close) - 2.8,
            "close": close,
            "volume": 10_000_000 + (np.arange(count) % 13) * 420_000,
            "is_complete": True,
        }
    )


def main() -> None:
    output = Path("docs/examples")
    output.mkdir(parents=True, exist_ok=True)
    frame = sample_bars()
    smart = detect_smart_money(frame)
    checklist = tuple(
        ChecklistVisual(label, passed)
        for label, passed in (
            ("Daily bias", True),
            ("HTF/LTF zone", True),
            ("Sweep + BOS/MSS", True),
            ("A+ plan", True),
            ("Haber riski", False),
            ("Minimum 1:2 RR", True),
        )
    )
    morning = render_report_chart(
        frame,
        ReportChartSpec(
            instrument="THYAO",
            timeframe="1D",
            report_kind="morning",
            direction="bullish",
            sentiment_score=74,
            checklist=checklist,
            entry_low=315.20,
            entry_high=318.10,
            stop=307.40,
            targets=(329.50, 337.80, 346.20),
            rr=2.4,
            liquidity_levels=((310.50, "satış likiditesi alındı"),),
            date_label="28.07.2026",
        ),
        smart_money=smart,
        output_dir=output,
        dpi=140,
    )
    Path(morning).replace(output / "smxm_morning_report_example.png")

    evening = render_report_chart(
        frame,
        ReportChartSpec(
            instrument="THYAO",
            timeframe="1D",
            report_kind="evening",
            direction="bullish",
            sentiment_score=68,
            change_percent=0.83,
            news_timeline=(
                NewsTimelineItem("10:00", "TCMB beklenti anketi", "medium"),
                NewsTimelineItem("15:30", "ABD çekirdek veri", "high"),
                NewsTimelineItem("18:00", "Kapanış akışı", "low"),
            ),
            date_label="28.07.2026",
        ),
        smart_money=smart,
        output_dir=output,
        dpi=140,
    )
    Path(evening).replace(output / "smxm_evening_report_example.png")

    timestamps = [datetime(2026, 1, 2, tzinfo=timezone.utc) + timedelta(days=i) for i in range(90)]
    equity = 10_000 + np.linspace(0, 1_450, len(timestamps)) + np.sin(np.arange(len(timestamps)) / 5) * 220
    curve = render_equity_curve(
        timestamps,
        equity,
        title="THYAO • SMXM Sanal Portföy",
        output_dir=output,
        dpi=140,
    )
    Path(curve).replace(output / "smxm_equity_curve_example.png")
    print("Örnekler üretildi:")
    for path in sorted(output.glob("smxm_*_example.png")):
        print(path)


if __name__ == "__main__":
    main()
