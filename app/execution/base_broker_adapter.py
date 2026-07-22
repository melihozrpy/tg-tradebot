from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class BaseBrokerAdapter(ABC):
    """Canli/sanal emir gonderimi icin ortak arayuz."""

    name: str = "base"
    is_live: bool = False

    @abstractmethod
    def market_buy(self, symbol: str, quantity: float, market_price: float, **kwargs):
        ...

    @abstractmethod
    def market_sell(self, symbol: str, quantity: float, market_price: float, **kwargs):
        ...
