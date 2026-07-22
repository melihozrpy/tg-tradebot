from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app.analysis.indicator_engine import InsufficientDataError, ema
from app.data.base_provider import BaseMarketDataProvider, DataUnavailableError


@dataclass
class MarketBreadthResult:
    available: bool
    note: str
    universe_size: int = 0
    scanned: int = 0
    advancers: int = 0
    decliners: int = 0
    unchanged: int = 0
    above_ema20_ratio: Optional[float] = None
    above_ema50_ratio: Optional[float] = None
    new_20d_highs: Optional[int] = None
    new_20d_lows: Optional[int] = None
    rising_volume_ratio: Optional[float] = None


def load_symbol_universe(csv_path: str) -> list[dict]:
    path = Path(csv_path)
    if not path.exists():
        return []
    rows = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if str(row.get("active", "true")).strip().lower() in ("true", "1", "yes"):
                rows.append(row)
    return rows


def compute_market_breadth(
    provider: BaseMarketDataProvider,
    csv_path: str,
    timeframe: str = "1d",
    max_symbols: int = 50,
) -> MarketBreadthResult:
    """Yerel sembol evreni CSV'si uzerinden ucretsiz piyasa genisligi hesaplar.

    Tum evren HER analizde yeniden indirilmez (cagiran taraf zaten
    provider'in kendi cache katmanini kullanir); max_symbols ile taranan
    evren buyuklugu kontrol edilir ve sonuc mesajinda acikca belirtilir.
    Yetersiz veri varsa (evren bos ise) available=False doner.
    """
    universe = load_symbol_universe(csv_path)
    if not universe:
        return MarketBreadthResult(available=False, note="Yerel sembol evreni (bist_symbols.csv) bulunamadi/bos.")

    universe = universe[:max_symbols]

    advancers = decliners = unchanged = 0
    above_ema20 = above_ema50 = 0
    new_highs = new_lows = 0
    rising_volume = 0
    scanned = 0

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=120)

    for row in universe:
        symbol = row.get("symbol", "").strip().upper()
        if not symbol:
            continue
        try:
            df = provider.get_ohlcv(symbol, timeframe, start, end)
        except DataUnavailableError:
            continue
        if len(df) < 21:
            continue

        scanned += 1
        df = df.sort_values("timestamp").reset_index(drop=True)
        close = df["close"]
        last_close = float(close.iloc[-1])
        prev_close = float(close.iloc[-2])

        if last_close > prev_close:
            advancers += 1
        elif last_close < prev_close:
            decliners += 1
        else:
            unchanged += 1

        ema20 = ema(close, 20)
        if not ema20.isna().iloc[-1] and last_close > float(ema20.iloc[-1]):
            above_ema20 += 1
        if len(df) >= 50:
            ema50 = ema(close, 50)
            if not ema50.isna().iloc[-1] and last_close > float(ema50.iloc[-1]):
                above_ema50 += 1

        window20 = df.tail(20)
        if last_close >= float(window20["high"].max()):
            new_highs += 1
        if last_close <= float(window20["low"].min()):
            new_lows += 1

        if len(df) >= 21:
            avg_vol = df["volume"].tail(20).mean()
            if float(df["volume"].iloc[-1]) > avg_vol:
                rising_volume += 1

    if scanned == 0:
        return MarketBreadthResult(
            available=False,
            note=f"Taranan evrende ({len(universe)} sembol) yeterli veri bulunamadi.",
            universe_size=len(universe),
        )

    return MarketBreadthResult(
        available=True,
        note=f"Taranan evren buyuklugu: {scanned}/{len(universe)} sembol.",
        universe_size=len(universe),
        scanned=scanned,
        advancers=advancers,
        decliners=decliners,
        unchanged=unchanged,
        above_ema20_ratio=round(above_ema20 / scanned * 100, 1),
        above_ema50_ratio=round(above_ema50 / scanned * 100, 1) if scanned else None,
        new_20d_highs=new_highs,
        new_20d_lows=new_lows,
        rising_volume_ratio=round(rising_volume / scanned * 100, 1),
    )
