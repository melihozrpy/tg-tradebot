from __future__ import annotations

import csv
import json
import math
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from app.analysis.indicator_engine import ema
from app.data.base_provider import BaseMarketDataProvider, DataUnavailableError


@dataclass(frozen=True)
class BreadthCandidate:
    symbol: str
    direction: str
    score: int
    change_percent: float
    last_close: float
    relative_volume: float
    reasons: tuple[str, ...]
    confirmation_level: float | None = None
    technical_target: float | None = None
    target_basis: str = ""


@dataclass
class MarketBreadthResult:
    available: bool
    note: str
    universe_size: int = 0
    scanned: int = 0
    failed: int = 0
    coverage_ratio: Optional[float] = None
    advancers: int = 0
    decliners: int = 0
    unchanged: int = 0
    advance_decline_ratio: Optional[float] = None
    net_breadth: int = 0
    average_change_percent: Optional[float] = None
    median_change_percent: Optional[float] = None
    above_ema20_ratio: Optional[float] = None
    above_ema50_ratio: Optional[float] = None
    above_ema200_ratio: Optional[float] = None
    new_20d_highs: Optional[int] = None
    new_20d_lows: Optional[int] = None
    rising_volume_ratio: Optional[float] = None
    up_down_volume_ratio: Optional[float] = None
    breadth_score: Optional[int] = None
    regime: str = "VERİ YETERSİZ"
    tomorrow_bias: str = "BELİRSİZ"
    long_count: int = 0
    short_count: int = 0
    neutral_count: int = 0
    top_gainers: tuple[BreadthCandidate, ...] = ()
    top_losers: tuple[BreadthCandidate, ...] = ()
    long_candidates: tuple[BreadthCandidate, ...] = ()
    short_candidates: tuple[BreadthCandidate, ...] = ()
    from_cache: bool = False


@dataclass(frozen=True)
class _Snapshot:
    symbol: str
    close: float
    change: float
    volume: float
    relative_volume: float
    above20: bool
    above50: bool
    above200: bool | None
    ema20_above_ema50: bool
    momentum20: float
    new_high: bool
    new_low: bool
    prior_high: float
    prior_low: float
    atr14: float
    long_score: int
    short_score: int
    long_reasons: tuple[str, ...]
    short_reasons: tuple[str, ...]


_CACHE_LOCK = threading.Lock()
_RESULT_CACHE: dict[tuple, tuple[datetime, MarketBreadthResult]] = {}


def _resolve_source_path(source_path: str) -> Path:
    path = Path(source_path)
    if path.is_absolute() or path.exists():
        return path
    return Path(__file__).resolve().parents[2] / path


def load_symbol_universe(source_path: str) -> list[dict]:
    """CSV ve uygulamanın 571 hisselik JSON evrenini aynı arayüzle okur."""

    path = _resolve_source_path(source_path)
    if not path.exists():
        return []
    if path.suffix.casefold() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("instruments", []) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []
        output: list[dict] = []
        for item in rows:
            row = {"symbol": item} if isinstance(item, str) else item
            if not isinstance(row, dict) or not row.get("symbol"):
                continue
            if str(row.get("active", "true")).strip().casefold() not in {"false", "0", "no"}:
                output.append(row)
        return output

    with path.open("r", encoding="utf-8", newline="") as handle:
        return [
            row
            for row in csv.DictReader(handle)
            if str(row.get("active", "true")).strip().casefold() in {"true", "1", "yes"}
        ]


def _finite(value: float, default: float = 0.0) -> float:
    return float(value) if math.isfinite(float(value)) else default


def _score_snapshot(symbol: str, frame) -> _Snapshot:
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    if len(frame) < 51:
        raise DataUnavailableError("En az 51 tamamlanmış günlük bar gerekli.")
    close = frame["close"].astype(float)
    last = _finite(close.iloc[-1])
    previous = _finite(close.iloc[-2])
    if previous <= 0 or last <= 0:
        raise DataUnavailableError("Geçersiz kapanış fiyatı.")

    change = (last / previous - 1.0) * 100.0
    ema20_series = ema(close, 20)
    ema50_series = ema(close, 50)
    ema20_value = _finite(ema20_series.iloc[-1])
    ema50_value = _finite(ema50_series.iloc[-1])
    above20 = last > ema20_value
    above50 = last > ema50_value
    above200: bool | None = None
    if len(frame) >= 200:
        ema200_value = _finite(ema(close, 200).iloc[-1])
        above200 = last > ema200_value

    prior_window = frame.iloc[-21:-1]
    prior_high = _finite(prior_window["high"].max())
    prior_low = _finite(prior_window["low"].min())
    new_high = last >= prior_high
    new_low = last <= prior_low
    true_range = pd.concat(
        [
            (frame["high"].astype(float) - frame["low"].astype(float)).abs(),
            (frame["high"].astype(float) - close.shift(1)).abs(),
            (frame["low"].astype(float) - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr14 = _finite(true_range.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean().iloc[-1])
    momentum20 = (last / _finite(close.iloc[-21]) - 1.0) * 100.0
    previous_volume = _finite(frame["volume"].astype(float).iloc[-21:-1].mean())
    current_volume = max(0.0, _finite(frame["volume"].iloc[-1]))
    relative_volume = current_volume / previous_volume if previous_volume > 0 else 0.0

    long_score = 35
    short_score = 35
    long_reasons: list[str] = []
    short_reasons: list[str] = []
    if change > 0:
        long_score += min(12, 4 + int(abs(change) * 2))
        long_reasons.append(f"günlük %{change:+.2f}")
    elif change < 0:
        short_score += min(12, 4 + int(abs(change) * 2))
        short_reasons.append(f"günlük %{change:+.2f}")
    if above20:
        long_score += 12
        long_reasons.append("EMA20 üstü")
    else:
        short_score += 12
        short_reasons.append("EMA20 altı")
    if above50:
        long_score += 8
        long_reasons.append("EMA50 üstü")
    else:
        short_score += 8
        short_reasons.append("EMA50 altı")
    if ema20_value > ema50_value:
        long_score += 10
        long_reasons.append("EMA20 > EMA50")
    else:
        short_score += 10
        short_reasons.append("EMA20 < EMA50")
    if above200 is True:
        long_score += 5
        long_reasons.append("EMA200 üstü")
    elif above200 is False:
        short_score += 5
        short_reasons.append("EMA200 altı")
    if momentum20 > 0:
        long_score += min(8, 3 + int(abs(momentum20) / 3))
        long_reasons.append(f"20g momentum %{momentum20:+.1f}")
    elif momentum20 < 0:
        short_score += min(8, 3 + int(abs(momentum20) / 3))
        short_reasons.append(f"20g momentum %{momentum20:+.1f}")
    if new_high:
        long_score += 10
        long_reasons.append("20g yeni zirve")
    if new_low:
        short_score += 10
        short_reasons.append("20g yeni dip")
    if relative_volume >= 1.25:
        if change >= 0:
            long_score += 5
            long_reasons.append(f"hacim {relative_volume:.1f}x")
        else:
            short_score += 5
            short_reasons.append(f"hacim {relative_volume:.1f}x")

    return _Snapshot(
        symbol=symbol,
        close=round(last, 4),
        change=round(change, 3),
        volume=current_volume,
        relative_volume=round(relative_volume, 2),
        above20=above20,
        above50=above50,
        above200=above200,
        ema20_above_ema50=ema20_value > ema50_value,
        momentum20=round(momentum20, 3),
        new_high=new_high,
        new_low=new_low,
        prior_high=round(prior_high, 4),
        prior_low=round(prior_low, 4),
        atr14=round(atr14, 4),
        long_score=min(100, long_score),
        short_score=min(100, short_score),
        long_reasons=tuple(long_reasons),
        short_reasons=tuple(short_reasons),
    )


def _candidate(snapshot: _Snapshot, direction: str) -> BreadthCandidate:
    is_long = direction == "LONG"
    # A report target must come from an observed opposing swing first.  When a
    # stock has already closed beyond that swing, label the ATR extension
    # explicitly instead of disguising a projection as a historical level.
    if is_long:
        confirmation_level = snapshot.prior_high
        if snapshot.prior_high > snapshot.close:
            technical_target, target_basis = snapshot.prior_high, "önceki 20g direnç"
        else:
            technical_target = snapshot.close + snapshot.atr14 * 1.5
            target_basis = "1.5× ATR uzama bandı"
    else:
        confirmation_level = snapshot.prior_low
        if 0 < snapshot.prior_low < snapshot.close:
            technical_target, target_basis = snapshot.prior_low, "önceki 20g destek"
        else:
            technical_target = max(0.0, snapshot.close - snapshot.atr14 * 1.5)
            target_basis = "1.5× ATR aşağı uzama bandı"
    return BreadthCandidate(
        symbol=snapshot.symbol,
        direction=direction,
        score=snapshot.long_score if is_long else snapshot.short_score,
        change_percent=snapshot.change,
        last_close=snapshot.close,
        relative_volume=snapshot.relative_volume,
        reasons=(snapshot.long_reasons if is_long else snapshot.short_reasons)[:4],
        confirmation_level=round(confirmation_level, 4) if confirmation_level > 0 else None,
        technical_target=round(technical_target, 4) if technical_target > 0 else None,
        target_basis=target_basis,
    )


def compute_market_breadth(
    provider: BaseMarketDataProvider,
    csv_path: str,
    timeframe: str = "1d",
    max_symbols: int = 1000,
    *,
    provider_factory: Callable[[], BaseMarketDataProvider] | None = None,
    max_workers: int = 1,
    minimum_signal_score: int = 68,
    top_n: int = 12,
    cache_minutes: int = 0,
) -> MarketBreadthResult:
    """BIST evrenini fail-closed biçimde tarar ve deterministik piyasa özeti üretir.

    JSON yolu verildiğinde 571 hissenin tamamı değerlendirilir. Ağ sağlayıcıları
    thread-safe olmayabileceğinden paralel kullanımda her iş parçacığı kendi
    sağlayıcısını ``provider_factory`` üzerinden oluşturur.
    """

    cache_key = (
        str(_resolve_source_path(csv_path).resolve()), timeframe, int(max_symbols),
        int(minimum_signal_score), int(top_n),
    )
    if cache_minutes > 0:
        with _CACHE_LOCK:
            cached = _RESULT_CACHE.get(cache_key)
            if cached and datetime.now(timezone.utc) - cached[0] <= timedelta(minutes=cache_minutes):
                return replace(cached[1], from_cache=True)

    universe = load_symbol_universe(csv_path)[:max_symbols]
    symbols = [str(row.get("symbol", "")).strip().upper().removesuffix(".IS") for row in universe]
    symbols = list(dict.fromkeys(symbol for symbol in symbols if symbol))
    if not symbols:
        return MarketBreadthResult(False, "BIST sembol evreni bulunamadı veya boş.")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=430)
    local = threading.local()

    def analyze(symbol: str) -> _Snapshot:
        worker_provider = provider
        if provider_factory is not None:
            if not hasattr(local, "provider"):
                local.provider = provider_factory()
            worker_provider = local.provider
        frame = worker_provider.get_ohlcv(symbol, timeframe, start, end)
        return _score_snapshot(symbol, frame)

    snapshots: list[_Snapshot] = []
    workers = max(1, min(int(max_workers), 12)) if provider_factory is not None else 1
    if workers == 1:
        for symbol in symbols:
            try:
                snapshots.append(analyze(symbol))
            except Exception:  # Bir sembol bütün evreni bozmaz.
                continue
    else:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="bist-breadth") as pool:
            futures = {pool.submit(analyze, symbol): symbol for symbol in symbols}
            for future in as_completed(futures):
                try:
                    snapshots.append(future.result())
                except Exception:
                    continue

    scanned = len(snapshots)
    if scanned == 0:
        return MarketBreadthResult(
            False,
            f"{len(symbols)} hisselik evrende doğrulanabilir günlük veri alınamadı.",
            universe_size=len(symbols),
            failed=len(symbols),
            coverage_ratio=0.0,
        )

    advancers = sum(row.change > 0 for row in snapshots)
    decliners = sum(row.change < 0 for row in snapshots)
    unchanged = scanned - advancers - decliners
    above20 = sum(row.above20 for row in snapshots)
    above50 = sum(row.above50 for row in snapshots)
    ema200_rows = [row for row in snapshots if row.above200 is not None]
    above200 = sum(bool(row.above200) for row in ema200_rows)
    highs = sum(row.new_high for row in snapshots)
    lows = sum(row.new_low for row in snapshots)
    rising_volume = sum(row.relative_volume > 1.0 for row in snapshots)
    up_volume = sum(row.volume for row in snapshots if row.change > 0)
    down_volume = sum(row.volume for row in snapshots if row.change < 0)
    ad_ratio = advancers / decliners if decliners else float(advancers)
    changes = [row.change for row in snapshots]

    breadth_score = round(
        0.30 * (advancers / scanned * 100)
        + 0.24 * (above20 / scanned * 100)
        + 0.18 * (above50 / scanned * 100)
        + 0.12 * ((above200 / len(ema200_rows) * 100) if ema200_rows else 50)
        + 0.10 * ((highs + 1) / (highs + lows + 2) * 100)
        + 0.06 * (rising_volume / scanned * 100)
    )
    if breadth_score >= 62:
        regime, tomorrow_bias = "GÜÇLÜ / RİSK-ON", "YUKARI EĞİLİMLİ"
    elif breadth_score >= 54:
        regime, tomorrow_bias = "ILIMLI POZİTİF", "POZİTİF-YATAY"
    elif breadth_score <= 38:
        regime, tomorrow_bias = "ZAYIF / RİSK-OFF", "AŞAĞI EĞİLİMLİ"
    elif breadth_score <= 46:
        regime, tomorrow_bias = "ILIMLI NEGATİF", "NEGATİF-YATAY"
    else:
        regime, tomorrow_bias = "KARARSIZ / NÖTR", "YATAY-BELİRSİZ"

    long_rows = [
        row for row in snapshots
        if row.long_score >= minimum_signal_score and row.long_score >= row.short_score + 6
    ]
    short_rows = [
        row for row in snapshots
        if row.short_score >= minimum_signal_score and row.short_score >= row.long_score + 6
    ]
    long_rows.sort(key=lambda row: (row.long_score, row.change, row.relative_volume), reverse=True)
    short_rows.sort(key=lambda row: (row.short_score, -row.change, row.relative_volume), reverse=True)
    gainers = sorted(snapshots, key=lambda row: row.change, reverse=True)[:top_n]
    losers = sorted(snapshots, key=lambda row: row.change)[:top_n]
    coverage = round(scanned / len(symbols) * 100, 1)
    note = (
        f"{len(symbols)} hisselik BIST evreninin {scanned} tanesi doğrulandı (%{coverage:.1f}); "
        f"{len(symbols) - scanned} sembolde veri yok/yetersiz. SHORT etiketi spot satış emri değil, "
        "teknik zayıflık-risk sınıflamasıdır."
    )
    result = MarketBreadthResult(
        available=True,
        note=note,
        universe_size=len(symbols),
        scanned=scanned,
        failed=len(symbols) - scanned,
        coverage_ratio=coverage,
        advancers=advancers,
        decliners=decliners,
        unchanged=unchanged,
        advance_decline_ratio=round(ad_ratio, 2),
        net_breadth=advancers - decliners,
        average_change_percent=round(statistics.fmean(changes), 2),
        median_change_percent=round(statistics.median(changes), 2),
        above_ema20_ratio=round(above20 / scanned * 100, 1),
        above_ema50_ratio=round(above50 / scanned * 100, 1),
        above_ema200_ratio=(round(above200 / len(ema200_rows) * 100, 1) if ema200_rows else None),
        new_20d_highs=highs,
        new_20d_lows=lows,
        rising_volume_ratio=round(rising_volume / scanned * 100, 1),
        up_down_volume_ratio=(round(up_volume / down_volume, 2) if down_volume > 0 else None),
        breadth_score=max(0, min(100, breadth_score)),
        regime=regime,
        tomorrow_bias=tomorrow_bias,
        long_count=len(long_rows),
        short_count=len(short_rows),
        neutral_count=scanned - len(long_rows) - len(short_rows),
        top_gainers=tuple(_candidate(row, "LONG") for row in gainers),
        top_losers=tuple(_candidate(row, "SHORT/RİSK") for row in losers),
        long_candidates=tuple(_candidate(row, "LONG") for row in long_rows),
        short_candidates=tuple(_candidate(row, "SHORT/RİSK") for row in short_rows),
    )
    if cache_minutes > 0:
        with _CACHE_LOCK:
            _RESULT_CACHE[cache_key] = (datetime.now(timezone.utc), result)
    return result
