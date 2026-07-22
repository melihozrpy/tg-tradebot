from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

MODE_CONFIRMED_CLOSE = "confirmed_close"
MODE_INTRADAY_PREVIEW = "intraday_preview"


@dataclass
class AnalysisModeResult:
    mode: str  # confirmed_close | intraday_preview
    analysis_df: pd.DataFrame  # sinyal hesaplamasinda kullanilacak, sadece TAMAMLANMIS mumlar
    last_confirmed_date: Optional[date]
    intraday_quote: Optional[dict]  # mode == intraday_preview ise bugunun (henuz kesinlesmemis) barinin bilgisi
    is_weekend: bool
    note: str


def _parse_close_time(close_scan_time: str) -> time:
    hour_str, minute_str = close_scan_time.split(":")
    return time(hour=int(hour_str), minute=int(minute_str))


def determine_analysis_mode(
    df: pd.DataFrame,
    close_scan_time: str = "18:20",
    tz_name: str = "Europe/Istanbul",
    now_utc: Optional[datetime] = None,
) -> AnalysisModeResult:
    """Piyasanin acik/kapali oldugunu VE son gunluk barin kesinlesmis olup
    olmadigini birlikte degerlendirir. Sabit bir saate 'kor kor' bagli
    degildir; su bilgileri birlikte kontrol eder:

    - Europe/Istanbul yerel zamani (varsayilan)
    - Hafta sonu
    - Son mumun tarihi (bugune mi ait, gecmise mi ait)
    - Kapanis tarama saatinin gecilip gecilmedigi

    Piyasa acikken tamamlanmamis gunluk mum KESIN sinyal icin kullanilmaz;
    analysis_df bu durumda son (bugunku, tamamlanmamis) satiri icermez.
    """
    tz = ZoneInfo(tz_name)
    now = (now_utc or datetime.now(timezone.utc)).astimezone(tz)
    today = now.date()
    is_weekend = now.weekday() >= 5
    close_threshold = _parse_close_time(close_scan_time)

    if df.empty:
        return AnalysisModeResult(
            mode=MODE_CONFIRMED_CLOSE,
            analysis_df=df,
            last_confirmed_date=None,
            intraday_quote=None,
            is_weekend=is_weekend,
            note="Veri seti bos.",
        )

    df = df.sort_values("timestamp").reset_index(drop=True)
    last_row = df.iloc[-1]
    last_bar_ts = last_row["timestamp"]
    if last_bar_ts.tzinfo is None:
        last_bar_ts = last_bar_ts.tz_localize("UTC")
    last_bar_date_local = last_bar_ts.astimezone(tz).date()

    bar_is_from_today = last_bar_date_local == today
    past_close_scan_time = now.time() >= close_threshold

    if not bar_is_from_today:
        # Son bar bugune ait degil (gecmis bir islem gunune ait) -> zaten
        # kesinlesmis kapanis verisidir, hafta sonu/tatil oldugu icin normal.
        return AnalysisModeResult(
            mode=MODE_CONFIRMED_CLOSE,
            analysis_df=df,
            last_confirmed_date=last_bar_date_local,
            intraday_quote=None,
            is_weekend=is_weekend,
            note="",
        )

    # Son bar bugune ait: piyasa hafta sonuysa (teorik olarak olmamali ama
    # veri kaynagi hatali donerse) veya kapanis tarama saati gecildiyse
    # kesinlesmis sayilir; aksi halde gun ici on analiz modundadir.
    if is_weekend or past_close_scan_time:
        return AnalysisModeResult(
            mode=MODE_CONFIRMED_CLOSE,
            analysis_df=df,
            last_confirmed_date=last_bar_date_local,
            intraday_quote=None,
            is_weekend=is_weekend,
            note="",
        )

    # Gun ici on analiz modu: bugunku (tamamlanmamis) barı analiz disi birak,
    # yalnizca bilgi amacli goster.
    intraday_quote = {
        "price": round(float(last_row["close"]), 2),
        "open": round(float(last_row["open"]), 2),
        "high": round(float(last_row["high"]), 2),
        "low": round(float(last_row["low"]), 2),
        "timestamp": last_bar_ts.to_pydatetime() if hasattr(last_bar_ts, "to_pydatetime") else last_bar_ts,
    }
    confirmed_df = df.iloc[:-1].reset_index(drop=True)
    last_confirmed_date = None
    if not confirmed_df.empty:
        prev_ts = confirmed_df.iloc[-1]["timestamp"]
        if prev_ts.tzinfo is None:
            prev_ts = prev_ts.tz_localize("UTC")
        last_confirmed_date = prev_ts.astimezone(tz).date()

    return AnalysisModeResult(
        mode=MODE_INTRADAY_PREVIEW,
        analysis_df=confirmed_df,
        last_confirmed_date=last_confirmed_date,
        intraday_quote=intraday_quote,
        is_weekend=is_weekend,
        note="Gun ici fiyat gecikmeli olabilir; destek/direnc/stop/hedefler kapanisa kadar degisebilir.",
    )
