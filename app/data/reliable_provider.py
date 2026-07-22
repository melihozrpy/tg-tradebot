from __future__ import annotations

"""Retry, circuit breaker, kalite kapısı, fallback ve kalıcı cache katmanı."""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Callable, Optional

import pandas as pd

from app.analysis.data_quality import DataQualityEngine, DataQualityResult
from app.data.base_provider import BaseMarketDataProvider, DataFreshness, DataUnavailableError

logger = logging.getLogger("mergen_quant.data.reliable")


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 8.0

    @staticmethod
    def is_rate_limit(exc: Exception) -> bool:
        text = str(exc).lower()
        return "429" in text or "rate limit" in text or "too many request" in text

    @staticmethod
    def is_transient(exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        text = str(exc).lower()
        return any(token in text for token in ("timeout", "tempor", "connection", "network", "502", "503", "504", "rate limit", "429"))

    def execute(self, func: Callable[[], pd.DataFrame], *, sleep: Callable[[float], None] = time.sleep) -> pd.DataFrame:
        last_exc: Optional[Exception] = None
        for attempt in range(1, max(1, self.max_attempts) + 1):
            try:
                return func()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if not self.is_transient(exc) or attempt >= self.max_attempts:
                    break
                delay = min(self.max_delay_seconds, self.base_delay_seconds * (2 ** (attempt - 1)))
                if self.is_rate_limit(exc):
                    delay = min(self.max_delay_seconds, delay * 2)
                sleep(delay)
        if isinstance(last_exc, DataUnavailableError):
            raise last_exc
        raise DataUnavailableError(f"Provider çağrısı başarısız: {last_exc}") from last_exc


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, recovery_seconds: int = 120) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_seconds = max(1, recovery_seconds)
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.opened_at: Optional[datetime] = None
        self.last_failure: Optional[str] = None
        self._lock = RLock()

    def allow_request(self, now: Optional[datetime] = None) -> bool:
        with self._lock:
            now = now or datetime.now(timezone.utc)
            if self.state == CircuitState.OPEN:
                if self.opened_at and (now - self.opened_at).total_seconds() >= self.recovery_seconds:
                    self.state = CircuitState.HALF_OPEN
                    return True
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self.state = CircuitState.CLOSED
            self.consecutive_failures = 0
            self.opened_at = None
            self.last_failure = None

    def record_failure(self, exc: Exception, now: Optional[datetime] = None) -> None:
        with self._lock:
            self.consecutive_failures += 1
            self.last_failure = str(exc)
            if self.state == CircuitState.HALF_OPEN or self.consecutive_failures >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.opened_at = now or datetime.now(timezone.utc)

    def health(self) -> dict:
        return {
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "last_failure": self.last_failure,
        }


@dataclass
class CacheRecord:
    df: pd.DataFrame
    provider: str
    fetched_at: datetime
    age_minutes: float
    metadata: dict


class FileDataCache:
    """Her veri setini kaynak ve oluşturulma zamanı ile ayrı JSON dosyasında tutar."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0
        self.rejected_stale = 0

    @staticmethod
    def _safe(value: str) -> str:
        return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.upper())

    def _paths(self, symbol: str, timeframe: str) -> tuple[Path, Path]:
        stem = f"{self._safe(symbol)}__{self._safe(timeframe)}"
        return self.root / f"{stem}.json", self.root / f"{stem}.meta.json"

    def save(self, symbol: str, timeframe: str, df: pd.DataFrame, provider: str, metadata: Optional[dict] = None) -> None:
        data_path, meta_path = self._paths(symbol, timeframe)
        temp_data = data_path.with_suffix(".json.tmp")
        temp_meta = meta_path.with_suffix(".json.tmp")
        records = df.copy()
        records["timestamp"] = pd.to_datetime(records["timestamp"], utc=True).astype(str)
        temp_data.write_text(records.to_json(orient="records"), encoding="utf-8")
        payload = {
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "provider": provider,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata or {},
        }
        temp_meta.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp_data.replace(data_path)
        temp_meta.replace(meta_path)

    def load(self, symbol: str, timeframe: str, max_age_minutes: float, now: Optional[datetime] = None) -> Optional[CacheRecord]:
        data_path, meta_path = self._paths(symbol, timeframe)
        if not data_path.exists() or not meta_path.exists():
            self.misses += 1
            return None
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(payload["fetched_at"])
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            age = ((now or datetime.now(timezone.utc)) - fetched_at).total_seconds() / 60
            if age > max_age_minutes:
                self.rejected_stale += 1
                return None
            df = pd.read_json(data_path, orient="records")
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            self.hits += 1
            return CacheRecord(df=df, provider=payload.get("provider", "unknown"), fetched_at=fetched_at, age_minutes=age, metadata=payload.get("metadata") or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Yerel veri cache'i okunamadı %s/%s: %s", symbol, timeframe, exc)
            self.misses += 1
            return None

    def health(self) -> dict:
        return {"status": "ok", "path": str(self.root), "hits": self.hits, "misses": self.misses, "rejected_stale": self.rejected_stale}


class ReliableMarketDataProvider(BaseMarketDataProvider):
    """Primary -> fallback -> taze disk cache -> unavailable sırasını uygular."""

    name = "reliable"

    def __init__(
        self,
        primary: BaseMarketDataProvider,
        fallback: Optional[BaseMarketDataProvider],
        cache: FileDataCache,
        *,
        quality_engine: Optional[DataQualityEngine] = None,
        retry_policy: Optional[RetryPolicy] = None,
        circuit_failure_threshold: int = 3,
        circuit_recovery_seconds: int = 120,
        cache_max_age_daily_minutes: int = 720,
        cache_max_age_intraday_minutes: int = 30,
        price_mode: str = "unadjusted",
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.cache = cache
        self.quality_engine = quality_engine or DataQualityEngine()
        self.retry_policy = retry_policy or RetryPolicy()
        self.cache_max_age_daily_minutes = cache_max_age_daily_minutes
        self.cache_max_age_intraday_minutes = cache_max_age_intraday_minutes
        self.price_mode = price_mode
        self.circuits = {
            primary.name: CircuitBreaker(circuit_failure_threshold, circuit_recovery_seconds),
            **({fallback.name: CircuitBreaker(circuit_failure_threshold, circuit_recovery_seconds)} if fallback else {}),
        }
        self.last_fetch_metadata: dict[tuple[str, str], dict] = {}
        self.last_successful_fetch_at: Optional[datetime] = None
        self.name = f"reliable({primary.name})"

    @staticmethod
    def _min_bars(timeframe: str) -> int:
        return {"1d": 30, "1wk": 8, "1w": 8, "1h": 20, "15m": 20, "5m": 20}.get(timeframe, 20)

    def _cache_max_age(self, timeframe: str) -> int:
        return self.cache_max_age_daily_minutes if timeframe in {"1d", "1wk", "1w"} else self.cache_max_age_intraday_minutes

    def _attempt_provider(self, provider: BaseMarketDataProvider, symbol: str, timeframe: str, start: datetime, end: datetime) -> tuple[pd.DataFrame, DataQualityResult]:
        circuit = self.circuits[provider.name]
        if not circuit.allow_request():
            raise DataUnavailableError(f"{provider.name} circuit breaker OPEN.")
        try:
            df = self.retry_policy.execute(lambda: provider.get_ohlcv(symbol, timeframe, start, end))
            actions: list = []
            try:
                actions = provider.get_corporate_actions(symbol)
            except DataUnavailableError:
                pass
            quality = self.quality_engine.evaluate(
                df,
                symbol=symbol,
                timeframe=timeframe,
                min_bars=self._min_bars(timeframe),
                provider=provider.name,
                corporate_actions=actions,
                price_mode=self.price_mode,
            )
            if not quality.usable_for_analysis:
                raise DataUnavailableError(f"{provider.name} veri kalite kapısı: {quality.status.value}: {'; '.join(quality.issues)}")
            circuit.record_success()
            return quality.cleaned_df if quality.cleaned_df is not None else df, quality
        except Exception as exc:  # noqa: BLE001
            circuit.record_failure(exc)
            raise

    def get_ohlcv(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        errors: list[str] = []
        providers = [self.primary] + ([self.fallback] if self.fallback else [])
        for index, provider in enumerate(providers):
            try:
                df, quality = self._attempt_provider(provider, symbol, timeframe, start, end)
                quality.fallback_used = index > 0
                self.cache.save(symbol, timeframe, df, provider.name, metadata={"quality": quality.as_dict()})
                meta = {
                    "provider": provider.name,
                    "fallback_used": index > 0,
                    "cache_used": False,
                    "cache_age_minutes": None,
                    "quality": quality,
                    "fetched_at": datetime.now(timezone.utc),
                }
                self.last_fetch_metadata[(symbol.upper(), timeframe)] = meta
                self.last_successful_fetch_at = meta["fetched_at"]
                end_ts = pd.Timestamp(end)
                end_ts = end_ts.tz_localize("UTC") if end_ts.tzinfo is None else end_ts.tz_convert("UTC")
                start_ts = pd.Timestamp(start)
                start_ts = start_ts.tz_localize("UTC") if start_ts.tzinfo is None else start_ts.tz_convert("UTC")
                result = df.loc[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)].reset_index(drop=True)
                if result.empty:
                    raise DataUnavailableError(f"'{symbol}' için istenen aralıkta veri yok.")
                return result
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{provider.name}: {exc}")
                logger.warning("Provider başarısız symbol=%s tf=%s provider=%s: %s", symbol, timeframe, provider.name, exc)

        cached = self.cache.load(symbol, timeframe, self._cache_max_age(timeframe))
        if cached is not None:
            quality = self.quality_engine.evaluate(
                cached.df,
                symbol=symbol,
                timeframe=timeframe,
                min_bars=self._min_bars(timeframe),
                provider=cached.provider,
                fallback_used=True,
                cache_used=True,
                cache_age_minutes=cached.age_minutes,
                price_mode=self.price_mode,
            )
            if quality.usable_for_analysis:
                self.last_fetch_metadata[(symbol.upper(), timeframe)] = {
                    "provider": cached.provider,
                    "fallback_used": True,
                    "cache_used": True,
                    "cache_age_minutes": cached.age_minutes,
                    "quality": quality,
                    "fetched_at": cached.fetched_at,
                }
                start_ts = pd.Timestamp(start)
                start_ts = start_ts.tz_localize("UTC") if start_ts.tzinfo is None else start_ts.tz_convert("UTC")
                end_ts = pd.Timestamp(end)
                end_ts = end_ts.tz_localize("UTC") if end_ts.tzinfo is None else end_ts.tz_convert("UTC")
                result = cached.df.loc[(cached.df["timestamp"] >= start_ts) & (cached.df["timestamp"] <= end_ts)].reset_index(drop=True)
                if not result.empty:
                    return result

        detail = " | ".join(errors) if errors else "provider ve cache kullanılamıyor"
        raise DataUnavailableError(f"'{symbol}'/{timeframe} verisi kullanılamıyor: {detail}")

    def metadata_for(self, symbol: str, timeframe: str) -> Optional[dict]:
        return self.last_fetch_metadata.get((symbol.upper(), timeframe))

    def get_quote(self, symbol: str) -> dict:
        end = datetime.now(timezone.utc)
        df = self.get_ohlcv(symbol, "1d", end - timedelta(days=90), end)
        row = df.iloc[-1]
        meta = self.metadata_for(symbol, "1d") or {}
        return {"symbol": symbol.upper(), "price": float(row["close"]), "timestamp": row["timestamp"].to_pydatetime(), "provider": meta.get("provider", self.name), "fallback_used": meta.get("fallback_used", False), "cache_used": meta.get("cache_used", False)}

    def get_index_data(self, index_symbol: str, timeframe: str) -> pd.DataFrame:
        end = datetime.now(timezone.utc)
        return self.get_ohlcv(index_symbol, timeframe, end - timedelta(days=500), end)

    def get_sector_data(self, sector_symbol: str, timeframe: str) -> pd.DataFrame:
        return self.get_index_data(sector_symbol, timeframe)

    def get_corporate_actions(self, symbol: str) -> list:
        for provider in (self.primary, self.fallback):
            if provider is None:
                continue
            try:
                return provider.get_corporate_actions(symbol)
            except DataUnavailableError:
                continue
        return []

    def is_market_open(self) -> bool:
        return self.primary.is_market_open()

    def get_data_freshness(self, symbol: str, timeframe: str) -> DataFreshness:
        meta = self.metadata_for(symbol, timeframe)
        if meta and isinstance(meta.get("quality"), DataQualityResult):
            quality = meta["quality"]
            return DataFreshness(symbol, timeframe, quality.last_bar_time, quality.usable_for_analysis, self._cache_max_age(timeframe), meta.get("provider", self.name))
        return self.primary.get_data_freshness(symbol, timeframe)

    def health_check(self) -> dict:
        providers = {name: breaker.health() for name, breaker in self.circuits.items()}
        down = all(item["state"] == CircuitState.OPEN.value for item in providers.values())
        return {
            "provider": self.name,
            "status": "down" if down else ("degraded" if any(item["state"] != CircuitState.CLOSED.value for item in providers.values()) else "ok"),
            "providers": providers,
            "cache": self.cache.health(),
            "last_successful_fetch_at": self.last_successful_fetch_at.isoformat() if self.last_successful_fetch_at else None,
        }
