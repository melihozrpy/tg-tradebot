from __future__ import annotations

"""MERGEN QUANT - Asama 5c: Gelismis XU100 ve sektor goreceli guc (donemsel).

Mevcut `relative_strength_engine` (5/20/60 gunluk, tek benchmark) DEGISTIRILMEZ
ve BOZULMAZ; bu modul EK olarak 5/20/60/120 islem gunu (~1 hafta/1 ay/3 ay/6 ay)
periyotlarini, XU100 ve sektor endeksi icin AYRI AYRI hesaplayan, veritabanina
kaydedilebilir (relative_strength_periods) bir sonuc uretir.

Kurallar (mevcut motorla ayni disiplin):
- Tarihler ORTAK islem gunlerinde normalize edilir (inner join).
- Yetersiz veride sahte/uydurma skor UretilMEZ; ilgili periyot icin
  available=False ve aciklayici not doner.
- Hesaplama tamamen deterministik Python/pandas kodu ile yapilir; LLM/Groq
  bu hesaplamaya KESINLIKLE karismaz.
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

PERIOD_1HAFTA = "1hafta"
PERIOD_1AY = "1ay"
PERIOD_3AY = "3ay"
PERIOD_6AY = "6ay"

# Periyot adi -> (gerekli minimum ortak islem gunu, geri bakis bar sayisi)
PERIOD_DEFINITIONS: dict[str, int] = {
    PERIOD_1HAFTA: 5,
    PERIOD_1AY: 20,
    PERIOD_3AY: 60,
    PERIOD_6AY: 120,
}

BENCHMARK_XU100 = "xu100"
BENCHMARK_SECTOR = "sektor"

CLASS_VERY_STRONG = "cok_guclu"
CLASS_STRONG = "guclu"
CLASS_NEUTRAL = "notr"
CLASS_WEAK = "zayif"
CLASS_VERY_WEAK = "cok_zayif"
CLASS_INSUFFICIENT = "veri_yetersiz"

_CLASS_LABELS = {
    CLASS_VERY_STRONG: "Çok güçlü",
    CLASS_STRONG: "Güçlü",
    CLASS_NEUTRAL: "Nötr",
    CLASS_WEAK: "Zayıf",
    CLASS_VERY_WEAK: "Çok zayıf",
    CLASS_INSUFFICIENT: "Veri yetersiz",
}


def classification_label(classification: str) -> str:
    return _CLASS_LABELS.get(classification, classification)


@dataclass
class PeriodStrength:
    period: str  # 1hafta|1ay|3ay|6ay
    benchmark: str  # xu100|sektor
    available: bool
    note: str
    stock_return_pct: Optional[float] = None
    benchmark_return_pct: Optional[float] = None
    diff_pct: Optional[float] = None
    strength_score: Optional[float] = None
    classification: str = CLASS_INSUFFICIENT


@dataclass
class RelativeStrengthPeriodsResult:
    symbol: str
    benchmark: str  # xu100|sektor
    benchmark_symbol: str
    periods: dict[str, PeriodStrength]
    overall_trend: str  # guclenen | zayiflayan | yatay | veri_yetersiz


def _align_on_common_dates(stock_df: pd.DataFrame, bench_df: pd.DataFrame) -> pd.DataFrame:
    s = stock_df[["timestamp", "close"]].copy()
    b = bench_df[["timestamp", "close"]].copy()
    s["date_key"] = pd.to_datetime(s["timestamp"], utc=True).dt.date
    b["date_key"] = pd.to_datetime(b["timestamp"], utc=True).dt.date
    s = s.sort_values("date_key").drop_duplicates(subset="date_key", keep="last")
    b = b.sort_values("date_key").drop_duplicates(subset="date_key", keep="last")
    merged = pd.merge(s, b, on="date_key", suffixes=("_stock", "_bench"), how="inner")
    return merged.sort_values("date_key").reset_index(drop=True)


def _period_return(series: pd.Series, periods: int) -> Optional[float]:
    if len(series) <= periods:
        return None
    start_price = float(series.iloc[-periods - 1])
    end_price = float(series.iloc[-1])
    if start_price <= 0:
        return None
    return round(((end_price - start_price) / start_price) * 100, 2)


def _classify(score: float) -> str:
    if score >= 80:
        return CLASS_VERY_STRONG
    if score >= 60:
        return CLASS_STRONG
    if score >= 40:
        return CLASS_NEUTRAL
    if score >= 20:
        return CLASS_WEAK
    return CLASS_VERY_WEAK


def compute_relative_strength_periods(
    stock_df: pd.DataFrame,
    benchmark_df: Optional[pd.DataFrame],
    symbol: str,
    benchmark: str,
    benchmark_symbol: str,
) -> RelativeStrengthPeriodsResult:
    """Bir hisse icin TEK bir benchmark'a (XU100 ya da sektor) gore
    5/20/60/120 islem gunu periyotlarinda goreceli guc hesaplar.
    """
    periods: dict[str, PeriodStrength] = {}

    if benchmark_df is None or benchmark_df.empty or stock_df is None or stock_df.empty:
        note = "Endeks/hisse verisi bulunamadi."
        for period_name in PERIOD_DEFINITIONS:
            periods[period_name] = PeriodStrength(
                period=period_name, benchmark=benchmark, available=False, note=note
            )
        return RelativeStrengthPeriodsResult(
            symbol=symbol, benchmark=benchmark, benchmark_symbol=benchmark_symbol,
            periods=periods, overall_trend=CLASS_INSUFFICIENT,
        )

    merged = _align_on_common_dates(stock_df, benchmark_df)
    stock_close = merged["close_stock"]
    bench_close = merged["close_bench"]

    scores: list[float] = []
    for period_name, bar_count in PERIOD_DEFINITIONS.items():
        if len(merged) <= bar_count:
            periods[period_name] = PeriodStrength(
                period=period_name,
                benchmark=benchmark,
                available=False,
                note=f"Ortak islem gunu sayisi yetersiz ({len(merged)} <= {bar_count}); bu donem icin veri yetersiz.",
            )
            continue

        stock_ret = _period_return(stock_close, bar_count)
        bench_ret = _period_return(bench_close, bar_count)
        if stock_ret is None or bench_ret is None:
            periods[period_name] = PeriodStrength(
                period=period_name, benchmark=benchmark, available=False,
                note="Fiyat verisi bu donem icin hesaplanamadi.",
            )
            continue

        diff = round(stock_ret - bench_ret, 2)
        # 0-100 skor: +/-15 puanlik fark dogrusal olarak 0-100'e olceklenir.
        raw_score = 50 + (diff * (50.0 / 15.0))
        score = round(float(np.clip(raw_score, 0, 100)), 1)
        classification = _classify(score)
        scores.append(score)

        periods[period_name] = PeriodStrength(
            period=period_name,
            benchmark=benchmark,
            available=True,
            note="",
            stock_return_pct=stock_ret,
            benchmark_return_pct=bench_ret,
            diff_pct=diff,
            strength_score=score,
            classification=classification,
        )

    overall_trend = CLASS_INSUFFICIENT
    short = periods.get(PERIOD_1HAFTA)
    long = periods.get(PERIOD_3AY) or periods.get(PERIOD_1AY)
    if short and long and short.available and long.available:
        delta = (short.strength_score or 50) - (long.strength_score or 50)
        if delta > 8:
            overall_trend = "guclenen"
        elif delta < -8:
            overall_trend = "zayiflayan"
        else:
            overall_trend = "yatay"

    return RelativeStrengthPeriodsResult(
        symbol=symbol, benchmark=benchmark, benchmark_symbol=benchmark_symbol,
        periods=periods, overall_trend=overall_trend,
    )
