from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.models.database import User, WatchlistItem

SYMBOL_PATTERN = re.compile(r"^[A-Z]{3,6}$")


class InvalidSymbolError(Exception):
    pass


class SymbolAlreadyExistsError(Exception):
    pass


class SymbolNotFoundError(Exception):
    pass


def normalize_symbol(raw_symbol: str) -> str:
    symbol = raw_symbol.strip().upper()
    if not SYMBOL_PATTERN.match(symbol):
        raise InvalidSymbolError(
            f"'{raw_symbol}' gecerli bir BIST sembolu formatinda degil (3-6 buyuk harf bekleniyor, orn: THYAO)."
        )
    return symbol


def get_or_create_user(db: Session, telegram_user_id: int, is_admin: bool, default_capital: float) -> User:
    user = db.query(User).filter(User.telegram_user_id == telegram_user_id).first()
    if user is None:
        user = User(
            telegram_user_id=telegram_user_id,
            is_admin=is_admin,
            total_capital=default_capital,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def add_symbol(db: Session, user: User, raw_symbol: str) -> WatchlistItem:
    symbol = normalize_symbol(raw_symbol)
    existing = (
        db.query(WatchlistItem)
        .filter(WatchlistItem.user_id == user.id, WatchlistItem.symbol == symbol)
        .first()
    )
    if existing is not None:
        raise SymbolAlreadyExistsError(f"'{symbol}' zaten izleme listende.")

    item = WatchlistItem(user_id=user.id, symbol=symbol)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def remove_symbol(db: Session, user: User, raw_symbol: str) -> None:
    symbol = normalize_symbol(raw_symbol)
    item = (
        db.query(WatchlistItem)
        .filter(WatchlistItem.user_id == user.id, WatchlistItem.symbol == symbol)
        .first()
    )
    if item is None:
        raise SymbolNotFoundError(f"'{symbol}' izleme listende bulunamadi.")
    db.delete(item)
    db.commit()


def list_symbols(db: Session, user: User) -> list[WatchlistItem]:
    return (
        db.query(WatchlistItem)
        .filter(WatchlistItem.user_id == user.id)
        .order_by(WatchlistItem.symbol)
        .all()
    )


def is_any_kill_switch_active(db: Session) -> bool:
    """Herhangi bir kullanicinin kill switch'i aktifse True doner.

    Otomatik surecler (aksam taramasi, alarm kontrolu gibi kullanici
    etkilesimi olmayan islemler) bu kontrolu kullanarak calismayi
    durdurmalidir; boylece kill switch yalnizca Telegram komutlarinda degil,
    zamanlanmis (scheduled) islerde de etkili olur.
    """
    return db.query(User).filter(User.kill_switch_active.is_(True)).count() > 0
