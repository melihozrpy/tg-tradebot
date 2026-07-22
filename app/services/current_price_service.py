from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from app.analysis.data_quality import DataQualityEngine
from app.data.base_provider import BaseMarketDataProvider
from app.utils.financial_formatter import finite_float, percent_change, round_money

FALLBACK_WARNING = "⚠️ Güncel fiyat alınamadı; son kesinleşmiş kapanış kullanılıyor."


def _as_datetime(value) -> Optional[datetime]:
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return ts.to_pydatetime()
    except Exception:  # noqa: BLE001 - fiyat adaylarından biri bozuksa sıradaki denenir
        return None


@dataclass(frozen=True)
class CurrentPriceResult:
    symbol: str
    current_price: Optional[float]
    current_price_timestamp: Optional[datetime]
    current_price_source: str
    is_live_price: bool
    analysis_close: Optional[float]
    previous_close: Optional[float]
    daily_change_percent: Optional[float]
    fallback_used: bool = False
    warning: Optional[str] = None
    diagnostics: tuple[str, ...] = field(default_factory=tuple)

    @property
    def available(self) -> bool:
        return self.current_price is not None


class CurrentPriceResolver:
    """Anlık fiyatı kesinleşmiş analiz kapanışından merkezi olarak ayırır.

    Teknik indikatör üretmez; tamamlanmış günlük seri yalnızca ``analysis_close``
    ve ``previous_close`` referanslarını belirlemek için kullanılır.
    """

    def __init__(self, provider: BaseMarketDataProvider, timezone_name: str = "Europe/Istanbul") -> None:
        self.provider = provider
        self.timezone_name = timezone_name
        self.quality = DataQualityEngine()

    def _daily_reference(self, symbol: str, now: datetime, daily_df: Optional[pd.DataFrame]) -> tuple:
        errors: list[str] = []
        df = daily_df
        if df is None:
            try:
                df = self.provider.get_ohlcv(symbol, "1d", now - timedelta(days=45), now)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"1d: {exc}")
                df = None
        if df is None or df.empty:
            return None, None, None, errors
        complete = self.quality.completed_candles(df, "1d", now=now)
        if complete is None or complete.empty:
            return None, None, None, errors
        complete = complete.sort_values("timestamp").reset_index(drop=True)
        analysis_close = round_money(complete.iloc[-1].get("close"))
        previous_close = round_money(complete.iloc[-2].get("close")) if len(complete) > 1 else None
        timestamp = _as_datetime(complete.iloc[-1].get("timestamp"))
        return analysis_close, previous_close, timestamp, errors

    @staticmethod
    def _snapshot_candidate(snapshot: dict) -> tuple[Optional[float], Optional[datetime], str]:
        if not snapshot or snapshot.get("available") is False:
            return None, None, ""
        price = finite_float(snapshot.get("last_price", snapshot.get("price")))
        timestamp = _as_datetime(
            snapshot.get("timestamp") or snapshot.get("last_update") or snapshot.get("data_timestamp")
        )
        source = str(snapshot.get("provider") or snapshot.get("source") or "intraday_snapshot")
        return round_money(price), timestamp, source

    def resolve(
        self,
        symbol: str,
        *,
        now: Optional[datetime] = None,
        daily_df: Optional[pd.DataFrame] = None,
        allow_provider_calls: bool = True,
    ) -> CurrentPriceResult:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        symbol = symbol.strip().upper()
        analysis_close, previous_close, daily_ts, errors = self._daily_reference(symbol, now, daily_df)

        # Tarama/scheduler gibi günlük veriyi zaten çekmiş akışlarda provider'a
        # ikinci kez gitmeden en son ham satırı snapshot olarak kullan.
        if not allow_provider_calls and daily_df is not None and not daily_df.empty:
            raw_row = daily_df.sort_values("timestamp").iloc[-1]
            raw_price = round_money(raw_row.get("close"))
            raw_ts = _as_datetime(raw_row.get("timestamp"))
            if raw_price is not None and raw_price > 0:
                is_live = bool(raw_ts and daily_ts and raw_ts > daily_ts)
                source = "prefetched_provider_latest" if is_live else "prefetched_confirmed_close"
                return CurrentPriceResult(
                    symbol, raw_price, raw_ts, source, is_live,
                    analysis_close, previous_close,
                    percent_change(raw_price, analysis_close if is_live else previous_close),
                    fallback_used=not is_live,
                    warning=FALLBACK_WARNING if not is_live else None,
                    diagnostics=tuple(errors),
                )

        # 1) Sağlayıcının gerçek intraday snapshot alanı.
        snapshot_method = getattr(self.provider, "get_latest_intraday_snapshot", None)
        if callable(snapshot_method):
            try:
                price, timestamp, source = self._snapshot_candidate(snapshot_method(symbol))
                if price is not None and price > 0:
                    reference = analysis_close or previous_close
                    return CurrentPriceResult(
                        symbol, price, timestamp or now, source or "intraday_snapshot", True,
                        analysis_close, previous_close, percent_change(price, reference), diagnostics=tuple(errors),
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"snapshot: {exc}")

        # 2-4) Son tamamlanmış intraday mumlar.
        periods = {"5m": 5, "15m": 10, "1h": 90}
        source_labels = {"5m": "completed_5m", "15m": "completed_15m", "1h": "completed_1h"}
        for timeframe in ("5m", "15m", "1h"):
            try:
                df = self.provider.get_ohlcv(symbol, timeframe, now - timedelta(days=periods[timeframe]), now)
                complete = self.quality.completed_candles(df, timeframe, now=now)
                if complete is None or complete.empty:
                    continue
                row = complete.sort_values("timestamp").iloc[-1]
                price = round_money(row.get("close"))
                if price is None or price <= 0:
                    continue
                return CurrentPriceResult(
                    symbol, price, _as_datetime(row.get("timestamp")), source_labels[timeframe], True,
                    analysis_close, previous_close, percent_change(price, analysis_close or previous_close),
                    diagnostics=tuple(errors),
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{timeframe}: {exc}")

        # 5) Provider latest-price/quote alanı.
        try:
            quote = self.provider.get_quote(symbol) or {}
            price = round_money(quote.get("price", quote.get("last_price")))
            if price is not None and price > 0:
                source = str(quote.get("provider") or getattr(self.provider, "name", "provider_quote"))
                return CurrentPriceResult(
                    symbol, price, _as_datetime(quote.get("timestamp")) or now, f"provider_quote:{source}", True,
                    analysis_close, previous_close, percent_change(price, analysis_close or previous_close),
                    diagnostics=tuple(errors),
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"quote: {exc}")

        # 6) Açık ve görünür günlük kapanış fallback'i.
        if analysis_close is not None:
            return CurrentPriceResult(
                symbol, analysis_close, daily_ts, "confirmed_daily_close", False,
                analysis_close, previous_close, percent_change(analysis_close, previous_close),
                fallback_used=True, warning=FALLBACK_WARNING, diagnostics=tuple(errors),
            )
        return CurrentPriceResult(
            symbol, None, None, "unavailable", False, None, previous_close, None,
            fallback_used=True, warning=FALLBACK_WARNING, diagnostics=tuple(errors),
        )


def resolve_current_price(
    provider: BaseMarketDataProvider,
    symbol: str,
    *,
    now: Optional[datetime] = None,
    daily_df: Optional[pd.DataFrame] = None,
    timezone_name: str = "Europe/Istanbul",
    allow_provider_calls: bool = True,
) -> CurrentPriceResult:
    return CurrentPriceResolver(provider, timezone_name=timezone_name).resolve(
        symbol, now=now, daily_df=daily_df, allow_provider_calls=allow_provider_calls
    )


def resolve_portfolio_prices(
    provider: BaseMarketDataProvider,
    symbols: list[str],
    *,
    timezone_name: str = "Europe/Istanbul",
) -> tuple[dict[str, float], dict[str, CurrentPriceResult]]:
    """Bir sembol hatasının tüm portföy fiyatlamasını durdurmadığı toplu çözüm."""
    prices: dict[str, float] = {}
    contexts: dict[str, CurrentPriceResult] = {}
    resolver = CurrentPriceResolver(provider, timezone_name=timezone_name)
    for symbol in dict.fromkeys(value.upper() for value in symbols):
        try:
            result = resolver.resolve(symbol)
            contexts[symbol] = result
            if result.current_price is not None:
                prices[symbol] = result.current_price
        except Exception:  # noqa: BLE001 - tek sembol diğerlerini engellemez
            continue
    return prices, contexts
