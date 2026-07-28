from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import distinct
from sqlalchemy.orm import Session

from app.analysis.bist_trade_plan import DirectionPlan, build_bist_trade_plan
from app.analysis.indicator_engine import compute_technical_snapshot
from app.analysis.smart_money_engine import detect_smart_money
from app.models.database import VirtualPortfolio, VirtualTrade


class VirtualTradingError(ValueError):
    pass


@dataclass(frozen=True)
class VirtualRiskRules:
    normal_risk_percent: float = 1.0
    after_loss_risk_percent: float = 0.5
    minimum_rr: float = 2.0
    minimum_checklist: int = 5
    blocked_weekdays: tuple[int, ...] = (0, 4)
    maximum_portfolios: int = 3
    maximum_strategies: int = 2

    @classmethod
    def from_settings(cls, settings) -> "VirtualRiskRules":
        blocked: list[int] = []
        for item in str(settings.virtual_trade_blocked_weekdays).split(","):
            item = item.strip()
            if not item:
                continue
            value = int(item)
            if not 0 <= value <= 6:
                raise ValueError("VIRTUAL_TRADE_BLOCKED_WEEKDAYS değerleri 0-6 olmalıdır.")
            blocked.append(value)
        return cls(
            normal_risk_percent=float(settings.virtual_trade_risk_percent),
            after_loss_risk_percent=float(settings.virtual_trade_after_loss_risk_percent),
            minimum_rr=float(settings.virtual_trade_minimum_rr),
            minimum_checklist=int(settings.virtual_trade_minimum_checklist),
            blocked_weekdays=tuple(dict.fromkeys(blocked)),
            maximum_portfolios=int(settings.virtual_portfolio_max_per_user),
            maximum_strategies=int(settings.virtual_portfolio_max_strategies),
        )


@dataclass(frozen=True)
class SmxmSignalCandidate:
    instrument: str
    direction: str
    entry_price: float
    sl: float
    tp: float
    planned_rr: float
    checklist_score: int
    strategy_name: str = "smxm"
    notes: str = ""


@dataclass(frozen=True)
class SimulatedTrade:
    instrument: str
    direction: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    sl: float
    tp: float
    size: float
    risk_percent: float
    planned_rr: float
    checklist_score: int
    pnl: float
    exit_reason: str


@dataclass(frozen=True)
class SmxmBacktestResult:
    instrument: str
    start_date: datetime
    end_date: datetime
    starting_balance: float
    ending_balance: float
    trades: tuple[SimulatedTrade, ...]
    equity_timestamps: tuple[datetime, ...]
    equity_values: tuple[float, ...]
    win_rate: float
    average_rr: float
    max_drawdown_percent: float
    total_return_percent: float
    skipped_setups: tuple[str, ...] = ()


class SmxmVirtualPortfolioEngine:
    """Gerçek emir göndermeyen, kuralları DB seviyesinde loglayan motor."""

    def __init__(self, db: Session, rules: VirtualRiskRules | None = None):
        self.db = db
        self.rules = rules or VirtualRiskRules()

    def create_portfolio(self, *, user_id: int, name: str, starting_balance: float) -> VirtualPortfolio:
        clean_name = " ".join(name.split())
        if not clean_name or len(clean_name) > 96:
            raise VirtualTradingError("Portföy adı 1-96 karakter olmalıdır.")
        if starting_balance <= 0:
            raise VirtualTradingError("Başlangıç bakiyesi pozitif olmalıdır.")
        count = self.db.query(VirtualPortfolio).filter_by(user_id=user_id).count()
        if count >= self.rules.maximum_portfolios:
            raise VirtualTradingError(
                f"En fazla {self.rules.maximum_portfolios} sanal portföy oluşturabilirsin."
            )
        if self.db.query(VirtualPortfolio).filter_by(user_id=user_id, name=clean_name).first():
            raise VirtualTradingError("Aynı isimde sanal portföy zaten var.")
        portfolio = VirtualPortfolio(
            user_id=user_id,
            name=clean_name,
            starting_balance=float(starting_balance),
            current_balance=float(starting_balance),
        )
        self.db.add(portfolio)
        self.db.commit()
        self.db.refresh(portfolio)
        return portfolio

    def list_portfolios(self, user_id: int) -> list[VirtualPortfolio]:
        return (
            self.db.query(VirtualPortfolio)
            .filter_by(user_id=user_id)
            .order_by(VirtualPortfolio.id)
            .all()
        )

    def _last_closed_trade(self, portfolio_id: int) -> VirtualTrade | None:
        return (
            self.db.query(VirtualTrade)
            .filter_by(portfolio_id=portfolio_id, status="closed")
            .order_by(VirtualTrade.closed_at.desc(), VirtualTrade.id.desc())
            .first()
        )

    def next_risk_percent(self, portfolio_id: int) -> float:
        previous = self._last_closed_trade(portfolio_id)
        return (
            self.rules.after_loss_risk_percent
            if previous is not None and float(previous.pnl or 0.0) < 0
            else self.rules.normal_risk_percent
        )

    def _validate_strategy_limit(self, portfolio_id: int, strategy_name: str) -> None:
        names = {
            value
            for (value,) in self.db.query(distinct(VirtualTrade.strategy_name))
            .filter(VirtualTrade.portfolio_id == portfolio_id)
            .all()
            if value
        }
        if strategy_name not in names and len(names) >= self.rules.maximum_strategies:
            raise VirtualTradingError(
                f"Bir portföyde en fazla {self.rules.maximum_strategies} strateji simüle edilebilir."
            )

    def open_trade(
        self,
        *,
        portfolio_id: int,
        candidate: SmxmSignalCandidate,
        opened_at: datetime | None = None,
    ) -> VirtualTrade:
        portfolio = self.db.get(VirtualPortfolio, portfolio_id)
        if portfolio is None:
            raise VirtualTradingError("Sanal portföy bulunamadı.")
        timestamp = opened_at or datetime.now(timezone.utc)
        if timestamp.weekday() in self.rules.blocked_weekdays:
            raise VirtualTradingError("Bu hafta günü config gereği yeni sanal işlem açılamaz.")
        direction = candidate.direction.strip().lower()
        if direction not in {"long", "short"}:
            raise VirtualTradingError("Yön long veya short olmalıdır.")
        if candidate.checklist_score < self.rules.minimum_checklist:
            raise VirtualTradingError(
                f"Yalnız {self.rules.minimum_checklist}/6 ve üzeri A+ setup kabul edilir."
            )
        if candidate.planned_rr < self.rules.minimum_rr:
            raise VirtualTradingError(f"Minimum RR 1:{self.rules.minimum_rr:g} şartı sağlanmıyor.")
        risk_per_unit = (
            candidate.entry_price - candidate.sl
            if direction == "long"
            else candidate.sl - candidate.entry_price
        )
        reward_per_unit = (
            candidate.tp - candidate.entry_price
            if direction == "long"
            else candidate.entry_price - candidate.tp
        )
        if risk_per_unit <= 0 or reward_per_unit <= 0:
            raise VirtualTradingError("Entry/SL/TP sıralaması yönle uyumlu değil.")
        actual_rr = reward_per_unit / risk_per_unit
        if actual_rr + 1e-9 < self.rules.minimum_rr:
            raise VirtualTradingError("Fiyat seviyelerinden hesaplanan gerçek RR yetersiz.")
        self._validate_strategy_limit(portfolio_id, candidate.strategy_name)
        if (
            self.db.query(VirtualTrade)
            .filter_by(portfolio_id=portfolio_id, instrument=candidate.instrument.upper(), status="open")
            .first()
        ):
            raise VirtualTradingError("Bu enstrümanda açık sanal işlem zaten var.")

        risk_percent = self.next_risk_percent(portfolio_id)
        risk_budget = float(portfolio.current_balance) * risk_percent / 100.0
        size = math.floor((risk_budget / risk_per_unit) * 10_000) / 10_000
        if size <= 0:
            raise VirtualTradingError("Risk bütçesiyle pozitif işlem boyutu hesaplanamadı.")
        trade = VirtualTrade(
            portfolio_id=portfolio_id,
            instrument=candidate.instrument.upper(),
            direction=direction,
            entry_price=candidate.entry_price,
            sl=candidate.sl,
            tp=candidate.tp,
            size=size,
            risk_percent=risk_percent,
            opened_at=timestamp,
            status="open",
            setup_checklist_score=candidate.checklist_score,
            strategy_name=candidate.strategy_name,
            planned_rr=actual_rr,
            notes=candidate.notes,
        )
        self.db.add(trade)
        self.db.commit()
        self.db.refresh(trade)
        return trade

    def close_trade(
        self,
        *,
        trade_id: int,
        exit_price: float,
        closed_at: datetime | None = None,
        note: str = "",
    ) -> VirtualTrade:
        trade = self.db.get(VirtualTrade, trade_id)
        if trade is None or trade.status != "open":
            raise VirtualTradingError("Açık sanal işlem bulunamadı.")
        if exit_price <= 0:
            raise VirtualTradingError("Çıkış fiyatı pozitif olmalıdır.")
        sign = 1 if trade.direction == "long" else -1
        pnl = (float(exit_price) - float(trade.entry_price)) * float(trade.size) * sign
        portfolio = self.db.get(VirtualPortfolio, trade.portfolio_id)
        portfolio.current_balance = float(portfolio.current_balance) + pnl
        trade.exit_price = float(exit_price)
        trade.pnl = pnl
        trade.closed_at = closed_at or datetime.now(timezone.utc)
        trade.status = "closed"
        if note:
            trade.notes = "\n".join(filter(None, [trade.notes, note]))
        self.db.commit()
        self.db.refresh(trade)
        return trade


def _direction_plan(plan, direction: str) -> DirectionPlan:
    return plan.long if direction == "long" else plan.short


def _checklist_score(history: pd.DataFrame, plan, direction: str) -> int:
    snapshot = compute_technical_snapshot(history, plan.symbol, "1d")
    smart = detect_smart_money(history)
    wanted = "bullish" if direction == "long" else "bearish"
    score = 0
    score += int((snapshot.trend_direction == "up") == (direction == "long"))
    score += int(any(zone.direction == wanted for zone in smart.order_blocks))
    score += int(any(zone.direction == wanted for zone in smart.fvg))
    score += int(any(event.direction == wanted for event in smart.structure))
    score += int(snapshot.relative_volume >= 1.0)
    dplan = _direction_plan(plan, direction)
    score += int(any(value >= 2.0 for value in dplan.risk_multiples))
    return score


def _select_two_r_target(direction_plan: DirectionPlan) -> tuple[float, float]:
    for target, rr in zip(direction_plan.targets, direction_plan.risk_multiples):
        if rr >= 2.0:
            return float(target), float(rr)
    return float(direction_plan.targets[-1]), float(direction_plan.risk_multiples[-1])


def run_smxm_backtest(
    df: pd.DataFrame,
    *,
    instrument: str,
    start_date: datetime,
    end_date: datetime,
    starting_balance: float,
    rules: VirtualRiskRules | None = None,
    long_only: bool = False,
) -> SmxmBacktestResult:
    """Geçmiş OHLC üzerinde yalnız geçmiş barları kullanarak SMXM simülasyonu."""

    active_rules = rules or VirtualRiskRules()
    if start_date >= end_date:
        raise ValueError("Backtest başlangıcı bitişten önce olmalıdır.")
    if (end_date - start_date).days < 28:
        raise ValueError("SMXM backtest için en az bir aylık tarih aralığı gerekir.")
    if starting_balance <= 0:
        raise ValueError("Başlangıç bakiyesi pozitif olmalıdır.")
    data = df.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True, errors="coerce")
    data = data.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
    data = data.sort_values("timestamp").reset_index(drop=True)
    if len(data) < 80:
        raise ValueError("Backtest için warm-up dahil en az 80 mum gerekir.")

    balance = float(starting_balance)
    last_trade_pnl: float | None = None
    position: dict | None = None
    trades: list[SimulatedTrade] = []
    skipped: list[str] = []
    equity_times: list[datetime] = []
    equity_values: list[float] = []

    for index in range(60, len(data)):
        row = data.iloc[index]
        timestamp = pd.Timestamp(row["timestamp"]).to_pydatetime()
        if timestamp < start_date or timestamp > end_date:
            continue
        high, low, close = float(row["high"]), float(row["low"]), float(row["close"])

        if position is not None:
            direction = position["direction"]
            stop_hit = low <= position["sl"] if direction == "long" else high >= position["sl"]
            target_hit = high >= position["tp"] if direction == "long" else low <= position["tp"]
            if stop_hit or target_hit:
                # Aynı mumda ikisi de görülürse muhafazakâr şekilde stop önce.
                exit_price = position["sl"] if stop_hit else position["tp"]
                reason = "SL" if stop_hit else "TP"
                sign = 1 if direction == "long" else -1
                pnl = (exit_price - position["entry"]) * position["size"] * sign
                balance += pnl
                last_trade_pnl = pnl
                trades.append(
                    SimulatedTrade(
                        instrument=instrument.upper(), direction=direction,
                        entry_time=position["entry_time"], exit_time=timestamp,
                        entry_price=position["entry"], exit_price=exit_price,
                        sl=position["sl"], tp=position["tp"], size=position["size"],
                        risk_percent=position["risk_percent"], planned_rr=position["planned_rr"],
                        checklist_score=position["checklist_score"], pnl=pnl, exit_reason=reason,
                    )
                )
                position = None

        if position is None and timestamp.weekday() not in active_rules.blocked_weekdays:
            history = data.iloc[:index].copy()
            try:
                plan = build_bist_trade_plan(history, instrument)
                if plan.preferred_direction == "LONG":
                    direction = "long"
                elif plan.preferred_direction == "SHORT" and not long_only:
                    direction = "short"
                else:
                    direction = ""
                if direction:
                    dplan = _direction_plan(plan, direction)
                    checklist = _checklist_score(history, plan, direction)
                    target, planned_rr = _select_two_r_target(dplan)
                    touched = low <= dplan.entry_high and high >= dplan.entry_low
                    if checklist >= active_rules.minimum_checklist and planned_rr >= active_rules.minimum_rr and touched:
                        entry = min(max(float(row["open"]), dplan.entry_low), dplan.entry_high)
                        stop = float(dplan.stop_standard)
                        risk_per_unit = entry - stop if direction == "long" else stop - entry
                        reward = target - entry if direction == "long" else entry - target
                        actual_rr = reward / risk_per_unit if risk_per_unit > 0 else 0.0
                        if actual_rr >= active_rules.minimum_rr:
                            risk_percent = (
                                active_rules.after_loss_risk_percent
                                if last_trade_pnl is not None and last_trade_pnl < 0
                                else active_rules.normal_risk_percent
                            )
                            risk_budget = balance * risk_percent / 100.0
                            size = risk_budget / risk_per_unit
                            position = {
                                "direction": direction, "entry": entry, "entry_time": timestamp,
                                "sl": stop, "tp": target, "size": size,
                                "risk_percent": risk_percent, "planned_rr": actual_rr,
                                "checklist_score": checklist,
                            }
                        else:
                            skipped.append(f"{timestamp.date()}: gerçek RR {actual_rr:.2f}")
                    elif direction and len(skipped) < 50:
                        skipped.append(
                            f"{timestamp.date()}: checklist {checklist}/6, RR {planned_rr:.2f}, touch={touched}"
                        )
            except Exception as exc:  # noqa: BLE001 - tek bar simülasyonu durdurmaz
                if len(skipped) < 50:
                    skipped.append(f"{timestamp.date()}: {type(exc).__name__}")

        unrealized = 0.0
        if position is not None:
            sign = 1 if position["direction"] == "long" else -1
            unrealized = (close - position["entry"]) * position["size"] * sign
        equity_times.append(timestamp)
        equity_values.append(balance + unrealized)

    if position is not None and equity_times:
        exit_time = equity_times[-1]
        exit_price = float(data[data["timestamp"] <= pd.Timestamp(exit_time)].iloc[-1]["close"])
        sign = 1 if position["direction"] == "long" else -1
        pnl = (exit_price - position["entry"]) * position["size"] * sign
        balance += pnl
        trades.append(
            SimulatedTrade(
                instrument=instrument.upper(), direction=position["direction"],
                entry_time=position["entry_time"], exit_time=exit_time,
                entry_price=position["entry"], exit_price=exit_price,
                sl=position["sl"], tp=position["tp"], size=position["size"],
                risk_percent=position["risk_percent"], planned_rr=position["planned_rr"],
                checklist_score=position["checklist_score"], pnl=pnl, exit_reason="PERIOD_END",
            )
        )
        equity_values[-1] = balance

    equity = pd.Series(equity_values or [starting_balance], dtype="float64")
    running_max = equity.cummax().replace(0, pd.NA)
    drawdown = ((equity / running_max) - 1.0) * 100.0
    wins = sum(item.pnl > 0 for item in trades)
    return SmxmBacktestResult(
        instrument=instrument.upper(),
        start_date=start_date,
        end_date=end_date,
        starting_balance=float(starting_balance),
        ending_balance=round(balance, 2),
        trades=tuple(trades),
        equity_timestamps=tuple(equity_times),
        equity_values=tuple(float(value) for value in equity_values),
        win_rate=round(wins / len(trades) * 100.0, 2) if trades else 0.0,
        average_rr=round(sum(item.planned_rr for item in trades) / len(trades), 2) if trades else 0.0,
        max_drawdown_percent=round(abs(float(drawdown.min() or 0.0)), 2),
        total_return_percent=round((balance / starting_balance - 1.0) * 100.0, 2),
        skipped_setups=tuple(skipped),
    )
