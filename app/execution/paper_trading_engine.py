from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
from sqlalchemy.orm import Session

from app.backtest.engine_v5g import TransactionCostConfig
from app.models.database import PaperAccount, PaperOrder, PaperTrade, PaperTradeEvent, Signal

ACTIVE_STATUSES = {"PENDING", "ACTIVE", "TARGET_1_HIT", "TARGET_2_HIT", "PARTIALLY_CLOSED"}
FINAL_STATUSES = {"TARGET_3_HIT", "STOPPED", "MANUALLY_CLOSED", "EXPIRED", "DATA_PROBLEM", "CANCELLED"}


class PaperTradingError(ValueError):
    pass


@dataclass(frozen=True)
class PaperTradePreview:
    symbol: str
    current_price: float
    quantity: float
    stop_price: float
    targets: tuple[Optional[float], Optional[float], Optional[float]]
    maximum_virtual_risk: float
    risk_reward: Optional[float]
    estimated_entry_cost: float
    warning: str = "Bu yalnizca sanal islemdir; gercek emir gonderilmez."


class PaperTradingEngine:
    """SQLite kaliciligiyla kullanici-izole, brokersiz sanal islem motoru."""

    def __init__(
        self,
        db: Session,
        *,
        initial_capital: float = 100_000.0,
        transaction_costs: TransactionCostConfig | None = None,
    ):
        self.db = db
        self.initial_capital = float(initial_capital)
        self.costs = transaction_costs or TransactionCostConfig()

    def get_or_create_account(self, user_id: int) -> PaperAccount:
        account = self.db.query(PaperAccount).filter_by(user_id=user_id).one_or_none()
        if account is None:
            account = PaperAccount(
                user_id=user_id,
                initial_capital=self.initial_capital,
                cash_balance=self.initial_capital,
                realized_pnl=0.0,
            )
            self.db.add(account)
            self.db.commit()
            self.db.refresh(account)
        return account

    def preview(
        self,
        *,
        symbol: str,
        quantity: float,
        current_price: float,
        stop_price: float,
        targets: tuple[Optional[float], Optional[float], Optional[float]],
    ) -> PaperTradePreview:
        self._validate_levels(quantity, current_price, stop_price, targets)
        fill = self.costs.entry_fill(current_price)
        commission = self.costs.cash_cost(fill * quantity)
        risk = max(0.0, (fill - stop_price) * quantity) + commission
        first_target = next((value for value in targets if value is not None), None)
        rr = ((first_target - fill) / (fill - stop_price)) if first_target and fill > stop_price else None
        return PaperTradePreview(
            symbol=symbol.upper(), current_price=current_price, quantity=quantity,
            stop_price=stop_price, targets=targets,
            maximum_virtual_risk=round(risk, 2),
            risk_reward=round(rr, 2) if rr is not None else None,
            estimated_entry_cost=round(commission + self.costs.impact_cost(current_price, fill, quantity), 2),
        )

    def open_trade(
        self,
        *,
        user_id: int,
        symbol: str,
        quantity: float,
        current_price: float,
        stop_price: float,
        targets: tuple[Optional[float], Optional[float], Optional[float]],
        signal_type: str = "MANUAL",
        signal_id: Optional[int] = None,
        signal_snapshot_id: Optional[int] = None,
        provider: str = "unknown",
        data_quality: str = "VALID",
        partial_exit_ratios: tuple[float, float, float] = (0.40, 0.30, 0.30),
        opened_at: Optional[datetime] = None,
        max_holding_days: int = 60,
    ) -> PaperTrade:
        if data_quality.upper() not in {"VALID", "OK", "GOOD"}:
            raise PaperTradingError("Veri kalitesi uygun degil; sanal islem acilmadi.")
        self._validate_levels(quantity, current_price, stop_price, targets)
        if len(partial_exit_ratios) != 3 or any(value < 0 for value in partial_exit_ratios) or sum(partial_exit_ratios) > 1.000001:
            raise PaperTradingError("Kismi cikis oranlari gecersiz.")
        if signal_id is not None:
            source_signal = self.db.get(Signal, signal_id)
            if source_signal is None:
                raise PaperTradingError("Sinyal bulunamadi; sanal islem kaynagi dogrulanamadi.")
            if source_signal.user_id is not None and source_signal.user_id != user_id:
                raise PaperTradingError("Bu sinyal baska bir kullaniciya ait.")
            duplicate = self.db.query(PaperTrade).filter(
                PaperTrade.user_id == user_id,
                PaperTrade.signal_id == signal_id,
            ).one_or_none()
            if duplicate is not None:
                raise PaperTradingError("Ayni sinyal icin acik sanal islem zaten var.")

        account = self.get_or_create_account(user_id)
        fill = self.costs.entry_fill(current_price)
        notional = fill * quantity
        commission = self.costs.cash_cost(notional)
        slippage = self.costs.impact_cost(current_price, fill, quantity)
        debit = notional + commission
        if debit > account.cash_balance + 1e-9:
            raise PaperTradingError("Sanal bakiye yetersiz.")

        order = PaperOrder(
            user_id=user_id, symbol=symbol.upper(), side="BUY", order_type="MARKET",
            quantity=quantity, status="FILLED", created_at=opened_at or datetime.now(timezone.utc),
        )
        self.db.add(order)
        self.db.flush()
        trade = PaperTrade(
            order_id=order.id,
            user_id=user_id,
            signal_id=signal_id,
            signal_snapshot_id=signal_snapshot_id,
            symbol=symbol.upper(),
            side="BUY",
            quantity=quantity,
            original_quantity=quantity,
            remaining_quantity=quantity,
            fill_price=fill,
            entry_price=fill,
            commission=commission,
            slippage=slippage,
            executed_at=opened_at or datetime.now(timezone.utc),
            opened_at=opened_at or datetime.now(timezone.utc),
            expires_at=(opened_at or datetime.now(timezone.utc)) + timedelta(days=max(1, int(max_holding_days))),
            status="ACTIVE",
            stop_price=stop_price,
            target_1=targets[0], target_2=targets[1], target_3=targets[2],
            partial_exit_config=json.dumps(list(partial_exit_ratios)),
            current_price=current_price,
            realized_pnl=-commission,
            unrealized_pnl=(current_price - fill) * quantity,
            max_favorable_pnl=0.0,
            max_adverse_pnl=min(0.0, (current_price - fill) * quantity),
            data_provider=provider,
            data_quality=data_quality.upper(),
        )
        account.cash_balance -= debit
        self.db.add(trade)
        self.db.flush()
        self._event(trade, "OPENED", current_price, quantity, commission, slippage, {"signal_type": signal_type})
        self.db.commit()
        self.db.refresh(trade)
        return trade

    def update_trade_from_completed_bar(
        self,
        *,
        user_id: int,
        trade_id: int,
        bar: dict | pd.Series,
        fetched_at: datetime,
        now: Optional[datetime] = None,
        max_cache_age: timedelta = timedelta(minutes=30),
    ) -> PaperTrade:
        trade = self.get_trade(user_id, trade_id)
        if trade.status not in ACTIVE_STATUSES:
            return trade
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        if now - fetched_at > max_cache_age:
            # Eski cache fiyatini islem sonucu gibi kullanma; durum acik kalir.
            return trade
        if not bool(bar.get("is_complete", False)):
            return trade
        quality = str(bar.get("data_quality", "VALID")).upper()
        if quality not in {"VALID", "OK", "GOOD"}:
            trade.status = "DATA_PROBLEM"
            trade.close_reason = "DATA_PROBLEM"
            trade.data_quality = quality
            self._event(trade, "DATA_PROBLEM", None, None, 0.0, 0.0, {})
            self.db.commit()
            return trade

        self._apply_split(trade, float(bar.get("split_factor", 1.0) or 1.0))
        dividend = float(bar.get("cash_dividend", 0.0) or 0.0)
        if dividend > 0:
            account = self.get_or_create_account(user_id)
            dividend_cash = dividend * float(trade.remaining_quantity or 0.0)
            account.cash_balance += dividend_cash
            account.realized_pnl += dividend_cash
            trade.realized_pnl = float(trade.realized_pnl or 0.0) + dividend_cash
            for name in ("stop_price", "target_1", "target_2", "target_3"):
                value = getattr(trade, name)
                if value is not None:
                    setattr(trade, name, max(0.0001, value - dividend))
            self._event(trade, "CASH_DIVIDEND", dividend, trade.remaining_quantity, 0.0, 0.0, {"cash": dividend_cash})
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        if trade.expires_at is not None:
            bar_time = pd.Timestamp(bar.get("timestamp", now))
            expiry = pd.Timestamp(trade.expires_at)
            if bar_time.tzinfo is None and expiry.tzinfo is not None:
                bar_time = bar_time.tz_localize(expiry.tzinfo)
            elif bar_time.tzinfo is not None and expiry.tzinfo is None:
                expiry = expiry.tz_localize(bar_time.tzinfo)
            if bar_time >= expiry:
                self._exit_quantity(trade, float(trade.remaining_quantity or 0.0), close, "EXPIRED")
                self.db.commit(); self.db.refresh(trade)
                return trade
        remaining = float(trade.remaining_quantity or 0.0)
        trade.current_price = close
        current_pnl = (close - float(trade.entry_price)) * remaining
        trade.unrealized_pnl = current_pnl
        trade.max_favorable_pnl = max(float(trade.max_favorable_pnl or 0.0), (high - float(trade.entry_price)) * remaining)
        trade.max_adverse_pnl = min(float(trade.max_adverse_pnl or 0.0), (low - float(trade.entry_price)) * remaining)

        targets = [trade.target_1, trade.target_2, trade.target_3]
        already = {
            0: trade.status in {"TARGET_1_HIT", "TARGET_2_HIT", "PARTIALLY_CLOSED"},
            1: trade.status == "TARGET_2_HIT",
            2: False,
        }
        stop_hit = trade.stop_price is not None and low <= trade.stop_price
        target_hits = [target is not None and not already[index] and high >= target for index, target in enumerate(targets)]
        # Ayni tamamlanmis mumda iki taraf gorulurse conservative: once stop.
        if stop_hit:
            self._exit_quantity(trade, remaining, float(trade.stop_price), "STOPPED")
        else:
            ratios = json.loads(trade.partial_exit_config or "[0.4,0.3,0.3]")
            for index, hit in enumerate(target_hits):
                if not hit or float(trade.remaining_quantity or 0.0) <= 0:
                    continue
                quantity = float(trade.original_quantity) * float(ratios[index])
                if index == 2:
                    quantity = float(trade.remaining_quantity)
                quantity = min(quantity, float(trade.remaining_quantity))
                self._exit_quantity(trade, quantity, float(targets[index]), f"TARGET_{index + 1}_HIT")
                if float(trade.remaining_quantity or 0.0) > 0:
                    trade.status = f"TARGET_{index + 1}_HIT"
        self.db.commit()
        self.db.refresh(trade)
        return trade

    def close_manually(self, *, user_id: int, trade_id: int, current_price: float) -> PaperTrade:
        trade = self.get_trade(user_id, trade_id)
        if trade.status not in ACTIVE_STATUSES:
            raise PaperTradingError("Sanal islem zaten kapali.")
        self._exit_quantity(trade, float(trade.remaining_quantity or 0.0), current_price, "MANUALLY_CLOSED")
        self.db.commit()
        self.db.refresh(trade)
        return trade

    def get_trade(self, user_id: int, trade_id: int) -> PaperTrade:
        trade = self.db.query(PaperTrade).filter_by(id=trade_id, user_id=user_id).one_or_none()
        if trade is None:
            raise PaperTradingError("Sanal islem bulunamadi.")
        return trade

    def list_trades(self, user_id: int, *, active_only: bool = False, symbol: str | None = None) -> list[PaperTrade]:
        query = self.db.query(PaperTrade).filter(PaperTrade.user_id == user_id)
        if active_only:
            query = query.filter(PaperTrade.status.in_(ACTIVE_STATUSES))
        if symbol:
            query = query.filter(PaperTrade.symbol == symbol.upper())
        return query.order_by(PaperTrade.id.desc()).all()

    def performance(self, user_id: int, *, symbol: str | None = None) -> dict:
        account = self.get_or_create_account(user_id)
        trades = self.list_trades(user_id, symbol=symbol)
        realized = sum(float(item.realized_pnl or 0.0) for item in trades)
        unrealized = sum(float(item.unrealized_pnl or 0.0) for item in trades if item.status in ACTIVE_STATUSES)
        return {
            "initial_capital": account.initial_capital,
            "cash_balance": round(account.cash_balance, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "trade_count": len(trades),
            "active_trade_count": sum(item.status in ACTIVE_STATUSES for item in trades),
            "symbol": symbol.upper() if symbol else None,
            "is_real_money": False,
        }

    def _exit_quantity(self, trade: PaperTrade, quantity: float, reference: float, reason: str) -> None:
        if quantity <= 0:
            return
        account = self.get_or_create_account(int(trade.user_id))
        fill = self.costs.exit_fill(reference)
        notional = fill * quantity
        fee = self.costs.cash_cost(notional)
        slip = self.costs.impact_cost(reference, fill, quantity)
        proceeds = notional - fee
        account.cash_balance += proceeds
        entry_fee_share = float(trade.commission or 0.0) * quantity / max(float(trade.original_quantity or quantity), 1e-9)
        pnl = (fill - float(trade.entry_price)) * quantity - fee - entry_fee_share
        account.realized_pnl = float(account.realized_pnl or 0.0) + pnl
        trade.realized_pnl = float(trade.realized_pnl or 0.0) + (fill - float(trade.entry_price)) * quantity - fee
        trade.commission = float(trade.commission or 0.0) + fee
        trade.slippage = float(trade.slippage or 0.0) + slip
        trade.remaining_quantity = max(0.0, float(trade.remaining_quantity or 0.0) - quantity)
        trade.unrealized_pnl = (float(trade.current_price or fill) - float(trade.entry_price)) * float(trade.remaining_quantity)
        self._event(trade, reason, reference, quantity, fee, slip, {"net_pnl": round(pnl, 6)})
        if float(trade.remaining_quantity) <= 1e-9:
            trade.remaining_quantity = 0.0
            trade.status = reason
            trade.close_reason = reason
            trade.closed_at = datetime.now(timezone.utc)
        else:
            trade.status = "PARTIALLY_CLOSED"

    def _event(self, trade, event_type, price, quantity, commission, slippage, payload) -> None:
        self.db.add(PaperTradeEvent(
            paper_trade_id=trade.id,
            event_type=event_type,
            price=price,
            quantity=quantity,
            commission=commission,
            slippage=slippage,
            payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ))

    @staticmethod
    def _apply_split(trade: PaperTrade, factor: float) -> None:
        if factor <= 0 or abs(factor - 1.0) < 1e-12:
            return
        trade.quantity *= factor
        trade.original_quantity = float(trade.original_quantity or 0.0) * factor
        trade.remaining_quantity = float(trade.remaining_quantity or 0.0) * factor
        trade.entry_price /= factor
        trade.fill_price /= factor
        for name in ("stop_price", "target_1", "target_2", "target_3"):
            value = getattr(trade, name)
            if value is not None:
                setattr(trade, name, value / factor)

    @staticmethod
    def _validate_levels(quantity, current_price, stop_price, targets) -> None:
        if quantity <= 0 or current_price <= 0:
            raise PaperTradingError("Lot ve fiyat pozitif olmali.")
        if stop_price <= 0 or stop_price >= current_price:
            raise PaperTradingError("Stop, guncel fiyatin altinda pozitif bir seviye olmali.")
        ordered = [value for value in targets if value is not None]
        if any(value <= current_price for value in ordered) or ordered != sorted(ordered):
            raise PaperTradingError("Hedefler guncel fiyatin ustunde ve sirali olmali.")


def run_paper_trade_scan(db: Session, provider, *, now: Optional[datetime] = None) -> int:
    """Scheduler girisi; yalnizca tamamlanmis ve taze provider mumunu isler."""
    engine = PaperTradingEngine(db)
    processed = 0
    current_time = now or datetime.now(timezone.utc)
    trades = db.query(PaperTrade).filter(PaperTrade.status.in_(ACTIVE_STATUSES)).all()
    for trade in trades:
        try:
            bars = provider.get_ohlcv(trade.symbol, "1d", trade.opened_at, current_time)
            if bars is None or bars.empty:
                continue
            from app.analysis.data_quality import DataQualityEngine
            completed = DataQualityEngine().completed_candles(bars, "1d", current_time)
            if completed is None or completed.empty:
                continue
            row = completed.sort_values("timestamp").iloc[-1].to_dict()
            row["is_complete"] = True
            row.setdefault("data_quality", "VALID")
            fetched_at = row.get("fetched_at", current_time)
            if not isinstance(fetched_at, datetime):
                fetched_at = pd.Timestamp(fetched_at).to_pydatetime()
            engine.update_trade_from_completed_bar(
                user_id=int(trade.user_id), trade_id=trade.id, bar=row,
                fetched_at=fetched_at, now=current_time, max_cache_age=timedelta(hours=24),
            )
            processed += 1
        except Exception:
            db.rollback()
    return processed
