from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

from app.analysis.bist_trade_plan import DirectionPlan, build_bist_trade_plan

logger = logging.getLogger("mergen_quant.services.instrument_universe")


@dataclass(frozen=True)
class EntryCandidate:
    symbol: str
    direction: str
    setup_score: int
    ranking_score: float
    entry_low: float
    entry_high: float
    stop: float
    target: float
    risk_reward: float
    entry_distance_percent: float
    status: str
    data_timestamp: str
    confirmations: tuple[str, ...]
    risks: tuple[str, ...]


@dataclass(frozen=True)
class UniverseEntryScanResult:
    generated_at: datetime
    symbols_requested: int
    symbols_succeeded: int
    symbols_failed: int
    candidates: tuple[EntryCandidate, ...]
    failures: tuple[tuple[str, str], ...]
    from_cache: bool = False


ProviderFactory = Callable[[], object]


def _direction_plan(plan, *, long_only: bool) -> tuple[str, DirectionPlan] | None:
    if plan.preferred_direction == "LONG":
        return "LONG", plan.long
    if plan.preferred_direction == "SHORT" and not long_only:
        return "SHORT", plan.short
    return None


def _target_at_two_r(direction: DirectionPlan) -> tuple[float, float]:
    for target, rr in zip(direction.targets, direction.risk_multiples):
        if rr >= 2.0:
            return float(target), float(rr)
    return float(direction.targets[-1]), float(direction.risk_multiples[-1])


def _scan_one(
    provider,
    symbol: str,
    *,
    end: datetime,
    minimum_score: int,
    long_only: bool,
) -> EntryCandidate | None:
    df = provider.get_ohlcv(symbol, "1d", end - timedelta(days=520), end)
    plan = build_bist_trade_plan(df, symbol)
    picked = _direction_plan(plan, long_only=long_only)
    if picked is None:
        return None
    direction_name, direction = picked
    if direction.score < minimum_score:
        return None
    entry_mid = (direction.entry_low + direction.entry_high) / 2.0
    distance = abs(plan.current_price - entry_mid) / max(plan.current_price, 1e-9) * 100.0
    target, rr = _target_at_two_r(direction)
    # Puanın kendisi ana ölçüdür. Uzak girişler ve fazla risk notu açıkça
    # cezalandırılır; RR 2'nin üzerindeyse küçük bir katkı alır.
    ranking = direction.score - min(20.0, distance * 2.5) - min(8.0, len(direction.risks) * 1.2)
    ranking += min(5.0, max(0.0, rr - 2.0) * 2.0)
    timestamp = plan.data_timestamp.isoformat() if hasattr(plan.data_timestamp, "isoformat") else str(plan.data_timestamp)
    return EntryCandidate(
        symbol=symbol,
        direction=direction_name,
        setup_score=direction.score,
        ranking_score=round(max(0.0, min(100.0, ranking)), 2),
        entry_low=direction.entry_low,
        entry_high=direction.entry_high,
        stop=direction.stop_standard,
        target=target,
        risk_reward=rr,
        entry_distance_percent=round(distance, 2),
        status=direction.status,
        data_timestamp=timestamp,
        confirmations=direction.confirmations,
        risks=direction.risks,
    )


def scan_best_entries(
    provider_factory: ProviderFactory,
    symbols: Sequence[str],
    *,
    top_n: int = 50,
    minimum_score: int = 68,
    max_workers: int = 3,
    long_only: bool = True,
    now: datetime | None = None,
) -> UniverseEntryScanResult:
    """Bir sembol hatasında durmayan, sağlayıcı başına worker izole tarama."""

    generated_at = now or datetime.now(timezone.utc)
    requested = list(dict.fromkeys(str(symbol).strip().upper().removesuffix(".IS") for symbol in symbols if str(symbol).strip()))
    local_state = threading.local()

    def run(symbol: str):
        if not hasattr(local_state, "provider"):
            local_state.provider = provider_factory()
        try:
            return symbol, _scan_one(
                local_state.provider,
                symbol,
                end=generated_at,
                minimum_score=minimum_score,
                long_only=long_only,
            ), None
        except Exception as exc:  # noqa: BLE001 - tek sembol tüm evreni bozmaz
            return symbol, None, f"{type(exc).__name__}: {str(exc)[:180]}"

    candidates: list[EntryCandidate] = []
    failures: list[tuple[str, str]] = []
    succeeded = 0
    with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as pool:
        futures = [pool.submit(run, symbol) for symbol in requested]
        for future in as_completed(futures):
            symbol, candidate, error = future.result()
            if error:
                failures.append((symbol, error))
                continue
            succeeded += 1
            if candidate is not None:
                candidates.append(candidate)
    candidates.sort(
        key=lambda item: (item.ranking_score, item.setup_score, -item.entry_distance_percent),
        reverse=True,
    )
    return UniverseEntryScanResult(
        generated_at=generated_at,
        symbols_requested=len(requested),
        symbols_succeeded=succeeded,
        symbols_failed=len(failures),
        candidates=tuple(candidates[: max(1, int(top_n))]),
        failures=tuple(sorted(failures)),
    )


def save_scan_cache(result: UniverseEntryScanResult, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": result.generated_at.isoformat(),
        "symbols_requested": result.symbols_requested,
        "symbols_succeeded": result.symbols_succeeded,
        "symbols_failed": result.symbols_failed,
        "candidates": [asdict(item) for item in result.candidates],
        "failures": list(result.failures),
    }
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def load_scan_cache(path: str | Path, *, max_age_minutes: int) -> UniverseEntryScanResult | None:
    target = Path(path)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        generated_at = datetime.fromisoformat(payload["generated_at"])
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - generated_at > timedelta(minutes=max_age_minutes):
            return None
        return UniverseEntryScanResult(
            generated_at=generated_at,
            symbols_requested=int(payload["symbols_requested"]),
            symbols_succeeded=int(payload["symbols_succeeded"]),
            symbols_failed=int(payload["symbols_failed"]),
            candidates=tuple(
                EntryCandidate(
                    **{
                        **item,
                        "confirmations": tuple(item.get("confirmations") or ()),
                        "risks": tuple(item.get("risks") or ()),
                    }
                )
                for item in payload.get("candidates", [])
            ),
            failures=tuple(tuple(item) for item in payload.get("failures", [])),
            from_cache=True,
        )
    except Exception as exc:  # noqa: BLE001 - bozuk cache sessizce yok sayılır
        logger.warning("Evren tarama cache'i okunamadı: %s", type(exc).__name__)
        return None
