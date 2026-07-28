from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


class PaperBrokerError(Exception):
    pass


@dataclass
class PaperPosition:
    symbol: str
    quantity: float
    average_price: float
    stop_price: Optional[float] = None
    take_profit: Optional[float] = None
    trailing_stop_percent: Optional[float] = None
    highest_price_since_entry: float = 0.0


@dataclass
class PaperFill:
    symbol: str
    side: str
    quantity: float
    fill_price: float
    commission: float
    slippage_amount: float
    executed_at: datetime


class PaperBroker:
    """Sanal bakiye ile calisan paper trading motoru.

    Gercek emir gondermez; BaseBrokerAdapter interface'ine benzer sekilde
    tasarlanmistir ki ileride DisabledLiveBroker/gercek broker adaptoru ile
    kolayca degistirilebilsin. FAZ 1'de yalnizca bu adaptor aktiftir.
    """

    def __init__(
        self,
        starting_balance: float,
        commission_percent: float = 0.15,
        slippage_percent: float = 0.05,
    ):
        self.starting_balance = starting_balance
        self.cash = starting_balance
        self.commission_percent = commission_percent
        self.slippage_percent = slippage_percent
        self.positions: dict[str, PaperPosition] = {}
        self.fills: list[PaperFill] = []
        self.closed_trade_pnls: list[float] = []

    def _apply_commission(self, notional: float) -> float:
        return notional * (self.commission_percent / 100)

    def market_buy(self, symbol: str, quantity: float, market_price: float, stop_price: Optional[float] = None, take_profit: Optional[float] = None) -> PaperFill:
        if quantity <= 0:
            raise PaperBrokerError("Miktar pozitif olmalidir.")
        if market_price <= 0:
            raise PaperBrokerError("Fiyat pozitif olmalidir.")

        slip = market_price * (self.slippage_percent / 100)
        fill_price = market_price + slip
        notional = fill_price * quantity
        commission = self._apply_commission(notional)
        total_cost = notional + commission

        if total_cost > self.cash:
            raise PaperBrokerError(
                f"Yetersiz sanal bakiye: gerekli={total_cost:.2f} mevcut={self.cash:.2f}"
            )

        self.cash -= total_cost

        if symbol in self.positions:
            pos = self.positions[symbol]
            new_qty = pos.quantity + quantity
            new_avg = ((pos.average_price * pos.quantity) + (fill_price * quantity)) / new_qty
            pos.quantity = new_qty
            pos.average_price = new_avg
            if stop_price is not None:
                pos.stop_price = stop_price
            if take_profit is not None:
                pos.take_profit = take_profit
        else:
            self.positions[symbol] = PaperPosition(
                symbol=symbol,
                quantity=quantity,
                average_price=fill_price,
                stop_price=stop_price,
                take_profit=take_profit,
                highest_price_since_entry=fill_price,
            )

        fill = PaperFill(
            symbol=symbol,
            side="BUY",
            quantity=quantity,
            fill_price=round(fill_price, 4),
            commission=round(commission, 4),
            slippage_amount=round(slip * quantity, 4),
            executed_at=datetime.now(timezone.utc),
        )
        self.fills.append(fill)
        return fill

    def market_sell(self, symbol: str, quantity: float, market_price: float) -> PaperFill:
        if symbol not in self.positions:
            raise PaperBrokerError(f"'{symbol}' icin acik pozisyon yok.")
        pos = self.positions[symbol]
        if quantity > pos.quantity:
            raise PaperBrokerError("Satis miktari mevcut pozisyondan buyuk olamaz.")
        if market_price <= 0:
            raise PaperBrokerError("Fiyat pozitif olmalidir.")

        slip = market_price * (self.slippage_percent / 100)
        fill_price = max(market_price - slip, 0.01)
        notional = fill_price * quantity
        commission = self._apply_commission(notional)
        proceeds = notional - commission

        pnl = (fill_price - pos.average_price) * quantity - commission
        self.closed_trade_pnls.append(round(pnl, 2))

        pos.quantity -= quantity
        self.cash += proceeds
        if pos.quantity <= 1e-9:
            del self.positions[symbol]

        fill = PaperFill(
            symbol=symbol,
            side="SELL",
            quantity=quantity,
            fill_price=round(fill_price, 4),
            commission=round(commission, 4),
            slippage_amount=round(slip * quantity, 4),
            executed_at=datetime.now(timezone.utc),
        )
        self.fills.append(fill)
        return fill

    def check_stop_and_targets(self, symbol: str, current_price: float) -> Optional[str]:
        """Fiyat stop/hedef seviyesine ulastiysa otomatik kapatir ve nedeni doner."""
        if symbol not in self.positions:
            return None
        pos = self.positions[symbol]
        pos.highest_price_since_entry = max(pos.highest_price_since_entry, current_price)

        if pos.stop_price is not None and current_price <= pos.stop_price:
            self.market_sell(symbol, pos.quantity, pos.stop_price)
            return "STOP_HIT"
        if pos.take_profit is not None and current_price >= pos.take_profit:
            self.market_sell(symbol, pos.quantity, pos.take_profit)
            return "TARGET_HIT"
        return None

    def equity(self, mark_prices: dict[str, float]) -> float:
        position_value = sum(
            mark_prices.get(sym, pos.average_price) * pos.quantity for sym, pos in self.positions.items()
        )
        return round(self.cash + position_value, 2)

    def performance_summary(self, mark_prices: Optional[dict[str, float]] = None) -> dict:
        mark_prices = mark_prices or {}
        equity_now = self.equity(mark_prices)
        realized_pnl = sum(self.closed_trade_pnls)
        return {
            "starting_balance": self.starting_balance,
            "cash": round(self.cash, 2),
            "equity": equity_now,
            "realized_pnl": round(realized_pnl, 2),
            "open_positions": len(self.positions),
            "total_fills": len(self.fills),
            "closed_trades": len(self.closed_trade_pnls),
            "return_percent": round(((equity_now / self.starting_balance) - 1) * 100, 2)
            if self.starting_balance
            else 0.0,
        }
