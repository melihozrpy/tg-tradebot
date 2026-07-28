from __future__ import annotations

"""MERGEN QUANT - Asama 5, Bolum 2: Gunluk / Haftalik / Aylik destek-direnc.

Mevcut `support_resistance_engine` MANTIGI korunur ve genisletilir: ayni
kumeleme (cluster) / guven puanlama yaklasimini gunluk barlarin yaninda
haftalik ve aylik resample edilmis mumlara da uygular. Zaman dilimleri
BIRBIRINE KARISTIRILMAZ - her biri kendi DataFrame'i uzerinden ayri ayri
hesaplanir ve sonuc nesnesinde ayri alanlarda tutulur.

Yeterli veri yoksa seviye UYDURULMAZ; ilgili timeframe icin `reliable=False`
ve "Guvenilir seviye hesaplanamadi." notu doner.
"""

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from app.analysis.indicator_engine import atr as compute_atr
from app.analysis.indicator_engine import ema as compute_ema
from app.analysis.support_resistance_engine import (
    CLUSTER_ATR_FACTOR,
    CLUSTER_PRICE_FACTOR,
    LevelZone,
    _anchored_vwap,
    _classic_pivot_points,
    _cluster_levels,
    _find_swing_points,
    _volume_profile_zones,
    _zone_confidence,
    round2,
)

TIMEFRAME_DAILY = "gunluk"
TIMEFRAME_WEEKLY = "haftalik"
TIMEFRAME_MONTHLY = "aylik"

MIN_BARS = {
    TIMEFRAME_DAILY: 30,
    TIMEFRAME_WEEKLY: 8,
    TIMEFRAME_MONTHLY: 4,
}

RELIABLE_NOTE = "Guvenilir seviye hesaplanamadi."


@dataclass
class LevelDetail:
    """Tek bir destek/direnc seviyesi icin bolge (zone) detayi."""

    low: float
    high: float
    mid: float
    confidence: float
    touches: int
    rejections: int
    last_test_date: Optional[str]
    sources: list[str] = field(default_factory=list)
    volume_confirmed: bool = False
    timeframe: str = TIMEFRAME_DAILY
    note: str = ""
    successful_reactions: int = 0
    failed_tests: int = 0
    break_count: int = 0
    age_bars: int = 0
    distance_percent: float = 0.0
    strength_class: str = "Veri yetersiz"
    active: bool = True
    next_zone_low: Optional[float] = None
    next_zone_high: Optional[float] = None
    invalidation_condition: str = ""
    role_reversal: bool = False

    def as_range_text(self) -> str:
        if abs(self.high - self.low) < 0.01:
            return f"{self.mid:.2f} TL"
        return f"{self.low:.2f}-{self.high:.2f} TL"


@dataclass
class TimeframeLevelResult:
    timeframe: str
    reliable: bool
    note: str
    support_1: Optional[LevelDetail] = None
    support_2: Optional[LevelDetail] = None
    main_support: Optional[LevelDetail] = None
    resistance_1: Optional[LevelDetail] = None
    resistance_2: Optional[LevelDetail] = None
    main_resistance: Optional[LevelDetail] = None


@dataclass
class MultiTimeframeLevelsResult:
    daily: TimeframeLevelResult
    weekly: TimeframeLevelResult
    monthly: TimeframeLevelResult

    def all_zones(self) -> list[LevelDetail]:
        """Tum zaman dilimlerindeki tum seviyeleri tek listede doner
        (confluence/cakisma motoru bunu girdi olarak kullanir)."""
        zones: list[LevelDetail] = []
        seen: set[tuple[str, int]] = set()
        for tf_result in (self.daily, self.weekly, self.monthly):
            for lvl in (
                tf_result.support_1,
                tf_result.support_2,
                tf_result.main_support,
                tf_result.resistance_1,
                tf_result.resistance_2,
                tf_result.main_resistance,
            ):
                if lvl is not None:
                    key = (lvl.timeframe, int(round(lvl.mid * 100)))
                    if key not in seen:
                        seen.add(key)
                        zones.append(lvl)
        return zones


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Gunluk barlari haftalik/aylik mumlara cevirir.

    Henuz TAMAMLANMAMIS son periyodu (icinde bulunulan hafta/ay) her zaman
    atar: pandas resample bucket etiketi (haftanin Cuma'si / ayin son
    gunu) elimizdeki son gunluk bardan daha ileri bir tarihse, o periyot
    henuz kapanmamis demektir ve kesinlesmis analizde kullanilmaz - tipki
    tamamlanmamis gunluk mumun kullanilmamasi kurali gibi.
    """
    idx = pd.to_datetime(df["timestamp"])
    working = df.copy()
    working.index = idx
    working = working.sort_index()
    last_daily_date = working.index.max()
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = working.resample(rule).agg(agg).dropna(subset=["open", "high", "low", "close"])
    out = out.reset_index().rename(columns={"index": "timestamp"})
    if not out.empty and out["timestamp"].iloc[-1] > last_daily_date:
        out = out.iloc[:-1].reset_index(drop=True)
    return out


def _add_candidate(
    supports: list[tuple[float, str]],
    resistances: list[tuple[float, str]],
    value: Optional[float],
    source: str,
    current_price: float,
) -> None:
    """Geçerli bir fiyatı yalnızca bulunduğu tarafa ekler; fiyat uydurmaz."""
    if value is None or not np.isfinite(value) or value <= 0:
        return
    target = supports if value <= current_price else resistances
    target.append((float(value), source))


def _rolling_vwap(df: pd.DataFrame, lookback: int = 20) -> Optional[float]:
    window = df.tail(lookback)
    if window.empty or float(window["volume"].sum()) <= 0:
        return None
    typical = (window["high"] + window["low"] + window["close"]) / 3
    return float((typical * window["volume"]).sum() / window["volume"].sum())


def _volume_profile_nodes(df: pd.DataFrame, bins: int = 32) -> dict[str, list[float] | float | None]:
    """POC, HVN ve LVN benzeri doğrulanmış hacim düğümleri."""
    if df is None or len(df) < 10 or float(df["volume"].sum()) <= 0:
        return {"poc": None, "hvn": [], "lvn": []}
    low, high = float(df["low"].min()), float(df["high"].max())
    if high <= low:
        return {"poc": None, "hvn": [], "lvn": []}
    edges = np.linspace(low, high, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2
    volumes = np.zeros(bins)
    typical = ((df["high"] + df["low"] + df["close"]) / 3).to_numpy()
    for price, volume in zip(typical, df["volume"].to_numpy()):
        idx = int(np.clip(np.searchsorted(edges, price, side="right") - 1, 0, bins - 1))
        volumes[idx] += max(0.0, float(volume))
    positive = volumes[volumes > 0]
    if not len(positive):
        return {"poc": None, "hvn": [], "lvn": []}
    poc = float(centers[int(np.argmax(volumes))])
    high_cut = float(np.quantile(positive, 0.80))
    low_cut = float(np.quantile(positive, 0.20))
    hvn = [float(centers[i]) for i in np.argsort(volumes)[::-1] if volumes[i] >= high_cut][:3]
    lvn = [float(centers[i]) for i in np.argsort(volumes) if 0 < volumes[i] <= low_cut][:2]
    return {"poc": poc, "hvn": hvn, "lvn": lvn}


def _gap_levels(df: pd.DataFrame, atr_value: float) -> list[tuple[float, str]]:
    levels: list[tuple[float, str]] = []
    if len(df) < 2:
        return levels
    recent = df.tail(120).reset_index(drop=True)
    for i in range(1, len(recent)):
        prev = recent.iloc[i - 1]
        row = recent.iloc[i]
        if row["low"] > prev["high"] + atr_value * 0.10:
            levels.append(((float(row["low"]) + float(prev["high"])) / 2, "gap_yukari_bolgesi"))
        elif row["high"] < prev["low"] - atr_value * 0.10:
            levels.append(((float(row["high"]) + float(prev["low"])) / 2, "gap_asagi_bolgesi"))
    return levels[-8:]


def _fibonacci_levels(df: pd.DataFrame, current_price: float) -> list[tuple[float, str]]:
    window = df.tail(min(120, len(df)))
    if len(window) < 20:
        return []
    swing_low = float(window["low"].min())
    swing_high = float(window["high"].max())
    span = swing_high - swing_low
    if span <= 0:
        return []
    result = [
        (swing_low + span * ratio, f"fibonacci_{ratio:.3f}")
        for ratio in (0.236, 0.382, 0.5, 0.618, 0.786)
    ]
    # Extension yalnızca fiyat yapıya yakınsa eklenir; uzak ve anlamsız
    # seviyeler varsayılan listede sonradan mesafe filtresine takılır.
    if current_price >= swing_high * 0.98:
        result.extend(
            (swing_low + span * ratio, f"fibonacci_extension_{ratio:.3f}")
            for ratio in (1.272, 1.618)
        )
    return result


def _horizontal_clusters(df: pd.DataFrame, atr_value: float) -> list[tuple[float, str]]:
    """Uzun süreli yatay kapanış kümelerini fiyat histrogramından çıkarır."""
    window = df.tail(min(180, len(df)))
    if len(window) < 30 or atr_value <= 0:
        return []
    bin_width = max(atr_value * 0.5, float(window["close"].median()) * 0.004)
    keys = (window["close"] / bin_width).round().astype(int)
    counts = keys.value_counts()
    return [
        (float(key * bin_width), "yatay_fiyat_kumesi")
        for key, count in counts.head(3).items()
        if int(count) >= 4
    ]


def _strength_class(confidence: float, touches: int) -> str:
    if touches <= 0:
        return "Veri yetersiz"
    if confidence >= 82 and touches >= 3:
        return "Çok güçlü"
    if confidence >= 68 and touches >= 2:
        return "Güçlü"
    if confidence >= 48:
        return "Orta"
    return "Zayıf"


def _build_zone_detail(
    zone: LevelZone,
    atr_value: float,
    current_price: float,
    df: pd.DataFrame,
    timeframe: str,
    is_support: bool,
) -> LevelDetail:
    tolerance = max(atr_value * CLUSTER_ATR_FACTOR, current_price * CLUSTER_PRICE_FACTOR)
    average_turnover = float((df["close"] * df["volume"]).tail(30).mean()) if len(df) else 0.0
    # Düşük likiditede aşırı kesin çizgi üretme; bölgeyi ölçülü genişlet.
    if average_turnover < 5_000_000:
        tolerance *= 1.5
    low = zone.price - tolerance / 2
    high = zone.price + tolerance / 2

    # Temas / reddedilme / son test tarihi: fiyatin bu bolgeye ne zaman ve
    # kac kez yaklastigini, tepki verip vermedigini gecmis barlardan okur.
    touches = 0
    rejections = 0
    failed_tests = 0
    break_count = 0
    last_test_date: Optional[str] = None
    last_test_index: Optional[int] = None
    touch_volumes: list[float] = []
    avg_volume = float(df["volume"].tail(30).mean()) if len(df) else 0.0

    for idx, row in df.iterrows():
        probe_price = row["low"] if is_support else row["high"]
        if abs(probe_price - zone.price) <= tolerance:
            touches += 1
            touch_volumes.append(float(row["volume"]))
            last_test_index = int(idx)
            ts = row["timestamp"]
            last_test_date = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)
            if is_support and row["close"] > zone.price + tolerance * 0.25:
                rejections += 1
            elif not is_support and row["close"] < zone.price - tolerance * 0.25:
                rejections += 1
        close_beyond = row["close"] < low if is_support else row["close"] > high
        if close_beyond:
            break_count += 1
            if float(row["volume"]) < avg_volume * 1.1:
                failed_tests += 1

    volume_confirmed = bool(touch_volumes) and (sum(touch_volumes) / len(touch_volumes)) >= avg_volume * 0.9

    touches = max(touches, zone.votes)
    age_bars = (len(df) - 1 - last_test_index) if last_test_index is not None else len(df)
    distance_percent = abs(zone.price - current_price) / current_price * 100 if current_price else 0.0
    distinct_methods = len(set(zone.sources))
    timeframe_bonus = {TIMEFRAME_DAILY: 8.0, TIMEFRAME_WEEKLY: 14.0, TIMEFRAME_MONTHLY: 20.0}[timeframe]
    base = _zone_confidence(zone) * 0.32
    touch_bonus = min(20.0, touches * 3.5)
    reaction_bonus = min(16.0, rejections * 4.0)
    method_bonus = min(14.0, distinct_methods * 2.5)
    volume_bonus = 8.0 if volume_confirmed else 0.0
    recency_bonus = max(0.0, 10.0 - age_bars * (0.18 if timeframe == TIMEFRAME_DAILY else 0.35))
    age_penalty = min(15.0, max(0, age_bars - 60) * 0.12)
    failure_penalty = min(20.0, failed_tests * 4.0)
    distance_penalty = min(15.0, max(0.0, distance_percent - 12.0) * 0.7)
    confidence = max(
        0.0,
        min(
            97.0,
            base + timeframe_bonus + touch_bonus + reaction_bonus + method_bonus + volume_bonus
            + recency_bonus - age_penalty - failure_penalty - distance_penalty,
        ),
    )
    # Tek temaslı seviye yüksek güven alamaz.
    if touches <= 1:
        confidence = min(confidence, 49.0)
    confidence = round(confidence, 1)

    recent = df.tail(3)
    strongly_broken = bool(
        ((recent["close"] < low).sum() >= 2 if is_support else (recent["close"] > high).sum() >= 2)
        and float(recent["volume"].mean()) >= avg_volume * 1.1
    )
    role_reversal = any("simdi_destek" in src or "simdi_direnc" in src for src in zone.sources)
    active = not strongly_broken or role_reversal
    invalidation = (
        f"{low:.2f} altında hacim ve ATR doğrulamalı kapanış"
        if is_support
        else f"{high:.2f} üzerinde hacim ve ATR doğrulamalı kapanış"
    )

    return LevelDetail(
        low=round2(low),
        high=round2(high),
        mid=round2(zone.price),
        confidence=confidence,
        touches=touches,
        rejections=rejections,
        last_test_date=last_test_date,
        sources=list(dict.fromkeys(zone.sources)),
        volume_confirmed=volume_confirmed,
        timeframe=timeframe,
        successful_reactions=rejections,
        failed_tests=failed_tests,
        break_count=break_count,
        age_bars=age_bars,
        distance_percent=round(distance_percent, 2),
        strength_class=_strength_class(confidence, touches),
        active=active,
        invalidation_condition=invalidation,
        role_reversal=role_reversal,
    )


def _compute_single_timeframe(
    df: pd.DataFrame,
    current_price: float,
    timeframe: str,
    max_secondary_levels: bool,
) -> TimeframeLevelResult:
    min_bars = MIN_BARS[timeframe]
    if df is None or len(df) < min_bars:
        return TimeframeLevelResult(timeframe=timeframe, reliable=False, note=RELIABLE_NOTE)

    df = df.sort_values("timestamp").reset_index(drop=True)
    atr_series = compute_atr(df, period=min(14, max(2, len(df) - 1)))
    atr_value = float(atr_series.iloc[-1]) if len(atr_series.dropna()) else float(
        (df["high"] - df["low"]).tail(14).mean()
    )
    if not atr_value or atr_value <= 0:
        return TimeframeLevelResult(timeframe=timeframe, reliable=False, note=RELIABLE_NOTE)

    swing_window = 2 if timeframe != TIMEFRAME_DAILY else 3
    swing_highs, swing_lows = _find_swing_points(df.tail(120), window=swing_window)

    support_candidates: list[tuple[float, str]] = []
    resistance_candidates: list[tuple[float, str]] = []

    lookbacks = [20, 60, 120, 250]
    for lb in lookbacks:
        window = df.tail(lb)
        if len(window) < min(lb, min_bars):
            continue
        support_candidates.append((float(window["low"].min()), f"son_{lb}_dip"))
        resistance_candidates.append((float(window["high"].max()), f"son_{lb}_tepe"))

    for sw_low in [lo for lo in swing_lows if lo < current_price]:
        support_candidates.append((sw_low, "swing_low_fractal_pivot"))
    for sw_high in [hi for hi in swing_highs if hi > current_price]:
        resistance_candidates.append((sw_high, "swing_high_fractal_pivot"))

    pivots = _classic_pivot_points(df)
    if pivots:
        for key in ("s1", "s2"):
            if pivots[key] < current_price:
                support_candidates.append((pivots[key], f"pivot_{key}"))
        for key in ("r1", "r2"):
            if pivots[key] > current_price:
                resistance_candidates.append((pivots[key], f"pivot_{key}"))

    if len(df) >= 20:
        ema20 = float(compute_ema(df["close"], min(20, len(df) - 1)).iloc[-1])
        ema50 = float(compute_ema(df["close"], min(50, len(df) - 1)).iloc[-1]) if len(df) >= 25 else None
        ema100 = float(compute_ema(df["close"], 100).iloc[-1]) if len(df) >= 100 else None
        ema200 = float(compute_ema(df["close"], 200).iloc[-1]) if len(df) >= 200 else None
        for label, value in (("ema20", ema20), ("ema50", ema50), ("ema100", ema100), ("ema200", ema200)):
            _add_candidate(support_candidates, resistance_candidates, value, label, current_price)

        middle = df["close"].rolling(20).mean()
        std = df["close"].rolling(20).std(ddof=0)
        if pd.notna(middle.iloc[-1]) and pd.notna(std.iloc[-1]):
            _add_candidate(support_candidates, resistance_candidates, float(middle.iloc[-1]), "bollinger_orta", current_price)
            _add_candidate(support_candidates, resistance_candidates, float(middle.iloc[-1] + 2 * std.iloc[-1]), "bollinger_ust", current_price)
            _add_candidate(support_candidates, resistance_candidates, float(middle.iloc[-1] - 2 * std.iloc[-1]), "bollinger_alt", current_price)

    anchored_vwap = _anchored_vwap(df, lookback=min(60, len(df)))
    _add_candidate(support_candidates, resistance_candidates, anchored_vwap, "ankor_vwap", current_price)
    _add_candidate(support_candidates, resistance_candidates, _rolling_vwap(df, 20), "rolling_vwap_20", current_price)

    vol_below, vol_above = _volume_profile_zones(df.tail(min(120, len(df))), current_price)
    if vol_below is not None:
        support_candidates.append((vol_below, "hacimli_bolge"))
    if vol_above is not None:
        resistance_candidates.append((vol_above, "hacimli_bolge"))

    profile = _volume_profile_nodes(df.tail(min(180, len(df))))
    _add_candidate(support_candidates, resistance_candidates, profile["poc"], "volume_profile_poc", current_price)
    for value in profile["hvn"]:
        _add_candidate(support_candidates, resistance_candidates, value, "volume_profile_hvn", current_price)
    for value in profile["lvn"]:
        _add_candidate(support_candidates, resistance_candidates, value, "volume_profile_lvn", current_price)

    # Önceki tamamlanmış periyot yüksek/düşüğü.
    if len(df) >= 2:
        previous = df.iloc[-2]
        _add_candidate(support_candidates, resistance_candidates, float(previous["low"]), "onceki_periyot_dusuk", current_price)
        _add_candidate(support_candidates, resistance_candidates, float(previous["high"]), "onceki_periyot_yuksek", current_price)

    for value, source in _gap_levels(df, atr_value):
        _add_candidate(support_candidates, resistance_candidates, value, source, current_price)
    for value, source in _fibonacci_levels(df, current_price):
        _add_candidate(support_candidates, resistance_candidates, value, source, current_price)
    for value, source in _horizontal_clusters(df, atr_value):
        _add_candidate(support_candidates, resistance_candidates, value, source, current_price)

    # Büyük mum açılış/kapanışları ve hacimli fitil reddedilmeleri.
    avg_volume = float(df["volume"].tail(30).mean())
    for _, row in df.tail(90).iterrows():
        body = abs(float(row["close"]) - float(row["open"]))
        if body >= atr_value * 1.25:
            _add_candidate(support_candidates, resistance_candidates, float(row["open"]), "buyuk_mum_acilis", current_price)
            _add_candidate(support_candidates, resistance_candidates, float(row["close"]), "buyuk_mum_kapanis", current_price)
        upper_wick = float(row["high"]) - max(float(row["open"]), float(row["close"]))
        lower_wick = min(float(row["open"]), float(row["close"])) - float(row["low"])
        if float(row["volume"]) >= avg_volume * 1.25:
            if lower_wick >= max(body, atr_value * 0.35):
                _add_candidate(support_candidates, resistance_candidates, float(row["low"]), "hacimli_fitil_reddi", current_price)
            if upper_wick >= max(body, atr_value * 0.35):
                _add_candidate(support_candidates, resistance_candidates, float(row["high"]), "hacimli_fitil_reddi", current_price)

    if len(df) >= 20:
        history = df.iloc[:-5].tail(120)
        recent = df.tail(5)
        if not history.empty:
            prior_high = float(history["high"].max())
            prior_low = float(history["low"].min())
            if (
                float(recent["close"].max()) > prior_high + atr_value * 0.20
                and float(recent["low"].min()) <= prior_high + atr_value * 0.35
                and current_price > prior_high
            ):
                support_candidates.append((prior_high, "kirilan_direnc_simdi_destek_retest"))
            if (
                float(recent["close"].min()) < prior_low - atr_value * 0.20
                and float(recent["high"].max()) >= prior_low - atr_value * 0.35
                and current_price < prior_low
            ):
                resistance_candidates.append((prior_low, "kirilan_destek_simdi_direnc_retest"))

    max_distance = 0.50 if timeframe == TIMEFRAME_MONTHLY else 0.35
    support_candidates = [c for c in support_candidates if 0 < c[0] <= current_price and (current_price - c[0]) / current_price <= max_distance]
    resistance_candidates = [c for c in resistance_candidates if c[0] > current_price and (c[0] - current_price) / current_price <= max_distance]

    support_zones = sorted(
        _cluster_levels(support_candidates, atr_value, current_price), key=lambda z: -z.price
    )
    resistance_zones = sorted(
        _cluster_levels(resistance_candidates, atr_value, current_price), key=lambda z: z.price
    )

    reliable = len(support_zones) >= 1 or len(resistance_zones) >= 1
    if not reliable:
        return TimeframeLevelResult(timeframe=timeframe, reliable=False, note=RELIABLE_NOTE)

    def detail(zone: Optional[LevelZone], is_support: bool) -> Optional[LevelDetail]:
        if zone is None:
            return None
        return _build_zone_detail(zone, atr_value, current_price, df, timeframe, is_support)

    support_details = [built for zone in support_zones if (built := detail(zone, True)) is not None]
    resistance_details = [built for zone in resistance_zones if (built := detail(zone, False)) is not None]
    active_supports = [level for level in support_details if level.active]
    active_resistances = [level for level in resistance_details if level.active]

    for index, level in enumerate(active_supports[:-1]):
        level.next_zone_low = active_supports[index + 1].low
        level.next_zone_high = active_supports[index + 1].high
    for index, level in enumerate(active_resistances[:-1]):
        level.next_zone_low = active_resistances[index + 1].low
        level.next_zone_high = active_resistances[index + 1].high

    support_1 = active_supports[0] if active_supports else None
    support_2 = active_supports[1] if max_secondary_levels and len(active_supports) >= 2 else None
    resistance_1 = active_resistances[0] if active_resistances else None
    resistance_2 = active_resistances[1] if max_secondary_levels and len(active_resistances) >= 2 else None
    main_support = max(active_supports, key=lambda level: level.confidence, default=None)
    main_resistance = max(active_resistances, key=lambda level: level.confidence, default=None)

    return TimeframeLevelResult(
        timeframe=timeframe,
        reliable=bool(active_supports or active_resistances),
        note="" if (active_supports or active_resistances) else RELIABLE_NOTE,
        support_1=support_1,
        support_2=support_2,
        main_support=main_support,
        resistance_1=resistance_1,
        resistance_2=resistance_2,
        main_resistance=main_resistance,
    )


def compute_timeframe_levels(df_daily: pd.DataFrame, current_price: float) -> MultiTimeframeLevelsResult:
    """Gunluk barlardan gunluk/haftalik/aylik destek-direnc seviyelerini hesaplar.

    df_daily: en az `timestamp, open, high, low, close, volume` kolonlarini
    iceren, TAMAMLANMIS (kesinlesmis) gunluk mumlardan olusan DataFrame.
    """
    if df_daily is None or df_daily.empty:
        return MultiTimeframeLevelsResult(
            daily=TimeframeLevelResult(timeframe=TIMEFRAME_DAILY, reliable=False, note=RELIABLE_NOTE),
            weekly=TimeframeLevelResult(timeframe=TIMEFRAME_WEEKLY, reliable=False, note=RELIABLE_NOTE),
            monthly=TimeframeLevelResult(timeframe=TIMEFRAME_MONTHLY, reliable=False, note=RELIABLE_NOTE),
        )

    df_daily = df_daily.sort_values("timestamp").reset_index(drop=True)
    daily_result = _compute_single_timeframe(df_daily, current_price, TIMEFRAME_DAILY, max_secondary_levels=True)

    weekly_df = _resample(df_daily, "W-FRI")
    weekly_result = _compute_single_timeframe(
        weekly_df, current_price, TIMEFRAME_WEEKLY, max_secondary_levels=True
    )

    monthly_df = _resample(df_daily, "ME")
    # Aylik seviyelerde spesifikasyon geregi yalnizca "Destek/Direnc 1" ve
    # "Ana Destek/Direnc" gosterilir (Destek/Direnc 2 yok).
    monthly_result = _compute_single_timeframe(
        monthly_df, current_price, TIMEFRAME_MONTHLY, max_secondary_levels=False
    )

    return MultiTimeframeLevelsResult(daily=daily_result, weekly=weekly_result, monthly=monthly_result)
