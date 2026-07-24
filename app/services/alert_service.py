from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.database import AlertEvent, PriceAlert, User

VALID_ALERT_TYPES = {"fiyat", "ust", "alt", "hacim", "skor", "skor_altinda", "sinyal", "rejim", "rg", "anomali"}


class InvalidAlertError(Exception):
    pass


def create_alert(
    db: Session,
    user: User,
    symbol: str,
    alert_type: str,
    threshold_value: Optional[float] = None,
    threshold_text: Optional[str] = None,
    cooldown_minutes: int = 120,
) -> PriceAlert:
    alert_type = alert_type.lower()
    if alert_type not in VALID_ALERT_TYPES:
        raise InvalidAlertError(f"Gecersiz alarm turu: {alert_type}. Gecerli turler: {sorted(VALID_ALERT_TYPES)}")

    alert = PriceAlert(
        user_id=user.id,
        symbol=symbol.upper(),
        alert_type=alert_type,
        threshold_value=threshold_value,
        threshold_text=threshold_text,
        cooldown_minutes=cooldown_minutes,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert


def delete_alert(db: Session, user: User, alert_id: int) -> None:
    alert = db.query(PriceAlert).filter(PriceAlert.id == alert_id, PriceAlert.user_id == user.id).first()
    if alert is None:
        raise InvalidAlertError(f"Alarm bulunamadi: {alert_id}")
    db.delete(alert)
    db.commit()


def list_alerts(db: Session, user: User) -> list[PriceAlert]:
    return db.query(PriceAlert).filter(PriceAlert.user_id == user.id, PriceAlert.is_active.is_(True)).all()


def _in_cooldown(alert: PriceAlert) -> bool:
    if alert.last_triggered_at is None:
        return False
    last_triggered = alert.last_triggered_at
    if last_triggered.tzinfo is None:
        last_triggered = last_triggered.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - last_triggered).total_seconds() / 60
    return elapsed < alert.cooldown_minutes


def evaluate_alert(
    db: Session,
    alert: PriceAlert,
    current_price: Optional[float] = None,
    relative_volume: Optional[float] = None,
    score: Optional[float] = None,
    signal_type: Optional[str] = None,
    market_regime: Optional[str] = None,
    anomaly_type: Optional[str] = None,
    anomaly_description: Optional[str] = None,
) -> Optional[str]:
    """Bir alarmi mevcut degerlerle degerlendirir. Tetiklendiyse (ve
    cooldown/idempotency engellemiyorsa) mesaji doner ve last_triggered_at'i
    gunceller; aksi halde None doner. Ayni alarm cooldown suresi icinde
    tekrar tekrar gonderilmez.
    """
    if _in_cooldown(alert):
        return None

    triggered = False
    message = None

    if alert.alert_type == "fiyat" and current_price is not None and alert.threshold_value is not None:
        tolerance = max(alert.threshold_value * 0.004, 0.02)
        if abs(current_price - alert.threshold_value) <= tolerance:
            triggered = True
            message = (
                f"🔔 ALARM ÇALIYOR — {alert.symbol}\n\n"
                f"İstenilen fiyata geldi.\n"
                f"Hedef: {alert.threshold_value:.2f} TL\n"
                f"Güncel: {current_price:.2f} TL"
            )
    elif alert.alert_type == "ust" and current_price is not None and alert.threshold_value is not None:
        if current_price >= alert.threshold_value:
            triggered = True
            message = f"{alert.symbol}: fiyat {alert.threshold_value} direnc seviyesinin ustunde kapandi ({current_price})."
    elif alert.alert_type == "alt" and current_price is not None and alert.threshold_value is not None:
        if current_price <= alert.threshold_value:
            triggered = True
            message = f"{alert.symbol}: fiyat {alert.threshold_value} destek seviyesinin altinda kapandi ({current_price})."
    elif alert.alert_type == "hacim" and relative_volume is not None and alert.threshold_value is not None:
        if relative_volume >= alert.threshold_value:
            triggered = True
            message = f"{alert.symbol}: hacim ortalamanin {alert.threshold_value}x uzerinde ({relative_volume:.2f}x)."
    elif alert.alert_type == "skor" and score is not None and alert.threshold_value is not None:
        if score >= alert.threshold_value:
            triggered = True
            message = f"{alert.symbol}: skor {alert.threshold_value} ustune cikti ({score})."
    elif alert.alert_type == "skor_altinda" and score is not None and alert.threshold_value is not None:
        if score <= alert.threshold_value:
            triggered = True
            message = f"{alert.symbol}: skor {alert.threshold_value} altina indi ({score})."
    elif alert.alert_type == "sinyal" and signal_type is not None and alert.threshold_text is not None:
        if signal_type.lower() == alert.threshold_text.lower():
            triggered = True
            message = f"{alert.symbol}: sinyal turu degisti -> {signal_type}."
    elif alert.alert_type == "rejim" and market_regime is not None and alert.threshold_text is not None:
        if market_regime == alert.threshold_text:
            triggered = True
            message = f"{alert.symbol}: piyasa rejimi '{market_regime}' oldu."
    elif alert.alert_type == "anomali" and anomaly_type is not None:
        # threshold_text bos ise HER anomali turu tetikler; doluysa yalnizca o tur.
        if not alert.threshold_text or alert.threshold_text.lower() == anomaly_type.lower():
            triggered = True
            message = f"{alert.symbol}: anormal hareket tespit edildi -> {anomaly_description or anomaly_type}."

    if not triggered:
        return None

    alert.last_triggered_at = datetime.now(timezone.utc)
    db.add(AlertEvent(price_alert_id=alert.id, triggered_value=current_price or score, message=message))
    db.commit()
    return message
