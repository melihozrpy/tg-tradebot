from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd

from app.data.base_provider import BaseMarketDataProvider, DataFreshness, DataUnavailableError

logger = logging.getLogger("mergen_quant.data.yfinance")

BIST_SUFFIX = ".IS"
MIN_REQUIRED_BARS = 250
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_SECONDS = 1.5
DEFAULT_REQUEST_DELAY_SECONDS = 1.0

# Bolum 2 spesifikasyonu: desteklenen zaman dilimleri ve yfinance interval karsiliklari.
DAILY_INTERVAL = "1d"
WEEKLY_INTERVAL = "1wk"
INTRADAY_INTERVALS = ("1h", "15m", "5m")
SUPPORTED_INTERVALS = (DAILY_INTERVAL, WEEKLY_INTERVAL) + INTRADAY_INTERVALS

# yfinance'in intraday veri icin uyguladigi gecmis sinirlarina uyum (bolum 2).
INTRADAY_MAX_PERIOD_DAYS = {"5m": 60, "15m": 60, "1h": 730}

# Zaman dilimi basina minimum onerilen bar sayisi (asilirsa uyari, hata degil).
MIN_REQUIRED_BARS_MAP = {
    DAILY_INTERVAL: 250,
    WEEKLY_INTERVAL: 52,
    "1h": 100,
    "15m": 100,
    "5m": 100,
}

# validate_bar_completion / tazelik kontrolleri icin zaman dilimi suresi.
INTERVAL_DURATION = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    DAILY_INTERVAL: timedelta(days=1),
    WEEKLY_INTERVAL: timedelta(days=7),
}

# get_data_freshness icin zaman dilimi basina izin verilen gecikme (dakika).
MAX_LAG_MINUTES_MAP = {
    "5m": 20,
    "15m": 35,
    "1h": 120,
    DAILY_INTERVAL: 7200,  # hafta sonu/tatil dahil bir sonraki islem gunune tolerans
    WEEKLY_INTERVAL: 12 * 24 * 60,
}


def normalize_bist_symbol(raw_symbol: str) -> str:
    """Kullanicinin girdigi sembolu yfinance'in bekledigi BIST formatina cevirir.

    'SVGYO' -> 'SVGYO.IS'
    'SVGYO.IS' -> 'SVGYO.IS' (tekrar eklenmez)
    Endeks sembolleri (orn. 'XU100') istisna olarak '.IS' almaz; XU100 icin
    yfinance karsiligi '^XU100' kullanilir.
    """
    symbol = raw_symbol.strip().upper()
    if symbol in ("XU100", "^XU100"):
        return "^XU100"
    # INSTRUMENTS yalnızca BIST ile sınırlı değildir. Kullanıcı doğrudan
    # Yahoo kodu verdiyse (.NYB, =X, =F, ^VIX gibi) kodu bozmayız. Yaygın
    # kullanıcı dostu adlar da burada yalnızca sağlayıcı sembolüne çevrilir;
    # enstrüman evreninin kendisi config/env'den okunmaya devam eder.
    aliases = {
        "EURUSD": "EURUSD=X",
        "XAUUSD": "GC=F",
        "XAGUSD": "SI=F",
        "US100": "^NDX",
        "VIX": "^VIX",
        "DXY": "DX-Y.NYB",
    }
    if symbol in aliases:
        return aliases[symbol]
    if symbol.startswith("^") or "=" in symbol or "." in symbol:
        return symbol
    if symbol.endswith(BIST_SUFFIX):
        return symbol
    return f"{symbol}{BIST_SUFFIX}"


def _period_to_days(period: str) -> int:
    """'250d', '2y', '6mo' gibi basit period string'lerini gun sayisina cevirir."""
    period = period.strip().lower()
    try:
        if period.endswith("d"):
            return int(period[:-1])
        if period.endswith("mo"):
            return int(period[:-2]) * 30
        if period.endswith("y"):
            return int(period[:-1]) * 365
        if period.endswith("wk"):
            return int(period[:-2]) * 7
    except ValueError:
        pass
    return 500


class YFinanceMarketDataProvider(BaseMarketDataProvider):
    """yfinance uzerinden ucretsiz gecikmeli piyasa verisi ceken saglayici.

    ONEMLI kurallar:
    - Gercek veri bulunamazsa KESINLIKLE mock veriye gecilmez; DataUnavailableError
      firlatilir ve cagiran taraf (analysis_service) fail-closed davranir.
    - Hafta sonu/tatil gunlerinde yfinance zaten yalnizca gercek islem
      gunlerini dondurdugu icin son satir otomatik olarak son islem gununun
      kapanisini temsil eder; ayrica bir "bugune tamamlama" islemi yapilmaz.
    - Tum agir/yavas cagrilar timeout + retry + exponential backoff ile sarilir.
    - V3.1: gunluk (1d) yaninda saatlik (1h), 15 dakikalik (15m), 5 dakikalik
      (5m) ve haftalik (1wk) zaman dilimleri de desteklenir (bolum 2).
    """

    name = "yfinance"

    def __init__(
        self,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        request_delay_seconds: float = DEFAULT_REQUEST_DELAY_SECONDS,
        price_mode: str = "unadjusted",
    ):
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self.request_delay_seconds = request_delay_seconds
        self.price_mode = price_mode
        self._cache: dict[tuple, pd.DataFrame] = {}
        self._intraday_cache: dict[tuple, tuple[datetime, pd.DataFrame]] = {}
        self._last_request_at: Optional[float] = None

    # ------------------------------------------------------------------
    # Rate limiting (istekler arasinda kontrollu gecikme)
    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        if self.request_delay_seconds <= 0:
            return
        now = time.monotonic()
        if self._last_request_at is not None:
            elapsed = now - self._last_request_at
            remaining = self.request_delay_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

    # ------------------------------------------------------------------
    # Dusuk seviye veri cekme (test edilebilirlik icin ayri metotlar)
    # ------------------------------------------------------------------
    def _fetch_history_raw(self, yf_symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """yfinance'ten GUNLUK ham veri ceker (geriye donuk uyumluluk icin ayni imza).

        Testlerde bu metot monkeypatch edilir ki gercek ag erisimi olmadan
        saglayicinin geri kalan mantigi (normalizasyon, hata yonetimi, retry)
        dogrulanabilsin.
        """
        import yfinance as yf

        ticker = yf.Ticker(yf_symbol)
        history = ticker.history(
            start=start.strftime("%Y-%m-%d"),
            end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
            interval=DAILY_INTERVAL,
            timeout=self.timeout_seconds,
            auto_adjust=self.price_mode == "adjusted",
        )
        return history

    def _fetch_history_raw_weekly(self, yf_symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """yfinance'ten HAFTALIK ham veri ceker (interval='1wk')."""
        import yfinance as yf

        ticker = yf.Ticker(yf_symbol)
        history = ticker.history(
            start=start.strftime("%Y-%m-%d"),
            end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
            interval=WEEKLY_INTERVAL,
            timeout=self.timeout_seconds,
            auto_adjust=self.price_mode == "adjusted",
        )
        return history

    def _fetch_history_raw_intraday(self, yf_symbol: str, interval: str, period: str) -> pd.DataFrame:
        """yfinance'ten GUN ICI (1h/15m/5m) ham veri ceker.

        yfinance intraday veride start/end yerine 'period' parametresini tercih
        eder; bu metot da testlerde monkeypatch edilebilir.
        """
        import yfinance as yf

        ticker = yf.Ticker(yf_symbol)
        history = ticker.history(
            period=period,
            interval=interval,
            timeout=self.timeout_seconds,
            auto_adjust=self.price_mode == "adjusted",
        )
        return history

    def _retry_fetch(self, fetch_fn: Callable[[], pd.DataFrame], description: str) -> pd.DataFrame:
        """Genel amacli retry/backoff sarmalayicisi (gunluk/haftalik/intraday ortak)."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                self._throttle()
                raw = fetch_fn()
                if raw is None or raw.empty:
                    raise DataUnavailableError(f"{description} icin yfinance'ten veri donmedi (bos sonuc).")
                return raw
            except DataUnavailableError:
                raise
            except Exception as exc:  # noqa: BLE001 - ag/kutuphane hatalarini genel yakala
                last_exc = exc
                logger.warning(
                    "yfinance veri cekme hatasi (deneme %s/%s) %s: %s",
                    attempt,
                    self.max_retries,
                    description,
                    exc,
                )
                if attempt < self.max_retries:
                    sleep_seconds = self.backoff_base_seconds * (2 ** (attempt - 1))
                    time.sleep(sleep_seconds)

        raise DataUnavailableError(
            f"{description} icin veri alinamadi ({self.max_retries} deneme sonrasi): {last_exc}"
        )

    def _fetch_with_retry(self, yf_symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Geriye donuk uyumluluk icin korunan gunluk retry metodu."""
        return self._retry_fetch(lambda: self._fetch_history_raw(yf_symbol, start, end), f"'{yf_symbol}' (gunluk)")

    # ------------------------------------------------------------------
    # Normalizasyon (tum zaman dilimleri icin ortak)
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_raw(raw: pd.DataFrame, yf_symbol: str) -> pd.DataFrame:
        raw = raw.reset_index()
        raw.columns = [str(c).lower() for c in raw.columns]
        rename_map = {"date": "timestamp", "datetime": "timestamp"}
        raw = raw.rename(columns=rename_map)

        required = {"timestamp", "open", "high", "low", "close", "volume"}
        missing = required - set(raw.columns)
        if missing:
            raise DataUnavailableError(f"'{yf_symbol}' icin donen veri eksik kolonlar iceriyor: {missing}")

        df = raw[["timestamp", "open", "high", "low", "close", "volume"]].copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.dropna(subset=["open", "high", "low", "close", "volume"])
        # Yinelenen tarihleri (ayni bar iki kez donerse) son degeri tutarak temizle.
        df = df.drop_duplicates(subset="timestamp", keep="last")
        df = df.sort_values("timestamp").reset_index(drop=True)

        if df.empty:
            raise DataUnavailableError(f"'{yf_symbol}' icin gecerli (bozuk olmayan) veri satiri bulunamadi.")

        return df

    def _get_normalized_df(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Gunluk (1d) normalize edilmis veri. Kisa sureli ic bellek cache'i vardir."""
        yf_symbol = normalize_bist_symbol(symbol)
        cache_key = (yf_symbol, DAILY_INTERVAL, start.date(), end.date())
        if cache_key in self._cache:
            return self._cache[cache_key].copy()

        raw = self._fetch_with_retry(yf_symbol, start, end)
        df = self._normalize_raw(raw, yf_symbol)

        self._cache[cache_key] = df
        return df.copy()

    def _get_normalized_weekly_df(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        yf_symbol = normalize_bist_symbol(symbol)
        cache_key = (yf_symbol, WEEKLY_INTERVAL, start.date(), end.date())
        if cache_key in self._cache:
            return self._cache[cache_key].copy()

        raw = self._retry_fetch(
            lambda: self._fetch_history_raw_weekly(yf_symbol, start, end), f"'{yf_symbol}' (haftalik)"
        )
        df = self._normalize_raw(raw, yf_symbol)
        self._cache[cache_key] = df
        return df.copy()

    def _get_normalized_intraday_df(self, symbol: str, interval: str, period: str) -> pd.DataFrame:
        yf_symbol = normalize_bist_symbol(symbol)
        cache_key = (yf_symbol, interval, period)
        cached = self._intraday_cache.get(cache_key)
        if cached is not None:
            fetched_at, cached_df = cached
            if (datetime.now(timezone.utc) - fetched_at).total_seconds() < 60:
                # Ayni veriyi kisa surede (1 dk icinde) tekrar indirme.
                return cached_df.copy()

        raw = self._retry_fetch(
            lambda: self._fetch_history_raw_intraday(yf_symbol, interval, period),
            f"'{yf_symbol}' ({interval} gun ici)",
        )
        df = self._normalize_raw(raw, yf_symbol)
        self._intraday_cache[cache_key] = (datetime.now(timezone.utc), df)
        return df.copy()

    # ------------------------------------------------------------------
    # BaseMarketDataProvider arayuzu
    # ------------------------------------------------------------------
    def get_quote(self, symbol: str) -> dict:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=15)
        df = self._get_normalized_df(symbol, start, end)
        last = df.iloc[-1]
        return {
            "symbol": normalize_bist_symbol(symbol),
            "price": round(float(last["close"]), 2),
            "timestamp": last["timestamp"].to_pydatetime(),
            "provider": self.name,
        }

    def get_ohlcv(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        if timeframe not in SUPPORTED_INTERVALS:
            raise DataUnavailableError(
                f"yfinance saglayicida '{timeframe}' zaman dilimi desteklenmiyor. "
                f"Desteklenenler: {SUPPORTED_INTERVALS}"
            )
        if start >= end:
            raise ValueError("start, end degerinden once olmali.")

        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        if timeframe == DAILY_INTERVAL:
            df = self._get_normalized_df(symbol, start, end)
        elif timeframe == WEEKLY_INTERVAL:
            df = self._get_normalized_weekly_df(symbol, start, end)
        else:
            requested_days = max((end - start).days, 1)
            max_days = INTRADAY_MAX_PERIOD_DAYS[timeframe]
            clamped_days = min(requested_days, max_days)
            if clamped_days < requested_days:
                logger.info(
                    "'%s' icin istenen %s gunluk %s araligi yfinance sinirina gore %s gune kisaltildi.",
                    symbol,
                    requested_days,
                    timeframe,
                    clamped_days,
                )
            df = self._get_normalized_intraday_df(symbol, timeframe, period=f"{clamped_days}d")

        mask = (df["timestamp"] >= start) & (df["timestamp"] <= end)
        result = df.loc[mask].reset_index(drop=True)
        if result.empty:
            raise DataUnavailableError(f"'{symbol}' icin istenen tarih araliginda yfinance verisi yok.")

        min_bars = MIN_REQUIRED_BARS_MAP.get(timeframe, MIN_REQUIRED_BARS)
        if len(result) < min_bars:
            logger.info(
                "'%s'/%s icin %s bar bulundu (< onerilen %s); analiz yine de mevcut veriyle denenecek.",
                symbol,
                timeframe,
                len(result),
                min_bars,
            )
        # Completion is explicit for point-in-time backtests. A bar whose
        # expected end is after the request cutoff is never silently accepted.
        duration = INTERVAL_DURATION.get(timeframe, timedelta(days=1))
        cutoff = pd.Timestamp(end)
        if cutoff.tzinfo is None:
            cutoff = cutoff.tz_localize("UTC")
        else:
            cutoff = cutoff.tz_convert("UTC")
        result["is_complete"] = (
            pd.to_datetime(result["timestamp"], utc=True) + duration <= cutoff
        )
        return result

    # ------------------------------------------------------------------
    # V3.1: Bolum 2 - yeni gun ici / coklu zaman dilimi metotlari
    # ------------------------------------------------------------------
    def get_daily_ohlcv(self, symbol: str, period: str = "2y") -> pd.DataFrame:
        """Gunluk OHLCV veri getirir ('2y', '1y', '250d' gibi basit period string'i)."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=_period_to_days(period))
        return self.get_ohlcv(symbol, DAILY_INTERVAL, start, end)

    def get_intraday_ohlcv(self, symbol: str, interval: str = "15m", period: Optional[str] = None) -> pd.DataFrame:
        """Gun ici (1h/15m/5m) OHLCV verisi getirir.

        yfinance'in gecmis sinirlarina uyulur (bkz. INTRADAY_MAX_PERIOD_DAYS);
        istenen period bu sinirdan buyukse otomatik olarak kisaltilir.
        Gun ici veri gecikmeli olabilir; cagiran taraf (Telegram katmani) bu
        durumu kullaniciya acikca belirtmelidir.
        """
        if interval not in INTRADAY_INTERVALS:
            raise DataUnavailableError(
                f"'{interval}' bir gun ici zaman dilimi degil. Desteklenenler: {INTRADAY_INTERVALS}"
            )
        max_days = INTRADAY_MAX_PERIOD_DAYS[interval]
        requested_days = _period_to_days(period) if period else max_days
        clamped_days = min(requested_days, max_days)
        df = self._get_normalized_intraday_df(symbol, interval, period=f"{clamped_days}d")
        min_bars = MIN_REQUIRED_BARS_MAP.get(interval, 100)
        if len(df) < min_bars:
            logger.info(
                "'%s'/%s icin %s bar bulundu (< onerilen %s).", symbol, interval, len(df), min_bars
            )
        return df

    def get_multi_timeframe_data(self, symbol: str) -> dict:
        """Bes zaman diliminin (5m,15m,1h,1d,1wk) tamamini tek seferde getirir.

        Bir zaman dilimi icin veri alinamazsa DIGER zaman dilimlerini etkilemez;
        o zaman dilimi icin sonuc sozlugunde None + hata mesaji doner (uydurma
        veri KESINLIKLE kullanilmaz).
        """
        result: dict[str, dict] = {}
        for tf in ("5m", "15m", "1h", DAILY_INTERVAL, WEEKLY_INTERVAL):
            try:
                if tf == DAILY_INTERVAL:
                    df = self.get_daily_ohlcv(symbol, period="2y")
                elif tf == WEEKLY_INTERVAL:
                    end = datetime.now(timezone.utc)
                    start = end - timedelta(days=365 * 3)
                    df = self.get_ohlcv(symbol, WEEKLY_INTERVAL, start, end)
                else:
                    df = self.get_intraday_ohlcv(symbol, interval=tf)
                result[tf] = {"data": df, "available": True, "error": None}
            except DataUnavailableError as exc:
                result[tf] = {"data": None, "available": False, "error": str(exc)}
        return result

    def get_latest_intraday_snapshot(self, symbol: str) -> dict:
        """Gun ici en guncel on-analiz icin ozet anlik goruntu (bolum 3).

        Onceki kapanisi (dunku/son islem gununun kapanisi) gunluk veriden,
        bugunku hareketi 15 dakikalik gun ici veriden alir. Gun ici veri
        bulunamazsa 'Gün içi veri alınamadı' bilgisini acikca isaretler.
        """
        try:
            intraday_df = self.get_intraday_ohlcv(symbol, interval="15m", period="5d")
        except DataUnavailableError as exc:
            return {
                "available": False,
                "detail": "Gün içi veri alınamadı.",
                "error": str(exc),
            }

        last_row = intraday_df.iloc[-1]
        last_ts = last_row["timestamp"]
        last_date = last_ts.tz_convert("UTC").date() if last_ts.tzinfo else last_ts.date()
        today_rows = intraday_df[
            intraday_df["timestamp"].apply(lambda ts: (ts.tz_convert("UTC") if ts.tzinfo else ts).date()) == last_date
        ]

        prev_close: Optional[float] = None
        try:
            daily_df = self.get_daily_ohlcv(symbol, period="15d")
            daily_before_today = daily_df[
                daily_df["timestamp"].apply(lambda ts: (ts.tz_convert("UTC") if ts.tzinfo else ts).date()) < last_date
            ]
            if not daily_before_today.empty:
                prev_close = round(float(daily_before_today.iloc[-1]["close"]), 2)
        except DataUnavailableError:
            prev_close = None

        last_price = round(float(last_row["close"]), 2)
        freshness = self.get_data_freshness(symbol, "15m")

        return {
            "available": True,
            "last_price": last_price,
            "today_open": round(float(today_rows.iloc[0]["open"]), 2) if not today_rows.empty else None,
            "today_high": round(float(today_rows["high"].max()), 2) if not today_rows.empty else None,
            "today_low": round(float(today_rows["low"].min()), 2) if not today_rows.empty else None,
            "today_volume": float(today_rows["volume"].sum()) if not today_rows.empty else None,
            "previous_close": prev_close,
            "daily_change_percent": (
                round(((last_price - prev_close) / prev_close) * 100, 2)
                if prev_close and prev_close > 0
                else None
            ),
            "last_update": last_ts.to_pydatetime() if hasattr(last_ts, "to_pydatetime") else last_ts,
            "is_fresh": freshness.is_fresh,
            "max_allowed_lag_minutes": freshness.max_allowed_lag_minutes,
            "detail": "" if freshness.is_fresh else "Gün içi veri gecikmeli olabilir.",
            "intraday_df": intraday_df,
        }

    def get_market_index_data(self, index_symbol: str, interval: str = DAILY_INTERVAL) -> pd.DataFrame:
        """Endeks (XU100 vb.) verisini istenen zaman diliminde getirir."""
        if interval == DAILY_INTERVAL:
            return self.get_daily_ohlcv(index_symbol, period="2y")
        if interval == WEEKLY_INTERVAL:
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=365 * 3)
            return self.get_ohlcv(index_symbol, WEEKLY_INTERVAL, start, end)
        return self.get_intraday_ohlcv(index_symbol, interval=interval)

    def validate_bar_completion(self, df: pd.DataFrame, interval: str) -> dict:
        """Son barin (mumun) tamamlanip tamamlanmadigini kontrol eder.

        Tamamlanmamis bir bar KESINLESMIS kapanis gibi kullanilmamalidir;
        cagiran taraf bu sonuca gore PREVIEW/CONFIRMED ayrimi yapabilir.
        """
        if df is None or df.empty:
            return {"is_complete": False, "detail": "Veri seti bos, tamamlanma durumu belirlenemedi."}

        last_ts = df["timestamp"].iloc[-1]
        if getattr(last_ts, "tzinfo", None) is None and hasattr(last_ts, "tz_localize"):
            last_ts = last_ts.tz_localize("UTC")
        last_ts_py = last_ts.to_pydatetime() if hasattr(last_ts, "to_pydatetime") else last_ts

        bar_duration = INTERVAL_DURATION.get(interval, timedelta(days=1))
        now = datetime.now(timezone.utc)
        expected_bar_end = last_ts_py + bar_duration
        is_complete = now >= expected_bar_end

        return {
            "is_complete": is_complete,
            "bar_start": last_ts_py,
            "expected_bar_end": expected_bar_end,
            "detail": "" if is_complete else "Son mum henüz tamamlanmadı (gün içi/gerçekleşmemiş bar).",
        }

    def get_index_data(self, index_symbol: str, timeframe: str) -> pd.DataFrame:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=500)
        return self.get_ohlcv(index_symbol, timeframe, start, end)

    def is_market_open(self) -> bool:
        now_ist = datetime.now(timezone.utc) + timedelta(hours=3)
        if now_ist.weekday() >= 5:
            return False
        return 10 <= now_ist.hour < 18

    def get_data_freshness(self, symbol: str, timeframe: str = DAILY_INTERVAL) -> DataFreshness:
        max_lag = MAX_LAG_MINUTES_MAP.get(timeframe, 7200)
        try:
            if timeframe in INTRADAY_INTERVALS:
                df = self.get_intraday_ohlcv(
                    symbol, interval=timeframe, period=f"{min(5, INTRADAY_MAX_PERIOD_DAYS[timeframe])}d"
                )
            elif timeframe == WEEKLY_INTERVAL:
                end = datetime.now(timezone.utc)
                start = end - timedelta(days=120)
                df = self._get_normalized_weekly_df(symbol, start, end)
            else:
                end = datetime.now(timezone.utc)
                start = end - timedelta(days=15)
                df = self._get_normalized_df(symbol, start, end)
        except DataUnavailableError:
            return DataFreshness(
                symbol=symbol,
                timeframe=timeframe,
                last_timestamp=None,
                is_fresh=False,
                max_allowed_lag_minutes=max_lag,
                provider=self.name,
            )
        last_ts = df.iloc[-1]["timestamp"].to_pydatetime()
        lag_minutes = (datetime.now(timezone.utc) - last_ts).total_seconds() / 60
        return DataFreshness(
            symbol=symbol,
            timeframe=timeframe,
            last_timestamp=last_ts,
            is_fresh=lag_minutes <= max_lag,
            max_allowed_lag_minutes=max_lag,
            provider=self.name,
        )

    def health_check(self) -> dict:
        detail_parts: list[str] = []
        overall_status = "ok"
        try:
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=15)
            self._get_normalized_df("XU100", start, end)
            detail_parts.append("gunluk: ok")
        except DataUnavailableError as exc:
            overall_status = "degraded"
            detail_parts.append(f"gunluk: {exc}")
        except Exception as exc:  # noqa: BLE001
            overall_status = "down"
            detail_parts.append(f"gunluk: {exc}")

        return {
            "provider": self.name,
            "status": overall_status,
            "detail": "; ".join(detail_parts) or "yfinance baglantisi calisiyor",
        }
