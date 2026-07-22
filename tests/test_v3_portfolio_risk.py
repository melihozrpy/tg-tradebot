from __future__ import annotations

from app.models.database import User
from app.services.portfolio_service import add_position, portfolio_risk_summary
from app.services.sector_service import set_sector_mapping
import tempfile
from pathlib import Path


def _user(db):
    u = User(telegram_user_id=99, total_capital=100000.0)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_sector_concentration_detected(db_session, monkeypatch):
    from app.config import settings as settings_module

    tmp_path = Path(tempfile.mkdtemp()) / "sector_map_risk_test.yaml"
    monkeypatch.setattr(settings_module.get_settings(), "sector_map_path", str(tmp_path))

    set_sector_mapping("AAAA", "XTEST.IS", "Test Sektoru")
    set_sector_mapping("BBBB", "XTEST.IS", "Test Sektoru")
    set_sector_mapping("CCCC", "XOTHER.IS", "Diger Sektor")

    user = _user(db_session)
    add_position(db_session, user, "AAAA", lot=1000, average_cost=10.0)
    add_position(db_session, user, "BBBB", lot=1000, average_cost=10.0)
    add_position(db_session, user, "CCCC", lot=200, average_cost=10.0)

    risk = portfolio_risk_summary(db_session, user, current_prices={})
    max_sector, max_pct = risk["max_sector_concentration"]
    assert max_sector == "Test Sektoru"
    assert max_pct > 50.0  # AAAA+BBBB ayni sektorde, toplam degerin cogunlugu


def test_worst_case_stop_loss_calculated(db_session):
    user = _user(db_session)
    add_position(db_session, user, "DDDD", lot=100, average_cost=50.0, stop_price=45.0)
    risk = portfolio_risk_summary(db_session, user, current_prices={"DDDD": 50.0})
    assert risk["worst_case_stop_loss"] < 0  # stop'a giderse zarar negatif olmali
