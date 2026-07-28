from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.models.database import get_db_session
from app.services.portfolio_service import add_position, portfolio_summary, remove_position
from app.services.watchlist_service import get_or_create_user

router = APIRouter()


class PositionCreateRequest(BaseModel):
    telegram_user_id: int
    symbol: str
    lot: float
    average_cost: float
    stop_price: float | None = None


@router.get("/portfolio")
def get_portfolio(telegram_user_id: int, db: Session = Depends(get_db_session)):
    settings = get_settings()
    user = get_or_create_user(db, telegram_user_id, telegram_user_id in settings.admin_ids, settings.default_total_capital)
    return portfolio_summary(db, user, current_prices={})


@router.post("/portfolio/positions")
def create_position(payload: PositionCreateRequest, db: Session = Depends(get_db_session)):
    settings = get_settings()
    user = get_or_create_user(
        db, payload.telegram_user_id, payload.telegram_user_id in settings.admin_ids, settings.default_total_capital
    )
    try:
        position = add_position(db, user, payload.symbol, payload.lot, payload.average_cost, payload.stop_price)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"id": position.id, "symbol": position.symbol, "lot": position.lot}


@router.delete("/portfolio/positions/{position_id}")
def delete_position(position_id: int, telegram_user_id: int, db: Session = Depends(get_db_session)):
    settings = get_settings()
    user = get_or_create_user(db, telegram_user_id, telegram_user_id in settings.admin_ids, settings.default_total_capital)
    try:
        remove_position(db, user, position_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "deleted", "id": position_id}
