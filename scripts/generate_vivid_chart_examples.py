from __future__ import annotations

from pathlib import Path
import shutil
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from app.analysis.bist_trade_plan import build_bist_trade_plan
from app.services.chart_service import (
    clear_chart_cache,
    delete_chart_file,
    generate_bist_trade_plan_chart,
    generate_intraday_chart,
    generate_long_term_chart,
    generate_multi_timeframe_chart,
    generate_professional_daily_chart,
)


def sample_bars(
    count: int,
    *,
    start: str,
    frequency: str,
    base: float = 286.0,
    drift: float = 0.20,
    seed: int = 42,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = np.arange(count, dtype=float)
    wave = np.sin(index / 7.2) * 5.8 + np.sin(index / 19.0) * 2.6
    impulse = np.where(index > count * 0.72, (index - count * 0.72) * 0.16, 0.0)
    close = base + index * drift + wave + impulse + rng.normal(0, 0.75, count)
    opened = close + rng.normal(0, 1.15, count)
    high = np.maximum(opened, close) + rng.uniform(1.0, 3.0, count)
    low = np.minimum(opened, close) - rng.uniform(1.0, 3.0, count)
    volume = rng.integers(8_000_000, 35_000_000, count)
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(start, periods=count, freq=frequency, tz="UTC"),
            "open": opened,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "is_complete": True,
        }
    )


def keep(path: str | Path, destination: Path) -> None:
    source = Path(path)
    shutil.copy2(source, destination)
    delete_chart_file(source)


def main() -> None:
    output = PROJECT_ROOT / "docs" / "examples" / "vivid"
    output.mkdir(parents=True, exist_ok=True)
    clear_chart_cache()

    daily = sample_bars(520, start="2024-07-01", frequency="B")
    current = float(daily.iloc[-1]["close"])
    atr_like = float((daily.tail(30)["high"] - daily.tail(30)["low"]).mean())
    entry_low = current - atr_like * 1.8
    entry_high = current - atr_like * 1.15
    stop = entry_low - atr_like * 1.05
    targets = [current + atr_like * multiple for multiple in (0.9, 1.8, 2.8, 4.0, 5.2)]
    common = {
        "entry_zone": (entry_low, entry_high),
        "entry_trigger": entry_high,
        "stop_price": stop,
        "targets": targets,
        "info_box": {"Teknik skor": 82, "Veri kalitesi": "GÜÇLÜ"},
    }
    keep(
        generate_professional_daily_chart(daily, "THYAO", chart_mode="standard", **common),
        output / "01_standard_teknik_analiz.png",
    )
    keep(
        generate_professional_daily_chart(daily, "THYAO", chart_mode="detailed", **common),
        output / "02_detayli_teknik_analiz.png",
    )

    frames = {
        "5 dk": sample_bars(120, start="2026-07-27 07:00", frequency="5min", base=320, drift=.025, seed=5),
        "15 dk": sample_bars(120, start="2026-07-24 07:00", frequency="15min", base=316, drift=.055, seed=15),
        "1 saat": sample_bars(120, start="2026-07-10 07:00", frequency="1h", base=302, drift=.16, seed=60),
        "4 saat": sample_bars(120, start="2026-05-01 07:00", frequency="4h", base=284, drift=.34, seed=240),
    }
    keep(generate_multi_timeframe_chart(frames, "THYAO"), output / "03_dortlu_zaman_analizi.png")

    plan = build_bist_trade_plan(daily, "THYAO")
    keep(generate_bist_trade_plan_chart(daily, plan), output / "04_long_short_islem_plani.png")

    intraday = sample_bars(320, start="2026-07-21 07:00", frequency="15min", base=318, drift=.035, seed=75)
    keep(
        generate_intraday_chart(
            intraday,
            "THYAO",
            daily_support=float(intraday.tail(100)["low"].quantile(.12)),
            daily_resistance=float(intraday.tail(100)["high"].quantile(.88)),
            previous_close=float(intraday.iloc[-80]["close"]),
            active_alarm_points=[(intraday.iloc[-18]["timestamp"], float(intraday.iloc[-18]["close"]))],
            info_box={"Teknik skor": 78},
        ),
        output / "05_gun_ici_15dk_analiz.png",
    )
    keep(
        generate_long_term_chart(
            daily,
            "THYAO",
            timeframe="weekly",
            current_price=current,
            user_target=current * 1.25,
            valuation_status="DENGELİ",
            speculation_risk="ORTA",
            info_box={"Teknik skor": 76},
        ),
        output / "06_uzun_vadeli_analiz.png",
    )

    print("Canlı grafik örnekleri üretildi:")
    for path in sorted(output.glob("*.png")):
        print(path)


if __name__ == "__main__":
    main()
