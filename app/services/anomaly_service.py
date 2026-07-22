from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.analysis.anomaly_engine import AnomalyDetectionResult, AnomalyEvent, detect_anomalies
from app.analysis.indicator_engine import InsufficientDataError, compute_technical_snapshot
from app.analysis.support_resistance_engine import compute_support_resistance
from app.data.base_provider import BaseMarketDataProvider, DataUnavailableError
from app.models.database import Anomaly

logger = logging.getLogger("mergen_quant.anomaly")

# Ayni sembol+tur icin kisa surede tekrar tekrar kayit acilmasini engellemek
# amaciyla, bu sure icinde ayni tur zaten kayitliyse yeni bir satir eklenmez.
DEDUPLICATION_WINDOW_HOURS = 6


class AnomalyDetectionUnavailableError(Exception):
    """Anomali tespiti icin yeterli/guvenilir veri yoksa firlatilir."""


@dataclass
class SymbolAnomalyOutcome:
    result: AnomalyDetectionResult
    new_anomalies: list[Anomaly]


def run_symbol_anomaly_scan(
    db: Session,
    provider: BaseMarketDataProvider,
    symbol: str,
    timeframe: str = "1d",
    lookback_days: int = 260,
    persist: bool = True,
) -> SymbolAnomalyOutcome:
    """Bir sembol icin anomali tespiti calistirir ve (persist=True ise) yeni
    tespit edilen olaylari veritabanina kaydeder. Mock/uydurma veri KESINLIKLE
    kullanilmaz; veri alinamazsa AnomalyDetectionUnavailableError firlatilir."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    try:
        df = provider.get_ohlcv(symbol, timeframe, start, end)
    except DataUnavailableError as exc:
        raise AnomalyDetectionUnavailableError(f"Veri alınamadı: {exc}") from exc

    sr_result = None
    try:
        snapshot = compute_technical_snapshot(df, symbol, timeframe)
        sr_result = compute_support_resistance(df, snapshot.close, snapshot.ema20, snapshot.ema50, snapshot.atr)
    except InsufficientDataError:
        sr_result = None

    try:
        corporate_actions = provider.get_corporate_actions(symbol)
    except Exception:  # noqa: BLE001 - aksiyon verisi yoksa eski davranış korunur
        corporate_actions = []
    result = detect_anomalies(
        df, symbol, timeframe=timeframe, sr_result=sr_result,
        corporate_actions=corporate_actions,
    )

    new_rows: list[Anomaly] = []
    if persist and result.available:
        for event in result.events:
            if _is_duplicate(db, symbol, timeframe, event):
                continue
            row = Anomaly(
                symbol=symbol,
                timeframe=timeframe,
                anomaly_type=event.anomaly_type,
                severity=event.severity,
                description=event.description,
                value=event.value,
                price_at_detection=event.price,
            )
            db.add(row)
            new_rows.append(row)
        if new_rows:
            db.commit()
            for row in new_rows:
                db.refresh(row)

    return SymbolAnomalyOutcome(result=result, new_anomalies=new_rows)


def _is_duplicate(db: Session, symbol: str, timeframe: str, event: AnomalyEvent) -> bool:
    since = datetime.now(timezone.utc) - timedelta(hours=DEDUPLICATION_WINDOW_HOURS)
    existing = (
        db.query(Anomaly)
        .filter(
            Anomaly.symbol == symbol,
            Anomaly.timeframe == timeframe,
            Anomaly.anomaly_type == event.anomaly_type,
            Anomaly.detected_at >= since,
        )
        .first()
    )
    return existing is not None


def list_recent_anomalies(
    db: Session, symbols: Optional[list[str]] = None, since_hours: int = 48, limit: int = 30
) -> list[Anomaly]:
    """Son N saat icindeki (varsa belirli semboller icin) anomali kayitlarini,
    en yeniden en eskiye dogru doner."""
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    query = db.query(Anomaly).filter(Anomaly.detected_at >= since)
    if symbols:
        query = query.filter(Anomaly.symbol.in_([s.upper() for s in symbols]))
    return query.order_by(Anomaly.detected_at.desc()).limit(limit).all()
