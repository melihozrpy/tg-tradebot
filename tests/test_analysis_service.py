from __future__ import annotations

import pytest

from app.config.settings import get_strategy_config
from app.services.analysis_service import run_symbol_analysis
from app.services.watchlist_service import (
    InvalidSymbolError,
    SymbolAlreadyExistsError,
    add_symbol,
    get_or_create_user,
    normalize_symbol,
    remove_symbol,
)


def test_normalize_symbol_valid():
    assert normalize_symbol(" thyao ") == "THYAO"


def test_normalize_symbol_invalid():
    with pytest.raises(InvalidSymbolError):
        normalize_symbol("th1")


def test_watchlist_add_remove(db_session):
    user = get_or_create_user(db_session, telegram_user_id=1, is_admin=False, default_capital=100_000)
    item = add_symbol(db_session, user, "THYAO")
    assert item.symbol == "THYAO"

    with pytest.raises(SymbolAlreadyExistsError):
        add_symbol(db_session, user, "THYAO")

    remove_symbol(db_session, user, "THYAO")


def test_analysis_idempotency_blocks_duplicate(db_session, mock_provider, strategy_config):
    outcome1 = run_symbol_analysis(db_session, mock_provider, "THYAO", "1d", strategy_config)
    assert outcome1.is_new_signal is True

    outcome2 = run_symbol_analysis(db_session, mock_provider, "THYAO", "1d", strategy_config)
    # Ayni veri zamani ve sinyal turunde ikinci cagri -> cooldown/idempotency ile engellenir
    assert outcome2.is_cooldown_blocked is True


def test_strategy_config_has_required_sections():
    cfg = get_strategy_config()
    for section in ["strategy", "timeframes", "thresholds", "risk", "filters"]:
        assert section in cfg
