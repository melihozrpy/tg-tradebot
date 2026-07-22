from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.database import PortfolioPosition, User
from app.services.sector_service import get_sector_info
from app.services.watchlist_service import normalize_symbol


def add_position(
    db: Session, user: User, raw_symbol: str, lot: float, average_cost: float, stop_price: float | None = None
) -> PortfolioPosition:
    symbol = normalize_symbol(raw_symbol)
    if lot <= 0:
        raise ValueError("lot pozitif olmalidir.")
    if average_cost <= 0:
        raise ValueError("average_cost pozitif olmalidir.")

    existing = (
        db.query(PortfolioPosition)
        .filter(
            PortfolioPosition.user_id == user.id,
            PortfolioPosition.symbol == symbol,
            PortfolioPosition.closed_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        raise ValueError(f"'{symbol}' icin zaten acik bir pozisyon var; /pozisyon_guncelle kullanin.")

    position = PortfolioPosition(
        user_id=user.id,
        symbol=symbol,
        lot=lot,
        average_cost=average_cost,
        stop_price=stop_price,
    )
    db.add(position)
    db.commit()
    db.refresh(position)
    return position


def update_position(
    db: Session, user: User, raw_symbol: str, lot: float, average_cost: float, stop_price: float | None = None
) -> PortfolioPosition:
    symbol = normalize_symbol(raw_symbol)
    position = (
        db.query(PortfolioPosition)
        .filter(
            PortfolioPosition.user_id == user.id,
            PortfolioPosition.symbol == symbol,
            PortfolioPosition.closed_at.is_(None),
        )
        .first()
    )
    if position is None:
        raise ValueError(f"'{symbol}' icin acik pozisyon bulunamadi; once /pozisyon_ekle kullanin.")
    if lot <= 0:
        raise ValueError("lot pozitif olmalidir.")
    if average_cost <= 0:
        raise ValueError("average_cost pozitif olmalidir.")

    position.lot = lot
    position.average_cost = average_cost
    if stop_price is not None:
        position.stop_price = stop_price
    db.commit()
    db.refresh(position)
    return position


def remove_position_by_symbol(db: Session, user: User, raw_symbol: str) -> None:
    symbol = normalize_symbol(raw_symbol)
    position = (
        db.query(PortfolioPosition)
        .filter(
            PortfolioPosition.user_id == user.id,
            PortfolioPosition.symbol == symbol,
            PortfolioPosition.closed_at.is_(None),
        )
        .first()
    )
    if position is None:
        raise ValueError(f"'{symbol}' icin acik pozisyon bulunamadi.")
    db.delete(position)
    db.commit()


def remove_position(db: Session, user: User, position_id: int) -> None:
    position = (
        db.query(PortfolioPosition)
        .filter(PortfolioPosition.id == position_id, PortfolioPosition.user_id == user.id)
        .first()
    )
    if position is None:
        raise ValueError(f"Pozisyon bulunamadi: {position_id}")
    db.delete(position)
    db.commit()


def list_positions(db: Session, user: User) -> list[PortfolioPosition]:
    return (
        db.query(PortfolioPosition)
        .filter(PortfolioPosition.user_id == user.id, PortfolioPosition.closed_at.is_(None))
        .order_by(PortfolioPosition.symbol)
        .all()
    )


def set_cash_balance(db: Session, user: User, amount: float) -> User:
    if amount < 0:
        raise ValueError("Nakit tutari negatif olamaz.")
    user.cash_balance = amount
    db.commit()
    db.refresh(user)
    return user


def set_total_capital(db: Session, user: User, amount: float) -> User:
    if amount <= 0:
        raise ValueError("Sermaye pozitif olmalidir.")
    user.total_capital = amount
    db.commit()
    db.refresh(user)
    return user


def portfolio_summary(db: Session, user: User, current_prices: dict[str, float]) -> dict:
    positions = list_positions(db, user)
    total_cost = 0.0
    total_value = 0.0
    rows = []
    for pos in positions:
        price = current_prices.get(pos.symbol, pos.average_cost)
        cost_value = pos.lot * pos.average_cost
        market_value = pos.lot * price
        pnl = market_value - cost_value
        pnl_percent = (pnl / cost_value * 100) if cost_value else 0.0
        stop_loss_amount = None
        if pos.stop_price is not None:
            stop_loss_amount = round((pos.stop_price - price) * pos.lot, 2)
        total_cost += cost_value
        total_value += market_value
        rows.append(
            {
                "symbol": pos.symbol,
                "lot": pos.lot,
                "average_cost": pos.average_cost,
                "current_price": price,
                "pnl": round(pnl, 2),
                "pnl_percent": round(pnl_percent, 2),
                "stop_price": pos.stop_price,
                "stop_loss_amount": stop_loss_amount,
                "position_value": round(market_value, 2),
            }
        )

    total_pnl = total_value - total_cost
    total_pnl_percent = (total_pnl / total_cost * 100) if total_cost else 0.0

    for row in rows:
        row["weight_percent"] = round((row["position_value"] / total_value * 100), 2) if total_value else 0.0

    return {
        "positions": rows,
        "total_cost": round(total_cost, 2),
        "total_value": round(total_value, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_percent": round(total_pnl_percent, 2),
        "cash_balance": user.cash_balance,
    }


def portfolio_risk_summary(db: Session, user: User, current_prices: dict[str, float]) -> dict:
    """Portfoy genelinde risk gorunumu: sektor yogunlasmasi, maksimum stop
    senaryosu, en buyuk pozisyon, ayni yonde hareket eden pozisyon sayisi.
    """
    summary = portfolio_summary(db, user, current_prices)
    rows = summary["positions"]

    sector_exposure: dict[str, float] = {}
    total_value = summary["total_value"] or 1e-9
    worst_case_loss = 0.0
    largest_position = None

    for row in rows:
        info = get_sector_info(row["symbol"])
        sector_name = info.sector_name if info else "Eslesmemis"
        sector_exposure[sector_name] = sector_exposure.get(sector_name, 0.0) + row["position_value"]

        if row["stop_loss_amount"] is not None and row["stop_loss_amount"] < 0:
            worst_case_loss += row["stop_loss_amount"]

        if largest_position is None or row["position_value"] > largest_position["position_value"]:
            largest_position = row

    sector_exposure_percent = {
        name: round(value / total_value * 100, 1) for name, value in sector_exposure.items()
    }
    max_sector = max(sector_exposure_percent.items(), key=lambda kv: kv[1]) if sector_exposure_percent else None

    return {
        "total_value": summary["total_value"],
        "total_pnl": summary["total_pnl"],
        "largest_position": largest_position,
        "sector_exposure_percent": sector_exposure_percent,
        "max_sector_concentration": max_sector,
        "worst_case_stop_loss": round(worst_case_loss, 2),
        "position_count": len(rows),
    }
