from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from app.config.settings import Settings, get_strategy_config
from app.data.base_provider import BaseMarketDataProvider
from app.models.database import Scan, ScanResult, WatchlistItem
from app.services.analysis_service_v3 import AnalysisOutcomeV3, AnalysisUnavailableErrorV3, run_symbol_analysis_v3
from app.services.watchlist_service import is_any_kill_switch_active

logger = logging.getLogger("mergen_quant.scan")


class ScanBlockedByKillSwitchError(Exception):
    """Kill switch aktifken tarama denenirse firlatilir."""


@dataclass
class ScanSummary:
    scan_id: Optional[int]
    symbols_scanned: int
    symbols_succeeded: int
    symbols_failed: int
    top_candidates: list[tuple[str, AnalysisOutcomeV3]] = field(default_factory=list)
    top_risks: list[tuple[str, AnalysisOutcomeV3]] = field(default_factory=list)
    failed_symbols: list[tuple[str, str]] = field(default_factory=list)
    market_regime: Optional[str] = None


def get_distinct_watchlist_symbols(db: Session) -> list[str]:
    rows = db.query(WatchlistItem.symbol).distinct().all()
    return sorted({r[0] for r in rows})


def run_evening_scan(
    db: Session,
    provider: BaseMarketDataProvider,
    settings: Settings,
    symbols: Optional[list[str]] = None,
    rate_limit_seconds: float = 0.0,
    top_n: int = 5,
    persist: bool = True,
) -> ScanSummary:
    """Izleme listesindeki (veya verilen) sembolleri sirayla tarar.

    Tek bir sembol hata verirse (veri yok, kalite sorunu, vb.) TUM tarama
    durmaz; o sembol 'veri alinamayan' listesine eklenir ve tarama devam eder.
    """
    if is_any_kill_switch_active(db):
        raise ScanBlockedByKillSwitchError(
            "Kill switch aktif oldugu icin tarama baslatilamadi. /devam_et ile kapatabilirsin."
        )

    strategy_config = get_strategy_config()
    symbols = symbols if symbols is not None else get_distinct_watchlist_symbols(db)

    scan_record: Optional[Scan] = None
    if persist:
        scan_record = Scan(scan_type="evening", symbols_scanned=len(symbols), status="RUNNING")
        db.add(scan_record)
        db.flush()

    results: list[tuple[str, AnalysisOutcomeV3]] = []
    failed: list[tuple[str, str]] = []
    market_regime: Optional[str] = None

    for symbol in symbols:
        try:
            outcome = run_symbol_analysis_v3(db, provider, symbol, settings, strategy_config)
            try:
                from app.services.target_tracking_service import update_target_records

                live_price = outcome.signal.extras.get("current_price") or outcome.signal.extras.get("close")
                if live_price is not None:
                    update_target_records(
                        db, symbol, bar_high=float(live_price), bar_low=float(live_price),
                        bar_close=float(live_price),
                        timestamp=(outcome.signal.extras.get("current_price_timestamp") or outcome.signal.data_timestamp),
                    )
            except Exception as exc:  # noqa: BLE001 - hedef takibi taramayı durdurmaz
                logger.warning("Hedef takibi güncellenemedi symbol=%s: %s", symbol, exc)
            results.append((symbol, outcome))
            if market_regime is None:
                market_regime = outcome.signal.market_regime
            if scan_record is not None:
                db.add(
                    ScanResult(
                        scan_id=scan_record.id,
                        symbol=symbol,
                        score=outcome.advanced_score.total,
                        signal_type=outcome.signal.signal_type,
                        data_available=True,
                    )
                )
        except AnalysisUnavailableErrorV3 as exc:
            logger.info("Tarama: %s icin veri alinamadi: %s", symbol, exc)
            failed.append((symbol, str(exc)))
            if scan_record is not None:
                db.add(
                    ScanResult(
                        scan_id=scan_record.id,
                        symbol=symbol,
                        data_available=False,
                        error_detail=str(exc),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - tek sembol hatasi TUM taramayi durdurmamali
            logger.warning("Tarama: %s icin beklenmeyen hata: %s", symbol, exc)
            failed.append((symbol, f"Beklenmeyen hata: {exc}"))
            if scan_record is not None:
                db.add(
                    ScanResult(
                        scan_id=scan_record.id,
                        symbol=symbol,
                        data_available=False,
                        error_detail=str(exc),
                    )
                )
        if rate_limit_seconds > 0:
            time.sleep(rate_limit_seconds)

    results.sort(key=lambda pair: pair[1].advanced_score.total, reverse=True)
    top_candidates = [r for r in results if r[1].signal.signal_type in ("STRONG_BUY_CANDIDATE", "BUY_CANDIDATE", "WATCH")][:top_n]

    risky = sorted(
        [r for r in results if r[1].signal.signal_type in ("REDUCE_POSITION", "STRONG_RISK", "WEAK_RISK")],
        key=lambda pair: pair[1].advanced_score.total,
    )[:top_n]

    if scan_record is not None:
        scan_record.status = "COMPLETED"
        scan_record.symbols_succeeded = len(results)
        scan_record.symbols_failed = len(failed)
        scan_record.market_regime = market_regime
        from app.models.database import utcnow

        scan_record.finished_at = utcnow()
        db.commit()

    return ScanSummary(
        scan_id=scan_record.id if scan_record else None,
        symbols_scanned=len(symbols),
        symbols_succeeded=len(results),
        symbols_failed=len(failed),
        top_candidates=top_candidates,
        top_risks=risky,
        failed_symbols=failed,
        market_regime=market_regime,
    )
