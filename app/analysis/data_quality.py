from __future__ import annotations

"""Piyasa verisi kalite denetimi.

Eski ``validate_ohlcv`` fonksiyonu geriye uyumlu tutulur. Yeni
``DataQualityEngine`` aynı kontrolleri sayısallaştırır ve provider/fallback
katmanının fail-closed karar verebilmesi için ayrıntılı bir sonuç üretir.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from app.data.yfinance_provider import normalize_bist_symbol

EXTREME_JUMP_THRESHOLD = 0.50
ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")


class DataQualityStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    INCOMPLETE = "INCOMPLETE"
    INVALID = "INVALID"
    PROVIDER_DOWN = "PROVIDER_DOWN"


@dataclass
class DataQualityResult:
    # İlk üç alan eski çağrıların kullandığı sözleşmedir.
    is_valid: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    status: DataQualityStatus = DataQualityStatus.HEALTHY
    score: int = 100
    data_age_minutes: Optional[float] = None
    last_bar_time: Optional[datetime] = None
    missing_bar_count: int = 0
    duplicate_bar_count: int = 0
    outlier_count: int = 0
    incomplete_bar_count: int = 0
    provider: str = "unknown"
    fallback_used: bool = False
    cache_used: bool = False
    cache_age_minutes: Optional[float] = None
    usable_for_analysis: bool = True
    normalized_symbol: Optional[str] = None
    price_mode: str = "unadjusted"
    cleaned_df: Optional[pd.DataFrame] = field(default=None, repr=False, compare=False)

    @classmethod
    def provider_down(cls, provider: str, detail: str) -> "DataQualityResult":
        return cls(
            is_valid=False,
            issues=[detail],
            status=DataQualityStatus.PROVIDER_DOWN,
            score=0,
            provider=provider,
            usable_for_analysis=False,
        )

    def as_dict(self) -> dict:
        return {
            "status": self.status.value,
            "score": self.score,
            "data_age_minutes": self.data_age_minutes,
            "last_bar_time": self.last_bar_time.isoformat() if self.last_bar_time else None,
            "missing_bar_count": self.missing_bar_count,
            "duplicate_bar_count": self.duplicate_bar_count,
            "outlier_count": self.outlier_count,
            "incomplete_bar_count": self.incomplete_bar_count,
            "provider": self.provider,
            "fallback_used": self.fallback_used,
            "cache_used": self.cache_used,
            "cache_age_minutes": self.cache_age_minutes,
            "warnings": list(self.warnings),
            "issues": list(self.issues),
            "usable_for_analysis": self.usable_for_analysis,
            "normalized_symbol": self.normalized_symbol,
            "price_mode": self.price_mode,
        }


def _as_utc_timestamp(value) -> Optional[pd.Timestamp]:
    try:
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return ts
    except Exception:  # noqa: BLE001 - kalite denetimi veri akışını çökertmemeli
        return None


def _timeframe_delta(timeframe: str) -> timedelta:
    return {
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
        "1d": timedelta(days=1),
        "1wk": timedelta(days=7),
        "1w": timedelta(days=7),
        "1mo": timedelta(days=31),
    }.get(timeframe, timedelta(days=1))


def _is_last_bar_incomplete(last_ts: pd.Timestamp, timeframe: str, now: datetime) -> bool:
    now_utc = pd.Timestamp(now).tz_convert("UTC") if pd.Timestamp(now).tzinfo else pd.Timestamp(now, tz="UTC")
    if timeframe == "1d":
        now_ist = now_utc.tz_convert(ISTANBUL_TZ)
        last_ist = last_ts.tz_convert(ISTANBUL_TZ)
        # BIST günlük mumu aynı gün 18:15'ten önce kesinleşmiş sayılmaz.
        return last_ist.date() == now_ist.date() and now_ist.time() < time(18, 15)
    if timeframe in {"1wk", "1w"}:
        now_ist = now_utc.tz_convert(ISTANBUL_TZ)
        last_ist = last_ts.tz_convert(ISTANBUL_TZ)
        return last_ist.isocalendar()[:2] == now_ist.isocalendar()[:2] and now_ist.weekday() < 4
    if timeframe == "1mo":
        now_ist = now_utc.tz_convert(ISTANBUL_TZ)
        last_ist = last_ts.tz_convert(ISTANBUL_TZ)
        return (last_ist.year, last_ist.month) == (now_ist.year, now_ist.month)
    return last_ts + _timeframe_delta(timeframe) > now_utc


def _corporate_action_dates(actions: Optional[Iterable[dict]]) -> set[date]:
    dates: set[date] = set()
    for action in actions or []:
        raw = action.get("date") or action.get("timestamp")
        ts = _as_utc_timestamp(raw)
        if ts is not None and str(action.get("type", "")).lower() in {"split", "dividend", "temettu", "bolunme"}:
            dates.add(ts.date())
    return dates


def _expected_missing_daily_bars(timestamps: pd.Series) -> int:
    if len(timestamps) < 2:
        return 0
    dates = pd.DatetimeIndex(timestamps).tz_convert("UTC").normalize().unique()
    expected = pd.bdate_range(dates.min(), dates.max(), tz="UTC")
    # Resmî tatiller haricî takvim olmadan kesin bilinemediği için bu sayı
    # uyarı amaçlıdır; tek başına veriyi INVALID yapmaz.
    return max(0, len(expected.difference(dates)))


class DataQualityEngine:
    """OHLCV veri setini normalize eder, puanlar ve fail-closed durum üretir."""

    def __init__(
        self,
        timezone_name: str = "Europe/Istanbul",
        extreme_jump_threshold: float = EXTREME_JUMP_THRESHOLD,
    ) -> None:
        self.timezone_name = timezone_name
        self.extreme_jump_threshold = extreme_jump_threshold

    def evaluate(
        self,
        df: pd.DataFrame,
        *,
        symbol: Optional[str] = None,
        timeframe: str = "1d",
        min_bars: int = 1,
        max_staleness_minutes: Optional[float] = None,
        now: Optional[datetime] = None,
        expect_timezone_aware: bool = True,
        check_incomplete: bool = False,
        corporate_actions: Optional[Iterable[dict]] = None,
        provider: str = "unknown",
        fallback_used: bool = False,
        cache_used: bool = False,
        cache_age_minutes: Optional[float] = None,
        price_mode: str = "unadjusted",
        daily_reference: Optional[pd.DataFrame] = None,
    ) -> DataQualityResult:
        now = now or datetime.now(timezone.utc)
        issues: list[str] = []
        warnings: list[str] = []
        normalized_symbol = normalize_bist_symbol(symbol) if symbol else None

        if df is None or df.empty:
            return DataQualityResult(
                is_valid=False,
                issues=["Veri seti boş."],
                status=DataQualityStatus.INCOMPLETE,
                score=0,
                provider=provider,
                fallback_used=fallback_used,
                cache_used=cache_used,
                cache_age_minutes=cache_age_minutes,
                usable_for_analysis=False,
                normalized_symbol=normalized_symbol,
                price_mode=price_mode,
            )

        required_cols = {"timestamp", "open", "high", "low", "close", "volume"}
        missing_cols = required_cols - set(df.columns)
        if missing_cols:
            return DataQualityResult(
                is_valid=False,
                issues=[f"Eksik kolonlar: {sorted(missing_cols)}"],
                status=DataQualityStatus.INVALID,
                score=0,
                provider=provider,
                fallback_used=fallback_used,
                cache_used=cache_used,
                cache_age_minutes=cache_age_minutes,
                usable_for_analysis=False,
                normalized_symbol=normalized_symbol,
                price_mode=price_mode,
            )

        working = df.copy()
        original_ts = working["timestamp"]
        sample_ts = original_ts.iloc[-1]
        if expect_timezone_aware and getattr(sample_ts, "tzinfo", None) is None:
            warnings.append("Zaman damgaları timezone bilgisi içermiyor; UTC olarak normalize edildi.")
        working["timestamp"] = pd.to_datetime(working["timestamp"], utc=True, errors="coerce")
        if working["timestamp"].isna().any():
            issues.append("Geçersiz timestamp değeri bulundu.")

        duplicate_count = int(working["timestamp"].duplicated().sum())
        if duplicate_count:
            issues.append(f"{duplicate_count} yinelenen timestamp bulundu.")
        if not working["timestamp"].is_monotonic_increasing:
            warnings.append("Tarihler sırasız geldi; normalize edilmiş kopya sıralandı.")
        working = working.sort_values("timestamp").reset_index(drop=True)

        if len(working) < min_bars:
            issues.append(f"Yetersiz mum sayısı: {len(working)} < {min_bars}")

        numeric_cols = ["open", "high", "low", "close", "volume"]
        for col in numeric_cols:
            working[col] = pd.to_numeric(working[col], errors="coerce")
        if working[numeric_cols].isnull().any().any():
            issues.append("Eksik veya sayısal olmayan OHLCV değeri bulundu.")

        price_cols = ["open", "high", "low", "close"]
        if (working[price_cols] <= 0).any().any():
            issues.append("Sıfır veya negatif fiyat değeri bulundu.")
        if (working["volume"] < 0).any():
            issues.append("Negatif hacim değeri bulundu.")
        if (working["high"] < working["low"]).any():
            issues.append("High < Low hatası bulundu.")
        if ((working["open"] > working["high"]) | (working["open"] < working["low"])).any():
            issues.append("Open değeri high-low aralığının dışında.")
        if ((working["close"] > working["high"]) | (working["close"] < working["low"])).any():
            issues.append("Close değeri high-low aralığının dışında.")

        missing_bar_count = 0
        weekend_count = 0
        if timeframe == "1d" and working["timestamp"].notna().all():
            weekend_count = int((working["timestamp"].dt.weekday >= 5).sum())
            if weekend_count:
                warnings.append(f"{weekend_count} hafta sonu timestamp'i bulundu.")
            missing_bar_count = _expected_missing_daily_bars(working["timestamp"])
            if missing_bar_count:
                warnings.append(
                    f"Takvim bazında {missing_bar_count} olası eksik iş günü bulundu; resmî tatiller ayrıca doğrulanmalıdır."
                )

        outlier_count = 0
        action_dates = _corporate_action_dates(corporate_actions)
        if len(working) >= 2:
            jumps = working["close"].pct_change().abs()
            outlier_indexes = list(jumps[jumps > self.extreme_jump_threshold].index)
            for idx in outlier_indexes:
                ts_date = working.loc[idx, "timestamp"].date()
                if any(abs((ts_date - action_date).days) <= 1 for action_date in action_dates):
                    continue
                outlier_count += 1
            corporate_count = len(outlier_indexes) - outlier_count
            if outlier_count:
                warnings.append(
                    f"{outlier_count} mumda %{self.extreme_jump_threshold * 100:.0f} üzeri açıklanamayan fiyat sıçraması bulundu."
                )
            if corporate_count:
                warnings.append(
                    f"{corporate_count} büyük hareket doğrulanmış split/temettü ile eşleşti; anomali sayılmadı."
                )

        last_ts = _as_utc_timestamp(working["timestamp"].iloc[-1])
        data_age_minutes: Optional[float] = None
        stale = False
        incomplete_count = 0
        if last_ts is not None:
            now_ts = pd.Timestamp(now)
            now_ts = now_ts.tz_localize("UTC") if now_ts.tzinfo is None else now_ts.tz_convert("UTC")
            data_age_minutes = max(0.0, (now_ts - last_ts).total_seconds() / 60)
            if max_staleness_minutes is not None and data_age_minutes > max_staleness_minutes:
                stale = True
                issues.append(
                    f"Veri eski: son mum {data_age_minutes:.0f} dakika önce; izin verilen en fazla {max_staleness_minutes:.0f} dakika."
                )
            if check_incomplete and _is_last_bar_incomplete(last_ts, timeframe, now):
                incomplete_count = 1
                issues.append("Son mum henüz tamamlanmamış.")

        if daily_reference is not None and timeframe not in {"1d", "1wk", "1w", "1mo"}:
            try:
                intraday_dates = set(working["timestamp"].dt.tz_convert(ISTANBUL_TZ).dt.date)
                daily_dates = set(pd.to_datetime(daily_reference["timestamp"], utc=True).dt.tz_convert(ISTANBUL_TZ).dt.date)
                if intraday_dates and not intraday_dates.intersection(daily_dates):
                    warnings.append("Günlük ve intraday veri ortak işlem günü içermiyor.")
            except Exception:  # noqa: BLE001
                warnings.append("Günlük/intraday tutarlılık kontrolü tamamlanamadı.")

        hard_invalid = any(
            token in issue
            for issue in issues
            for token in (
                "Eksik kolonlar",
                "Geçersiz timestamp",
                "yinelenen timestamp",
                "sayısal olmayan",
                "negatif fiyat",
                "Negatif hacim",
                "High < Low",
                "aralığının dışında",
            )
        )
        insufficient = any("Yetersiz mum" in issue for issue in issues)

        score = 100
        score -= min(35, duplicate_count * 10)
        score -= min(25, missing_bar_count * 2)
        score -= min(30, outlier_count * 10)
        score -= min(15, len(warnings) * 3)
        if hard_invalid:
            score = min(score, 25)
        if insufficient or incomplete_count:
            score = min(score, 45)
        if stale:
            score = min(score, 40)
        score = int(max(0, min(100, score)))

        if hard_invalid:
            status = DataQualityStatus.INVALID
        elif stale:
            status = DataQualityStatus.STALE
        elif insufficient or incomplete_count:
            status = DataQualityStatus.INCOMPLETE
        elif warnings:
            status = DataQualityStatus.DEGRADED
        else:
            status = DataQualityStatus.HEALTHY

        usable = status in {DataQualityStatus.HEALTHY, DataQualityStatus.DEGRADED} and score >= 50
        return DataQualityResult(
            is_valid=usable,
            issues=issues,
            warnings=warnings,
            status=status,
            score=score,
            data_age_minutes=round(data_age_minutes, 2) if data_age_minutes is not None else None,
            last_bar_time=last_ts.to_pydatetime() if last_ts is not None else None,
            missing_bar_count=missing_bar_count,
            duplicate_bar_count=duplicate_count,
            outlier_count=outlier_count,
            incomplete_bar_count=incomplete_count,
            provider=provider,
            fallback_used=fallback_used,
            cache_used=cache_used,
            cache_age_minutes=cache_age_minutes,
            usable_for_analysis=usable,
            normalized_symbol=normalized_symbol,
            price_mode=price_mode,
            cleaned_df=working,
        )

    def completed_candles(
        self,
        df: pd.DataFrame,
        timeframe: str,
        now: Optional[datetime] = None,
    ) -> pd.DataFrame:
        """Tamamlanmamış son mumu analiz girdisinden çıkarır."""
        if df is None or df.empty:
            return df
        working = df.sort_values("timestamp").reset_index(drop=True).copy()
        working["timestamp"] = pd.to_datetime(working["timestamp"], utc=True)
        last_ts = working["timestamp"].iloc[-1]
        if _is_last_bar_incomplete(last_ts, timeframe, now or datetime.now(timezone.utc)):
            return working.iloc[:-1].reset_index(drop=True)
        return working


def validate_ohlcv(
    df: pd.DataFrame,
    min_bars: int,
    max_staleness_minutes: Optional[float] = None,
    now: Optional[datetime] = None,
    expect_timezone_aware: bool = True,
    **kwargs,
) -> DataQualityResult:
    """Geriye uyumlu kalite fonksiyonu; yeni ayrıntılı sonucu döndürür."""
    engine = DataQualityEngine()
    return engine.evaluate(
        df,
        min_bars=min_bars,
        max_staleness_minutes=max_staleness_minutes,
        now=now,
        expect_timezone_aware=expect_timezone_aware,
        **kwargs,
    )
