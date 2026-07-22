from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from app.analysis.support_resistance_engine import SupportResistanceResult
from app.analysis.corporate_actions_engine import (
    CorporateActionEvent,
    classify_price_gap,
    normalize_corporate_actions,
)

MIN_BARS_FOR_ANOMALY = 22

ANOMALY_VOLUME_SPIKE = "hacim_patlamasi"
ANOMALY_GAP_UP = "gap_yukari"
ANOMALY_GAP_DOWN = "gap_asagi"
ANOMALY_SUPPORT_BREAK = "destek_kirilimi"
ANOMALY_RESISTANCE_BREAK = "direnc_kirilimi"
ANOMALY_VOLATILITY_SPIKE = "volatilite_patlamasi"

SEVERITY_LOW = "dusuk"
SEVERITY_MEDIUM = "orta"
SEVERITY_HIGH = "yuksek"

ANOMALY_LABELS_TR = {
    ANOMALY_VOLUME_SPIKE: "Hacim patlaması",
    ANOMALY_GAP_UP: "Yukarı gap",
    ANOMALY_GAP_DOWN: "Aşağı gap",
    ANOMALY_SUPPORT_BREAK: "Destek kırılımı",
    ANOMALY_RESISTANCE_BREAK: "Direnç kırılımı",
    ANOMALY_VOLATILITY_SPIKE: "Volatilite patlaması",
}

# Esikler (spesifikasyon bolum 8): sabit, aciklanabilir, hicbir ML/kara kutu yok.
VOLUME_SPIKE_RELATIVE_VOLUME = 2.5
VOLUME_SPIKE_HIGH_RELATIVE_VOLUME = 4.0
GAP_THRESHOLD_PERCENT = 3.0
GAP_HIGH_THRESHOLD_PERCENT = 6.0
VOLATILITY_SPIKE_MULTIPLIER = 2.5
RESISTANCE_BREAK_MIN_RELATIVE_VOLUME = 1.5


@dataclass
class AnomalyEvent:
    anomaly_type: str
    severity: str
    description: str
    value: Optional[float] = None
    price: Optional[float] = None

    @property
    def label_tr(self) -> str:
        return ANOMALY_LABELS_TR.get(self.anomaly_type, self.anomaly_type)


@dataclass
class AnomalyDetectionResult:
    symbol: str
    timeframe: str
    available: bool
    events: list[AnomalyEvent] = field(default_factory=list)
    note: str = ""
    relative_volume: Optional[float] = None


def detect_anomalies(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str = "1d",
    sr_result: Optional[SupportResistanceResult] = None,
    corporate_actions: Optional[list[dict] | list[CorporateActionEvent]] = None,
) -> AnomalyDetectionResult:
    """Gercek OHLCV verisinden anormal hareketleri tespit eder.

    Hicbir anomali UYDURULMAZ: yeterli veri yoksa available=False doner ve
    bos bir olay listesi verilir. Tum esikler sabit ve aciklanabilir
    (bolum 8 spesifikasyonu): hacim patlamasi, gap, destek/direnc kirilimi,
    volatilite patlamasi.
    """
    if df is None or len(df) < MIN_BARS_FOR_ANOMALY:
        return AnomalyDetectionResult(
            symbol=symbol, timeframe=timeframe, available=False,
            note="Anomali tespiti için yeterli veri yok.",
        )

    df = df.sort_values("timestamp").reset_index(drop=True)
    last = df.iloc[-1]
    prev = df.iloc[-2]
    events: list[AnomalyEvent] = []

    normalized_actions = (
        corporate_actions
        if corporate_actions and isinstance(corporate_actions[0], CorporateActionEvent)
        else normalize_corporate_actions(symbol, corporate_actions or [])
    )
    last_date = pd.Timestamp(last["timestamp"]).date()
    action_context = classify_price_gap(normalized_actions, last_date)
    exclude_price_anomaly = bool(action_context["excluded_from_gap_alarm"])

    last_close = float(last["close"])

    # --- Hacim patlamasi ---
    avg_vol_20 = float(df["volume"].tail(21).iloc[:-1].mean()) if len(df) >= 21 else float(df["volume"].mean())
    relative_volume = (float(last["volume"]) / avg_vol_20) if avg_vol_20 > 0 else None
    if relative_volume is not None and relative_volume >= VOLUME_SPIKE_RELATIVE_VOLUME:
        severity = SEVERITY_HIGH if relative_volume >= VOLUME_SPIKE_HIGH_RELATIVE_VOLUME else SEVERITY_MEDIUM
        events.append(
            AnomalyEvent(
                ANOMALY_VOLUME_SPIKE, severity,
                f"Hacim, son 20 bar ortalamasının {relative_volume:.1f} katına çıktı.",
                value=round(relative_volume, 2), price=last_close,
            )
        )

    # --- Gap (acilis - onceki kapanis farki) ---
    prev_close = float(prev["close"])
    if prev_close > 0 and not exclude_price_anomaly:
        gap_percent = (float(last["open"]) - prev_close) / prev_close * 100
        if gap_percent >= GAP_THRESHOLD_PERCENT:
            severity = SEVERITY_HIGH if gap_percent >= GAP_HIGH_THRESHOLD_PERCENT else SEVERITY_MEDIUM
            events.append(
                AnomalyEvent(
                    ANOMALY_GAP_UP, severity,
                    f"Açılış, önceki kapanışın %{gap_percent:.2f} üzerinde (yukarı gap).",
                    value=round(gap_percent, 2), price=float(last["open"]),
                )
            )
        elif gap_percent <= -GAP_THRESHOLD_PERCENT:
            severity = SEVERITY_HIGH if gap_percent <= -GAP_HIGH_THRESHOLD_PERCENT else SEVERITY_MEDIUM
            events.append(
                AnomalyEvent(
                    ANOMALY_GAP_DOWN, severity,
                    f"Açılış, önceki kapanışın %{abs(gap_percent):.2f} altında (aşağı gap).",
                    value=round(gap_percent, 2), price=float(last["open"]),
                )
            )

    # --- Volatilite patlamasi (gunluk gercek aralik / true range, ortalamaya gore) ---
    true_range = max(
        float(last["high"]) - float(last["low"]),
        abs(float(last["high"]) - prev_close),
        abs(float(last["low"]) - prev_close),
    )
    tr_percent = (true_range / last_close * 100) if last_close > 0 else None
    prior = df.iloc[:-1]
    avg_range_percent = None
    if len(prior) >= 10:
        ranges_pct = (prior["high"] - prior["low"]).tail(20) / prior["close"].tail(20).replace(0, pd.NA) * 100
        ranges_pct = ranges_pct.dropna()
        if not ranges_pct.empty:
            avg_range_percent = float(ranges_pct.mean())

    if not exclude_price_anomaly and tr_percent is not None and avg_range_percent and avg_range_percent > 0:
        ratio = tr_percent / avg_range_percent
        if ratio >= VOLATILITY_SPIKE_MULTIPLIER:
            severity = SEVERITY_HIGH if ratio >= VOLATILITY_SPIKE_MULTIPLIER * 1.6 else SEVERITY_MEDIUM
            events.append(
                AnomalyEvent(
                    ANOMALY_VOLATILITY_SPIKE, severity,
                    f"Günlük fiyat aralığı, son 20 barın ortalamasının {ratio:.1f} katına çıktı.",
                    value=round(ratio, 2), price=last_close,
                )
            )

    # --- Destek / direnc kirilimi ---
    if sr_result is not None and not exclude_price_anomaly:
        if sr_result.support_broken_with_volume and sr_result.support_1 is not None:
            events.append(
                AnomalyEvent(
                    ANOMALY_SUPPORT_BREAK, SEVERITY_HIGH,
                    f"{sr_result.support_1} destek seviyesi hacimli şekilde kırıldı.",
                    value=sr_result.support_1, price=last_close,
                )
            )
        if (
            sr_result.resistance_1 is not None
            and last_close > sr_result.resistance_1
            and relative_volume is not None
            and relative_volume >= RESISTANCE_BREAK_MIN_RELATIVE_VOLUME
        ):
            events.append(
                AnomalyEvent(
                    ANOMALY_RESISTANCE_BREAK, SEVERITY_MEDIUM,
                    f"{sr_result.resistance_1} direnç seviyesi hacim desteğiyle kırıldı.",
                    value=sr_result.resistance_1, price=last_close,
                )
            )

    return AnomalyDetectionResult(
        symbol=symbol,
        timeframe=timeframe,
        available=True,
        events=events,
        note=(
            action_context["note"] + "; fiyat hareketi normal gap/anomali olarak işaretlenmedi."
            if exclude_price_anomaly else ("" if events else "Anormal hareket tespit edilmedi.")
        ),
        relative_volume=round(relative_volume, 2) if relative_volume is not None else None,
    )
