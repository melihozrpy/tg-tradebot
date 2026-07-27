from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.analysis.data_quality import DataQualityEngine
from app.analysis.indicator_engine import atr, ema, macd, rsi
from app.analysis.multi_timeframe_engine import resample_completed_4h
from app.analysis.smart_money_engine import detect_smart_money


MIN_BARS_PER_TIMEFRAME = 60
OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


@dataclass(frozen=True)
class TimeframeInsight:
    label: str
    direction: str
    close: float
    atr_percent: float
    rsi: float
    macd_text: str
    fvg_text: str
    order_block_text: str
    structure_text: str


def _zone_text(zones, empty_text: str) -> str:
    if not zones:
        return empty_text
    zone = zones[-1]
    side = "yükseliş" if zone.direction == "bullish" else "düşüş"
    return f"{side} {zone.low:.2f}–{zone.high:.2f} TL"


def _inspect(df: pd.DataFrame, label: str) -> TimeframeInsight:
    data = df.sort_values("timestamp").dropna(subset=["open", "high", "low", "close"]).tail(300)
    if len(data) < MIN_BARS_PER_TIMEFRAME:
        raise ValueError(f"{label} için yeterli mum yok: {len(data)} < {MIN_BARS_PER_TIMEFRAME}")
    close = data["close"].astype(float)
    last = float(close.iloc[-1])
    ema20, ema50 = float(ema(close, 20).iloc[-1]), float(ema(close, 50).iloc[-1])
    direction = "YUKARI" if last > ema20 > ema50 else "AŞAĞI" if last < ema20 < ema50 else "KARIŞIK"
    atr_percent = float(atr(data, 14).iloc[-1]) / last * 100 if last else 0.0
    rsi_value = float(rsi(close, 14).iloc[-1])
    _, _, histogram = macd(close)
    hist, previous = float(histogram.iloc[-1]), float(histogram.iloc[-2])
    if hist > 0:
        macd_text = "pozitif momentum" + (" güçleniyor" if hist > previous else " yavaşlıyor")
    else:
        macd_text = "negatif momentum" + (" zayıflıyor" if hist > previous else " güçleniyor")
    smart = detect_smart_money(data)
    structure = smart.structure[-1] if smart.structure else None
    structure_text = (f"{structure.kind} • {'yukarı' if structure.direction == 'bullish' else 'aşağı'} yapı"
                      if structure else "yeni BOS/MSS yok")
    return TimeframeInsight(label, direction, last, atr_percent, rsi_value, macd_text,
                            _zone_text(smart.fvg, "güncel FVG yok"),
                            _zone_text(smart.order_blocks, "güncel order block yok"), structure_text)


def build_multi_timeframe_package(provider, symbol: str, *, timezone_name="Europe/Istanbul", now=None):
    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    frames = {
        label: pd.DataFrame(columns=OHLCV_COLUMNS)
        for label in ("5 dk", "15 dk", "1 saat", "4 saat")
    }
    errors = []
    quality = DataQualityEngine(timezone_name=timezone_name)
    for label, interval, lookback in (("5 dk", "5m", 8), ("15 dk", "15m", 25), ("1 saat", "1h", 120)):
        try:
            raw = provider.get_ohlcv(symbol, interval, end - timedelta(days=lookback), end)
            completed = quality.completed_candles(raw, interval, now=end)
            frames[label] = completed if completed is not None else frames[label]
        except Exception as exc:  # noqa: BLE001 - bir TF diğerlerini engellemez
            errors.append(f"{label}: veri alınamadı ({exc})")

    if not frames["1 saat"].empty:
        try:
            frames["4 saat"] = resample_completed_4h(
                frames["1 saat"], now=end, timezone_name=timezone_name,
            )
        except Exception as exc:  # noqa: BLE001 - diğer paneller yine kullanılabilir
            errors.append(f"4 saat: veri üretilemedi ({exc})")
    else:
        errors.append("4 saat: 1 saatlik kaynak veri yok")

    insights = []
    for label in ("5 dk", "15 dk", "1 saat", "4 saat"):
        if frames[label].empty:
            continue
        try:
            insights.append(_inspect(frames[label], label))
        except Exception as exc:  # noqa: BLE001 - bir TF'nin indikatör hatası diğerlerini engellemez
            errors.append(f"{label}: {exc}")
    return (tuple(insights), tuple(errors)), frames


def build_multi_timeframe_explanation(provider, symbol: str, *, timezone_name="Europe/Istanbul", now=None):
    result, _ = build_multi_timeframe_package(
        provider, symbol, timezone_name=timezone_name, now=now,
    )
    return result


def format_multi_timeframe_explanation(symbol: str, result) -> str:
    insights, errors = result
    lines = [f"⏱️ {symbol.upper()} • 5 DK–4 SAAT OKUMASI", "━━━━━━━━━━━━━━━━━━"]
    icons = {"YUKARI": "🟢", "AŞAĞI": "🔴", "KARIŞIK": "🟡"}
    for item in insights:
        volatility = "yüksek" if item.atr_percent >= 2 else "orta" if item.atr_percent >= 1 else "sakin"
        lines += [f"\n{icons[item.direction]} {item.label} • {item.direction} • {item.close:.2f} TL",
                  f"MACD: {item.macd_text}. RSI: {item.rsi:.0f}.",
                  f"ATR: %{item.atr_percent:.2f}; fiyat hareketi {volatility}.",
                  f"FVG: {item.fvg_text}", f"Order Block: {item.order_block_text}",
                  f"Yapı: {item.structure_text}"]
    if errors:
        lines.append("\n⚠️ Eksik zamanlar: " + " | ".join(errors))
    lines += ["\nℹ️ FVG dengesiz fiyat alanı, Order Block güçlü tepki alanı; BOS trend devamını, MSS yön değişimi ihtimalini anlatır.",
              "Teknik senaryodur; yatırım tavsiyesi değildir."]
    return "\n".join(lines)
