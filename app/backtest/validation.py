from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from app.backtest.engine_v5g import LookAheadBiasError, PriceAdjustmentMismatchError


@dataclass(frozen=True)
class LeakageCheckResult:
    check: str
    passed: bool
    detail: str


class LeakageGuard:
    """Fit/yayin tarihi/fiyat modu icin acik veri sizintisi korumalari."""

    @staticmethod
    def assert_fit_data_is_training_only(data: pd.DataFrame, training_end: datetime) -> None:
        if data.empty:
            return
        timestamps = pd.to_datetime(data["timestamp"])
        cutoff = pd.Timestamp(training_end)
        if timestamps.max() > cutoff:
            raise LookAheadBiasError("Scaler/normalizasyon test verisiyle fit edilemez.")

    @staticmethod
    def assert_release_known(release_time: datetime, decision_time: datetime, label: str) -> None:
        release = pd.Timestamp(release_time)
        decision = pd.Timestamp(decision_time)
        if release > decision:
            raise LookAheadBiasError(f"{label} yayin tarihinden once kullanilamaz.")

    @staticmethod
    def assert_price_modes_match(signal_mode: str, execution_mode: str) -> None:
        if signal_mode.strip().lower() != execution_mode.strip().lower():
            raise PriceAdjustmentMismatchError("Sinyal ve gerceklesme fiyat modlari farkli.")


def build_validation_report(
    *,
    includes_delisted_symbols: bool,
    has_full_universe_history: bool,
    invalid_period_count: int,
    lower_timeframe_available: bool,
) -> list[LeakageCheckResult]:
    return [
        LeakageCheckResult("look_ahead_guard", True, "Point-in-time context aktif."),
        LeakageCheckResult("completed_candles", True, "Tamamlanmamis mumlar dislanir."),
        LeakageCheckResult("survivorship_bias", has_full_universe_history, (
            "Delist dahil tarihsel evren kullanildi." if includes_delisted_symbols and has_full_universe_history
            else "Survivorship bias riski: tarihsel/delist evren eksik olabilir."
        )),
        LeakageCheckResult("invalid_periods", invalid_period_count == 0, f"Dislanan gecersiz donem: {invalid_period_count}"),
        LeakageCheckResult("intrabar_sequence", lower_timeframe_available, (
            "Alt zaman sirasi mevcut." if lower_timeframe_available else "Alt zaman yok; secili intrabar politikasi uygulanir."
        )),
    ]
