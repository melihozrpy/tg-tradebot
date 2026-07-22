from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

MIN_BARS_FOR_LEVELS = 30
SWING_WINDOW = 3  # bir barin swing high/low sayilmasi icin her iki yanindaki bar sayisi
CLUSTER_ATR_FACTOR = 0.5  # ayni bolge sayilmak icin ATR'nin bu kati kadar yakinlik
CLUSTER_PRICE_FACTOR = 0.008  # veya fiyatin bu yuzdesi kadar yakinlik (hangisi buyukse)
VOLUME_PROFILE_BINS = 24


def round2(value: Optional[float]) -> Optional[float]:
    """Fiyatlari her zaman en fazla 2 ondalik basamakla gosterir.

    140.2118261812036 gibi uzun ondalikli degerler asla disariya cikmaz.
    """
    if value is None:
        return None
    return round(float(value), 2)


@dataclass
class LevelZone:
    price: float
    votes: int
    sources: list[str] = field(default_factory=list)


@dataclass
class SupportResistanceResult:
    support_1: Optional[float]
    support_2: Optional[float]
    main_support: Optional[float]
    resistance_1: Optional[float]
    resistance_2: Optional[float]
    main_resistance: Optional[float]
    support_reliable: bool
    resistance_reliable: bool
    support_note: str
    resistance_note: str
    support_broken_with_volume: bool
    price_below_main_resistance: bool
    price_reacting_off_support: bool
    # V3.1 (bolum 7): her seviye icin guven skoru / temas sayisi / kaynak listesi.
    support_1_confidence: Optional[float] = None
    support_1_touches: int = 0
    support_1_sources: list[str] = field(default_factory=list)
    resistance_1_confidence: Optional[float] = None
    resistance_1_touches: int = 0
    resistance_1_sources: list[str] = field(default_factory=list)
    main_support_confidence: Optional[float] = None
    main_resistance_confidence: Optional[float] = None
    support_role_reversal: bool = False  # kirilan direnc simdi destek mi
    resistance_role_reversal: bool = False  # kirilan destek simdi direnc mi


def _find_swing_points(df: pd.DataFrame, window: int = SWING_WINDOW) -> tuple[list[float], list[float]]:
    """Basit lokal ekstrem (swing high/low) tespiti. Look-ahead bias yok:
    yalnizca gecmis tam veri seti uzerinde (backtest disi, canli analizde tum
    gecmis kapanmis mumlar) calisir; ileriye donuk hicbir bilgi kullanilmaz.
    """
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    swing_highs: list[float] = []
    swing_lows: list[float] = []

    for i in range(window, n - window):
        local_high_slice = highs[i - window: i + window + 1]
        local_low_slice = lows[i - window: i + window + 1]
        if highs[i] == local_high_slice.max():
            swing_highs.append(float(highs[i]))
        if lows[i] == local_low_slice.min():
            swing_lows.append(float(lows[i]))

    return swing_highs, swing_lows


def _volume_profile_zones(df: pd.DataFrame, current_price: float, bins: int = VOLUME_PROFILE_BINS) -> tuple[Optional[float], Optional[float]]:
    """Hacim agirlikli fiyat bolgelerinden fiyatin altindaki ve ustundeki
    en yogun (en cok islem gormus) bolgeyi doner.
    """
    if df.empty:
        return None, None

    price_min = float(df["low"].min())
    price_max = float(df["high"].max())
    if price_max <= price_min:
        return None, None

    bin_edges = np.linspace(price_min, price_max, bins + 1)
    volume_per_bin = np.zeros(bins)

    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    for tp, vol in zip(typical_price.values, df["volume"].values):
        idx = np.searchsorted(bin_edges, tp, side="right") - 1
        idx = min(max(idx, 0), bins - 1)
        volume_per_bin[idx] += vol

    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2

    below_mask = bin_centers < current_price
    above_mask = bin_centers > current_price

    best_below = None
    if below_mask.any() and volume_per_bin[below_mask].sum() > 0:
        idx_below = np.argmax(np.where(below_mask, volume_per_bin, -1))
        best_below = float(bin_centers[idx_below])

    best_above = None
    if above_mask.any() and volume_per_bin[above_mask].sum() > 0:
        idx_above = np.argmax(np.where(above_mask, volume_per_bin, -1))
        best_above = float(bin_centers[idx_above])

    return best_below, best_above


def _classic_pivot_points(df: pd.DataFrame) -> dict:
    if len(df) < 2:
        return {}
    last = df.iloc[-1]
    pivot = (last["high"] + last["low"] + last["close"]) / 3
    return {
        "pivot": float(pivot),
        "s1": float(2 * pivot - last["high"]),
        "s2": float(pivot - (last["high"] - last["low"])),
        "r1": float(2 * pivot - last["low"]),
        "r2": float(pivot + (last["high"] - last["low"])),
    }


def _cluster_levels(candidates: list[tuple[float, str]], atr: float, current_price: float) -> list[LevelZone]:
    """Birbirine yakin seviyeleri tek bir bolgede birlestirir."""
    candidates = [(float(price), source) for price, source in candidates if np.isfinite(price) and price > 0]
    if not candidates:
        return []

    tolerance = max(atr * CLUSTER_ATR_FACTOR, current_price * CLUSTER_PRICE_FACTOR)
    sorted_candidates = sorted(candidates, key=lambda c: c[0])

    zones: list[LevelZone] = []
    for price, source in sorted_candidates:
        if zones and abs(price - zones[-1].price) <= tolerance:
            zone = zones[-1]
            total_votes = zone.votes + 1
            zone.price = (zone.price * zone.votes + price) / total_votes
            zone.votes = total_votes
            zone.sources.append(source)
        else:
            zones.append(LevelZone(price=price, votes=1, sources=[source]))

    # BIST fiyat adımının en küçük ortak gösterimi olan kuruşa kanonikleştirme,
    # aynı veri veya çok küçük fiyat farklarında gereksiz seviye oynamasını azaltır.
    for zone in zones:
        zone.price = round(zone.price, 2)
    return zones


def _zone_confidence(zone: LevelZone) -> float:
    """Bir bolgenin 0-100 guven skoru: temas sayisi + kaynak cesitliligi.

    Cok sayida BAGIMSIZ yontemin (swing, pivot, EMA, hacimli bolge, vb.)
    ayni fiyat civarinda birlesmesi guveni artirir; tek yontemden gelen
    tek bir deger dusuk guvenle isaretlenir.
    """
    distinct_sources = len(set(zone.sources))
    base = min(60.0, zone.votes * 18.0)
    diversity_bonus = min(40.0, distinct_sources * 10.0)
    return round(min(100.0, base + diversity_bonus), 1)


def _anchored_vwap(df: pd.DataFrame, lookback: int = 60) -> Optional[float]:
    """Son N bar icin hacim agirlikli ortalama fiyat (basit ankor VWAP)."""
    window = df.tail(lookback)
    if window.empty or window["volume"].sum() <= 0:
        return None
    typical_price = (window["high"] + window["low"] + window["close"]) / 3
    return float((typical_price * window["volume"]).sum() / window["volume"].sum())


def compute_support_resistance(
    df: pd.DataFrame,
    current_price: float,
    ema20: float,
    ema50: float,
    atr: float,
    ema100: Optional[float] = None,
    ema200: Optional[float] = None,
) -> SupportResistanceResult:
    """Coklu teknik onaylama ile destek/direnc seviyelerini hesaplar.

    Yeterli/guvenilir seviye bulunamazsa deger uydurmaz; support_reliable /
    resistance_reliable alanlari False doner ve ilgili not alaninda
    "Guvenilir seviye hesaplanamadi." mesaji yer alir.
    """
    if len(df) < MIN_BARS_FOR_LEVELS or atr <= 0:
        note = "Guvenilir seviye hesaplanamadi."
        return SupportResistanceResult(
            support_1=None, support_2=None, main_support=None,
            resistance_1=None, resistance_2=None, main_resistance=None,
            support_reliable=False, resistance_reliable=False,
            support_note=note, resistance_note=note,
            support_broken_with_volume=False,
            price_below_main_resistance=True,
            price_reacting_off_support=False,
        )

    df = df.sort_values("timestamp").reset_index(drop=True)
    swing_highs, swing_lows = _find_swing_points(df.tail(120))

    last20 = df.tail(20)
    last60 = df.tail(60) if len(df) >= 60 else df

    support_candidates: list[tuple[float, str]] = []
    resistance_candidates: list[tuple[float, str]] = []

    support_candidates.append((float(last20["low"].min()), "son_20_gun_dip"))
    support_candidates.append((float(last60["low"].min()), "son_60_gun_dip"))
    resistance_candidates.append((float(last20["high"].max()), "son_20_gun_tepe"))
    resistance_candidates.append((float(last60["high"].max()), "son_60_gun_tepe"))

    for sw_low in [lo for lo in swing_lows if lo < current_price]:
        support_candidates.append((sw_low, "swing_low"))
    for sw_high in [hi for hi in swing_highs if hi > current_price]:
        resistance_candidates.append((sw_high, "swing_high"))

    pivots = _classic_pivot_points(df)
    if pivots:
        for key in ("s1", "s2"):
            if pivots[key] < current_price:
                support_candidates.append((pivots[key], f"pivot_{key}"))
        for key in ("r1", "r2"):
            if pivots[key] > current_price:
                resistance_candidates.append((pivots[key], f"pivot_{key}"))

    if ema20 < current_price:
        support_candidates.append((ema20, "ema20"))
    else:
        resistance_candidates.append((ema20, "ema20"))
    if ema50 < current_price:
        support_candidates.append((ema50, "ema50"))
    else:
        resistance_candidates.append((ema50, "ema50"))
    if ema100 is not None:
        if ema100 < current_price:
            support_candidates.append((ema100, "ema100"))
        else:
            resistance_candidates.append((ema100, "ema100"))
    if ema200 is not None:
        if ema200 < current_price:
            support_candidates.append((ema200, "ema200"))
        else:
            resistance_candidates.append((ema200, "ema200"))

    anchored_vwap = _anchored_vwap(df, lookback=60)
    if anchored_vwap is not None:
        if anchored_vwap < current_price:
            support_candidates.append((anchored_vwap, "ankor_vwap"))
        else:
            resistance_candidates.append((anchored_vwap, "ankor_vwap"))

    rolling_vwap = _anchored_vwap(df, lookback=20)
    if rolling_vwap is not None:
        (support_candidates if rolling_vwap < current_price else resistance_candidates).append(
            (rolling_vwap, "rolling_vwap_20")
        )

    if len(df) >= 20:
        mid = df["close"].rolling(20).mean().iloc[-1]
        std = df["close"].rolling(20).std(ddof=0).iloc[-1]
        for label, value in (
            ("bollinger_orta", mid),
            ("bollinger_ust", mid + 2 * std),
            ("bollinger_alt", mid - 2 * std),
        ):
            if pd.notna(value):
                (support_candidates if value < current_price else resistance_candidates).append((float(value), label))

    if len(df) >= 2:
        previous = df.iloc[-2]
        for label, value in (("onceki_gun_dusuk", previous["low"]), ("onceki_gun_yuksek", previous["high"])):
            (support_candidates if value < current_price else resistance_candidates).append((float(value), label))

    fib_window = df.tail(min(120, len(df)))
    fib_low, fib_high = float(fib_window["low"].min()), float(fib_window["high"].max())
    if fib_high > fib_low:
        for ratio in (0.382, 0.5, 0.618):
            value = fib_low + (fib_high - fib_low) * ratio
            (support_candidates if value < current_price else resistance_candidates).append((value, f"fibonacci_{ratio}"))

    # Kirilan direncin destege / kirilan destegin dirence donusmesi: onceki
    # 60 barin en yuksek/en dusuk seviyeleri fiyatin artik diger tarafinda ise
    # (rol degisimi) bu da bir kaynak olarak eklenir.
    support_role_reversal = False
    resistance_role_reversal = False
    if len(df) >= 20:
        history = df.iloc[:-5].tail(120)
        recent = df.tail(5)
        if not history.empty:
            prior_high = float(history["high"].max())
            prior_low = float(history["low"].min())
            if (
                current_price > prior_high
                and float(recent["close"].max()) > prior_high + atr * 0.2
                and float(recent["low"].min()) <= prior_high + atr * 0.35
            ):
                support_candidates.append((prior_high, "kirilan_direnc_simdi_destek_retest"))
                support_role_reversal = True
            if (
                current_price < prior_low
                and float(recent["close"].min()) < prior_low - atr * 0.2
                and float(recent["high"].max()) >= prior_low - atr * 0.35
            ):
                resistance_candidates.append((prior_low, "kirilan_destek_simdi_direnc_retest"))
                resistance_role_reversal = True

    vol_below, vol_above = _volume_profile_zones(df.tail(120), current_price)
    if vol_below is not None:
        support_candidates.append((vol_below, "hacimli_bolge"))
    if vol_above is not None:
        resistance_candidates.append((vol_above, "hacimli_bolge"))

    support_zones = sorted(
        _cluster_levels(support_candidates, atr, current_price), key=lambda z: -z.price
    )  # fiyata en yakindan uzaga (yuksekten dusuge)
    resistance_zones = sorted(
        _cluster_levels(resistance_candidates, atr, current_price), key=lambda z: z.price
    )  # fiyata en yakindan uzaga (dusukten yukseye)

    support_reliable = len(support_zones) >= 2
    resistance_reliable = len(resistance_zones) >= 2

    support_1 = round2(support_zones[0].price) if len(support_zones) >= 1 else None
    support_2 = round2(support_zones[1].price) if len(support_zones) >= 2 else None
    resistance_1 = round2(resistance_zones[0].price) if len(resistance_zones) >= 1 else None
    resistance_2 = round2(resistance_zones[1].price) if len(resistance_zones) >= 2 else None

    main_support = None
    if support_zones:
        strongest = max(support_zones, key=lambda z: z.votes)
        main_support = round2(strongest.price)

    main_resistance = None
    if resistance_zones:
        strongest = max(resistance_zones, key=lambda z: z.votes)
        main_resistance = round2(strongest.price)

    support_note = "" if support_reliable else "Guvenilir seviye hesaplanamadi."
    resistance_note = "" if resistance_reliable else "Guvenilir seviye hesaplanamadi."

    # Destek hacimli kirilmis mi? Son 3 barda kapanis en yakin support_1'in
    # altina duserken hacim ortalamanin uzerindeyse "kirildi" kabul edilir.
    support_broken_with_volume = False
    if support_1 is not None and len(df) >= 4:
        recent = df.tail(3)
        avg_vol = df["volume"].tail(20).mean()
        broke = (recent["close"] < support_1).any()
        high_vol = (recent["volume"] > avg_vol * 1.2).any()
        support_broken_with_volume = bool(broke and high_vol)

    price_below_main_resistance = main_resistance is not None and current_price < main_resistance
    price_reacting_off_support = (
        support_1 is not None
        and not support_broken_with_volume
        and (current_price - support_1) / current_price < 0.02
        and current_price >= support_1
    )

    support_1_confidence = _zone_confidence(support_zones[0]) if support_zones else None
    support_1_touches = support_zones[0].votes if support_zones else 0
    support_1_sources = list(support_zones[0].sources) if support_zones else []
    resistance_1_confidence = _zone_confidence(resistance_zones[0]) if resistance_zones else None
    resistance_1_touches = resistance_zones[0].votes if resistance_zones else 0
    resistance_1_sources = list(resistance_zones[0].sources) if resistance_zones else []

    main_support_confidence = None
    if support_zones:
        strongest = max(support_zones, key=lambda z: z.votes)
        main_support_confidence = _zone_confidence(strongest)

    main_resistance_confidence = None
    if resistance_zones:
        strongest = max(resistance_zones, key=lambda z: z.votes)
        main_resistance_confidence = _zone_confidence(strongest)

    return SupportResistanceResult(
        support_1=support_1,
        support_2=support_2,
        main_support=main_support,
        resistance_1=resistance_1,
        resistance_2=resistance_2,
        main_resistance=main_resistance,
        support_reliable=support_reliable,
        resistance_reliable=resistance_reliable,
        support_note=support_note,
        resistance_note=resistance_note,
        support_broken_with_volume=support_broken_with_volume,
        price_below_main_resistance=price_below_main_resistance,
        price_reacting_off_support=price_reacting_off_support,
        support_1_confidence=support_1_confidence,
        support_1_touches=support_1_touches,
        support_1_sources=support_1_sources,
        resistance_1_confidence=resistance_1_confidence,
        resistance_1_touches=resistance_1_touches,
        resistance_1_sources=resistance_1_sources,
        main_support_confidence=main_support_confidence,
        main_resistance_confidence=main_resistance_confidence,
        support_role_reversal=support_role_reversal,
        resistance_role_reversal=resistance_role_reversal,
    )
