from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.database import User
from app.services.alert_service import InvalidAlertError, create_alert, evaluate_alert


def _user(db):
    u = User(telegram_user_id=42, total_capital=100000.0)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_invalid_alert_type_rejected(db_session):
    user = _user(db_session)
    with pytest.raises(InvalidAlertError):
        create_alert(db_session, user, "SVGYO", "gecersiz_tur", 10.0)


def test_alert_triggers_when_threshold_crossed(db_session):
    user = _user(db_session)
    alert = create_alert(db_session, user, "SVGYO", "ust", threshold_value=14.25)
    message = evaluate_alert(db_session, alert, current_price=14.30)
    assert message is not None
    assert "14.25" in message


def test_alert_does_not_trigger_when_threshold_not_crossed(db_session):
    user = _user(db_session)
    alert = create_alert(db_session, user, "SVGYO", "ust", threshold_value=14.25)
    message = evaluate_alert(db_session, alert, current_price=13.0)
    assert message is None


def test_alert_cooldown_prevents_repeated_trigger(db_session):
    user = _user(db_session)
    alert = create_alert(db_session, user, "SVGYO", "ust", threshold_value=14.25, cooldown_minutes=120)

    first = evaluate_alert(db_session, alert, current_price=14.30)
    assert first is not None

    # Ayni cooldown penceresinde tekrar tetiklenmemeli
    second = evaluate_alert(db_session, alert, current_price=14.50)
    assert second is None


def test_anomaly_alert_type_is_valid_and_triggers(db_session):
    user = _user(db_session)
    alert = create_alert(db_session, user, "THYAO", "anomali")
    message = evaluate_alert(
        db_session, alert, anomaly_type="hacim_patlamasi", anomaly_description="Hacim ortalamanin 5 kati"
    )
    assert message is not None
    assert "THYAO" in message


def test_anomaly_alert_with_specific_type_filter_ignores_other_types(db_session):
    user = _user(db_session)
    alert = create_alert(db_session, user, "THYAO", "anomali", threshold_text="gap_yukari")
    # Farkli bir anomali turu tetiklememeli.
    message = evaluate_alert(db_session, alert, anomaly_type="hacim_patlamasi")
    assert message is None
    # Ayni tur tetiklemeli.
    message2 = evaluate_alert(db_session, alert, anomaly_type="gap_yukari")
    assert message2 is not None


def test_alert_triggers_again_after_cooldown_expires(db_session):
    user = _user(db_session)
    alert = create_alert(db_session, user, "SVGYO", "ust", threshold_value=14.25, cooldown_minutes=60)
    evaluate_alert(db_session, alert, current_price=14.30)

    # Cooldown'u manuel olarak gecmis gibi ayarla
    alert.last_triggered_at = datetime.now(timezone.utc) - timedelta(minutes=61)
    db_session.commit()

    third = evaluate_alert(db_session, alert, current_price=14.40)
    assert third is not None
