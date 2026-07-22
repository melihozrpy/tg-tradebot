from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


class InvalidStopError(Exception):
    """Stop mesafesi sifir veya anlamsizsa firlatilir; pozisyon reddedilir."""


@dataclass
class PositionSizeResult:
    lot: int
    risk_amount: float
    risk_per_share: float
    position_value: float
    position_percent_of_capital: float


def calculate_position_size(
    total_capital: float,
    risk_percent: float,
    entry_price: float,
    stop_price: float,
    lot_step: int = 1,
) -> PositionSizeResult:
    """risk_tutari = toplam_sermaye * risk_yuzdesi; lot = risk_tutari / |giris - stop|.

    Lot, piyasa kuraliyla uyumlu olacak sekilde asagi yuvarlanir.
    """
    if total_capital <= 0:
        raise ValueError("total_capital pozitif olmalidir.")
    if not (0 < risk_percent <= 100):
        raise ValueError("risk_percent 0-100 araliginda olmalidir.")
    if entry_price <= 0:
        raise ValueError("entry_price pozitif olmalidir.")

    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share <= 0 or risk_per_share / entry_price < 0.002:
        raise InvalidStopError(
            "Stop mesafesi sifir veya anlamsiz derecede dar; pozisyon boyutu hesaplanamaz."
        )

    risk_amount = total_capital * (risk_percent / 100.0)
    raw_lot = risk_amount / risk_per_share
    lot = math.floor(raw_lot / lot_step) * lot_step

    position_value = lot * entry_price
    position_percent = (position_value / total_capital) * 100 if total_capital else 0.0

    return PositionSizeResult(
        lot=int(lot),
        risk_amount=round(risk_amount, 2),
        risk_per_share=round(risk_per_share, 4),
        position_value=round(position_value, 2),
        position_percent_of_capital=round(position_percent, 2),
    )


def calculate_atr_stop(entry_price: float, atr_value: float, multiplier: float = 2.0) -> float:
    stop = entry_price - (atr_value * multiplier)
    return round(max(stop, 0.01), 2)


def enforce_daily_loss_limit(
    realized_pnl_today: float, total_capital: float, max_daily_loss_percent: float
) -> bool:
    """Gunluk zarar limiti asilmissa True doner (yeni islem engellenmeli)."""
    if total_capital <= 0:
        return True
    loss_percent = (-realized_pnl_today / total_capital) * 100 if realized_pnl_today < 0 else 0.0
    return loss_percent >= max_daily_loss_percent


def enforce_max_open_positions(open_position_count: int, max_open_positions: int) -> bool:
    """Maksimum acik pozisyon sayisi asilmissa True doner."""
    return open_position_count >= max_open_positions


def enforce_sector_exposure(
    current_sector_value: float, new_position_value: float, total_capital: float, max_sector_percent: float
) -> bool:
    """Sektor maruziyeti limiti asilirsa True doner (yeni pozisyon reddedilmeli)."""
    if total_capital <= 0:
        return True
    projected_percent = ((current_sector_value + new_position_value) / total_capital) * 100
    return projected_percent > max_sector_percent
