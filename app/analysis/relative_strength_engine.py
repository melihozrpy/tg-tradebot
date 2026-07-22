from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

MIN_ALIGNED_BARS = 65  # 60 gunluk karsilastirma icin en az bu kadar ortak islem gunu gerekir

CLASS_VERY_STRONG = "cok_guclu"
CLASS_STRONG = "guclu"
CLASS_NEUTRAL = "notr"
CLASS_WEAK = "zayif"
CLASS_VERY_WEAK = "cok_zayif"


@dataclass
class RelativeStrengthResult:
    available: bool
    note: str
    return_5d_stock: Optional[float] = None
    return_5d_index: Optional[float] = None
    return_20d_stock: Optional[float] = None
    return_20d_index: Optional[float] = None
    return_60d_stock: Optional[float] = None
    return_60d_index: Optional[float] = None
    diff_20d: Optional[float] = None
    diff_60d: Optional[float] = None
    relative_score: Optional[float] = None
    classification: Optional[str] = None
    strong_while_index_down: bool = False
    weak_while_index_up: bool = False


def _period_return(series: pd.Series, periods: int) -> Optional[float]:
    if len(series) <= periods:
        return None
    start_price = float(series.iloc[-periods - 1])
    end_price = float(series.iloc[-1])
    if start_price <= 0:
        return None
    return round(((end_price - start_price) / start_price) * 100, 2)


def _align_on_common_dates(stock_df: pd.DataFrame, index_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Hisse ve endeks verilerini ORTAK islem gunlerinde hizalar.

    Veri tarihleri uyumsuzsa (ortak gun sayisi yetersizse) cagiran taraf
    bunu MIN_ALIGNED_BARS kontroluyle tespit eder ve hesaplama yapmaz.
    """
    s = stock_df[["timestamp", "close"]].copy()
    i = index_df[["timestamp", "close"]].copy()
    s["date_key"] = pd.to_datetime(s["timestamp"], utc=True).dt.date
    i["date_key"] = pd.to_datetime(i["timestamp"], utc=True).dt.date

    # Yinelenen tarihler (ayni gun icin birden fazla satir) merge'i sisirip
    # yanlis "ortak islem gunu" sayimina yol acabilir; her tarih icin son
    # (en guncel) satiri tutarak dedup edilir.
    s = s.sort_values("date_key").drop_duplicates(subset="date_key", keep="last")
    i = i.sort_values("date_key").drop_duplicates(subset="date_key", keep="last")

    merged = pd.merge(s, i, on="date_key", suffixes=("_stock", "_index"), how="inner")
    merged = merged.sort_values("date_key").reset_index(drop=True)

    aligned_stock = merged[["date_key", "close_stock"]].rename(columns={"close_stock": "close"})
    aligned_index = merged[["date_key", "close_index"]].rename(columns={"close_index": "close"})
    return aligned_stock, aligned_index


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


def compute_relative_strength(stock_df: pd.DataFrame, index_df: pd.DataFrame) -> RelativeStrengthResult:
    """Hissenin XU100'e (veya baska bir endekse) gore goreceli gucunu hesaplar.

    Veri tarihleri uyumsuzsa veya ortak islem gunu sayisi yetersizse hesaplama
    YAPILMAZ; available=False ve aciklayici not doner (uydurma skor uretilmez).
    """
    if stock_df is None or index_df is None or stock_df.empty or index_df.empty:
        return RelativeStrengthResult(available=False, note="Hisse veya endeks verisi bulunamadi.")

    aligned_stock, aligned_index = _align_on_common_dates(stock_df, index_df)
    if len(aligned_stock) < MIN_ALIGNED_BARS:
        return RelativeStrengthResult(
            available=False,
            note=f"Ortak islem gunu sayisi yetersiz ({len(aligned_stock)} < {MIN_ALIGNED_BARS}); goreceli guc hesaplanamadi.",
        )

    stock_close = aligned_stock["close"]
    index_close = aligned_index["close"]

    r5_stock = _period_return(stock_close, 5)
    r5_index = _period_return(index_close, 5)
    r20_stock = _period_return(stock_close, 20)
    r20_index = _period_return(index_close, 20)
    r60_stock = _period_return(stock_close, 60)
    r60_index = _period_return(index_close, 60)

    if None in (r20_stock, r20_index, r60_stock, r60_index):
        return RelativeStrengthResult(
            available=False,
            note="Yeterli gecmis veri olmadigi icin 20/60 gunluk goreceli guc hesaplanamadi.",
        )

    diff_20d = round(r20_stock - r20_index, 2)
    diff_60d = round(r60_stock - r60_index, 2)
    diff_5d = round((r5_stock - r5_index), 2) if r5_stock is not None and r5_index is not None else 0.0

    # 0-100 skor: 20 ve 60 gunluk farklari agirlikli olarak normalize et.
    # +/-20 puanlik fark 0-100 arasinda dogrusal olceklenir (sinirlar clip edilir).
    raw_score = 50 + (diff_20d * 1.5) + (diff_60d * 0.8) + (diff_5d * 0.5)
    relative_score = round(float(np.clip(raw_score, 0, 100)), 1)
    classification = _classify(relative_score)

    strong_while_index_down = diff_20d > 0 and r20_index < 0
    weak_while_index_up = diff_20d < 0 and r20_index > 0

    return RelativeStrengthResult(
        available=True,
        note="",
        return_5d_stock=r5_stock,
        return_5d_index=r5_index,
        return_20d_stock=r20_stock,
        return_20d_index=r20_index,
        return_60d_stock=r60_stock,
        return_60d_index=r60_index,
        diff_20d=diff_20d,
        diff_60d=diff_60d,
        relative_score=relative_score,
        classification=classification,
        strong_while_index_down=strong_while_index_down,
        weak_while_index_up=weak_while_index_up,
    )
