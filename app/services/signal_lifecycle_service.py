from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.data.base_provider import BaseMarketDataProvider, DataUnavailableError
from app.models.database import Signal, SignalEvent, SignalStateEnum

logger = logging.getLogger("mergen_quant.signal_lifecycle")

_OPEN_STATES = (
    SignalStateEnum.CREATED,
    SignalStateEnum.WAITING_TRIGGER,
    SignalStateEnum.WAITING_CONFIRMATION,
    SignalStateEnum.CONFIRMED,
    SignalStateEnum.SENT,
    SignalStateEnum.ACTIVE,
    SignalStateEnum.TARGET_1_HIT,
    SignalStateEnum.TARGET_2_HIT,
)


def _record_event(db: Session, signal: Signal, to_state: SignalStateEnum, price: Optional[float], note: str) -> None:
    from_state = signal.state
    signal.state = to_state
    db.add(
        SignalEvent(
            signal_id=signal.id,
            from_state=from_state.value if from_state else None,
            to_state=to_state.value,
            price_at_event=price,
            trading_date=datetime.now(timezone.utc),
            note=note,
        )
    )


def update_open_signals(
    db: Session,
    provider: BaseMarketDataProvider,
    conservative_execution: bool = True,
    expiry_trading_days: int = 10,
) -> dict:
    """Acik (henuz sonuclanmamis) tum sinyalleri son fiyat verisiyle kontrol
    eder; stop/hedef gerceklesmislerse durumu gunceller. Gecmise donuk
    seviyeler (stop/hedef fiyatlari) DEGISTIRILMEZ, yalnizca durum ilerletilir.

    Ayni gunde hem stop hem hedef gorulduyse (gun ici sira belirsizse)
    conservative_execution=True oldugunda STOP oncelikli sayilir (daha
    temkinli/kotumser sonuc secilir).
    """
    # This is the legacy aggregate signal tracker. User-owned signals belong
    # exclusively to BistSignalRuntimeService; processing them here would skip
    # PENDING_ENTRY execution and could write legacy TARGET_* states over the
    # new deterministic lifecycle.
    open_signals = db.query(Signal).filter(
        Signal.user_id.is_(None),
        Signal.state.in_(_OPEN_STATES),
    ).all()
    updated = 0
    expired = 0
    errors = 0

    for sig in open_signals:
        try:
            end = datetime.now(timezone.utc)
            start = sig.trading_date or sig.created_at
            df = provider.get_ohlcv(sig.symbol, sig.timeframe, start, end)
        except DataUnavailableError as exc:
            logger.warning("Sinyal guncellenemedi (veri yok) symbol=%s: %s", sig.symbol, exc)
            errors += 1
            continue

        if df.empty:
            continue

        # Sinyalin olusturuldugu gunden SONRAKI mumlari incele (bakis onune
        # gecmis veri kullanilmaz; yalnizca ileri donuk gerceklesmeler kontrol edilir).
        df = df.sort_values("timestamp")
        reference_ts = sig.data_timestamp
        if reference_ts is not None and reference_ts.tzinfo is None:
            reference_ts = reference_ts.replace(tzinfo=timezone.utc)
        future_bars = df[df["timestamp"] > reference_ts] if reference_ts is not None else df

        # Sinyal zaten bir onceki hedefi gecmisse, yalnizca SONRAKI hedefleri
        # (ve stop'u) kontrol et; ayni hedefi sonsuza kadar yeniden tespit etmeyi onle.
        already_hit_rank = {
            SignalStateEnum.TARGET_1_HIT: 1,
            SignalStateEnum.TARGET_2_HIT: 2,
            SignalStateEnum.TARGET_3_HIT: 3,
        }.get(sig.state, 0)

        hit_target = None
        hit_stop = False
        for _, bar in future_bars.iterrows():
            stop_hit_today = sig.stop_price is not None and bar["low"] <= sig.stop_price
            target_hits_today = []
            if already_hit_rank < 1 and sig.target_1 is not None and bar["high"] >= sig.target_1:
                target_hits_today.append((SignalStateEnum.TARGET_1_HIT, sig.target_1))
            if already_hit_rank < 2 and sig.target_2 is not None and bar["high"] >= sig.target_2:
                target_hits_today.append((SignalStateEnum.TARGET_2_HIT, sig.target_2))
            if already_hit_rank < 3 and sig.target_3 is not None and bar["high"] >= sig.target_3:
                target_hits_today.append((SignalStateEnum.TARGET_3_HIT, sig.target_3))

            if stop_hit_today and target_hits_today:
                # Ayni mumda hem stop hem hedef: muhafazakar yontem STOP secer.
                if conservative_execution:
                    hit_stop = True
                else:
                    hit_target = max(target_hits_today, key=lambda t: t[1])
                break
            elif stop_hit_today:
                hit_stop = True
                break
            elif target_hits_today:
                hit_target = max(target_hits_today, key=lambda t: t[1])
                break
            # Bu barda hicbir sey gerceklesmedi; siradaki bara bak.

        if hit_stop:
            _record_event(db, sig, SignalStateEnum.STOP_HIT, sig.stop_price, "Stop seviyesi gerceklesti.")
            updated += 1
        elif hit_target is not None:
            state, price = hit_target
            _record_event(db, sig, state, price, f"{state.value} gerceklesti.")
            updated += 1
        elif expiry_trading_days > 0 and len(future_bars) >= expiry_trading_days and sig.state in (
            SignalStateEnum.CREATED, SignalStateEnum.WAITING_TRIGGER
        ):
            _record_event(
                db, sig, SignalStateEnum.EXPIRED, None,
                f"{expiry_trading_days} islem gunu icinde tetiklenmedi."
            )
            expired += 1

    db.commit()
    return {"checked": len(open_signals), "updated": updated, "expired": expired, "errors": errors}
