from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import logging
import math
import re
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import case, func
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.backtest.strategy_adapter import ExistingSignalStrategyAdapter
from app.backtest.signal_replay import (
    SignalReplayError,
    build_replay_plan,
    format_signal_replay_report,
    replay_from_provider,
)
from app.backtest.universe import UniverseBacktestEngine, UniverseBacktestRequest
from app.config.settings import get_settings, get_strategy_config
from app.data.provider_factory import build_market_data_provider
from app.models.database import (
    BacktestMetric,
    BacktestRun,
    BacktestTrade,
    Signal,
    SignalTarget,
    User,
    WatchlistItem,
    get_session_factory,
)
from app.services.watchlist_service import get_or_create_user
from app.telegram.handlers import _reject_unauthorized
from app.telegram.handlers_stage5g import _backtest_config, cmd_backtest_ozet

logger = logging.getLogger("mergen_quant.telegram.ultra_backtest")

_ACTIVE_UNIVERSE_TASKS: dict[int, asyncio.Task] = {}
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{3,8}$")
_TIMEFRAME_ALIASES = {
    "5d": "5m",
    "5m": "5m",
    "15d": "15m",
    "15m": "15m",
    "1s": "1h",
    "1h": "1h",
    "1g": "1d",
    "1d": "1d",
    "1hf": "1wk",
    "1w": "1wk",
    "1wk": "1wk",
}
_INDEX_ALIASES = {
    "XU030": {"XU030", "BIST30", "BIST030"},
    "XU050": {"XU050", "BIST50", "BIST050"},
    "XU100": {"XU100", "BIST100"},
}


class UniverseCommandError(ValueError):
    pass


def _normalize_symbol(value: str) -> str:
    symbol = value.strip().upper().removesuffix(".IS")
    if not _SYMBOL_PATTERN.fullmatch(symbol):
        raise UniverseCommandError("Evren dosyasında geçersiz BIST sembolü bulundu.")
    return symbol


def _normalize_index(value: str) -> str:
    return value.strip().upper().replace(" ", "").replace("_", "").removesuffix(".IS")


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "evet", "aktif"}


def _parse_timeframe_period(values: list[str]) -> tuple[str, datetime, datetime]:
    if len(values) != 2:
        raise UniverseCommandError("Zaman dilimi ve dönem gerekli. Örnek: 1g 3y")
    timeframe = _TIMEFRAME_ALIASES.get(values[0].strip().casefold())
    if timeframe is None:
        raise UniverseCommandError("Zaman dilimi desteklenmiyor. Kullan: 5d, 15d, 1s, 1g veya 1hf.")
    match = re.fullmatch(r"(\d+)(g|d|y|a|w|hf)", values[1].strip().casefold())
    if match is None:
        raise UniverseCommandError("Dönem geçersiz. Örnek: 60g, 3y veya 6a.")
    amount = int(match.group(1))
    unit = match.group(2)
    multiplier = {"g": 1, "d": 1, "w": 7, "hf": 7, "a": 30, "y": 365}[unit]
    days = amount * multiplier
    if not 1 <= days <= 3650:
        raise UniverseCommandError("Backtest dönemi 1 gün ile 10 yıl arasında olmalı.")
    end = datetime.now(timezone.utc)
    return timeframe, end - timedelta(days=days), end


def _read_csv(path: Path) -> tuple[list[dict[str, str]], set[str]]:
    if not path.exists() or not path.is_file():
        raise UniverseCommandError(f"Gerekli evren dosyası bulunamadı: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = {str(name).strip().casefold() for name in (reader.fieldnames or [])}
            rows = [
                {str(key).strip().casefold(): str(value or "").strip() for key, value in row.items()}
                for row in reader
            ]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise UniverseCommandError("Evren dosyası güvenli biçimde okunamadı.") from exc
    if not rows:
        raise UniverseCommandError("Evren dosyası boş.")
    return rows, fieldnames


def _load_sector_symbols(path: Path, sector_code: str) -> list[str]:
    rows, fields = _read_csv(path)
    if not {"symbol", "sector_index"} <= fields:
        raise UniverseCommandError("Sektör evreni dosyasında symbol ve sector_index sütunları gerekli.")
    requested = _normalize_index(sector_code)
    symbols: list[str] = []
    for row in rows:
        if "active" in fields and not _truthy(row.get("active")):
            continue
        if _normalize_index(row.get("sector_index", "")) != requested:
            continue
        symbol = _normalize_symbol(row.get("symbol", ""))
        if symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        raise UniverseCommandError(f"{requested} için doğrulanmış aktif sektör üyesi bulunamadı.")
    return symbols


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise UniverseCommandError("Endeks üyelik dosyasında tarih formatı geçersiz.") from exc


def _load_index_symbols(path: Path, index_code: str, expected_count: int, *, as_of: date | None = None) -> list[str]:
    """Yalnız açıkça güncellik bilgisi taşıyan üyelik dosyasını kabul eder.

    Üye sembolleri kodda tutulmaz. Dosya yoksa, güncellik alanı eksikse veya
    beklenen üye sayısı tutmuyorsa komut fail-closed davranır.
    """
    rows, fields = _read_csv(path)
    index_field = next((name for name in ("index_code", "index", "index_name", "universe") if name in fields), None)
    if "symbol" not in fields or index_field is None:
        raise UniverseCommandError("Endeks üyelik dosyasında symbol ve index_code sütunları gerekli.")
    freshness_fields = {"active", "as_of", "as_of_date", "effective_from", "effective_to"}
    if not fields.intersection(freshness_fields):
        raise UniverseCommandError("Endeks üyelik dosyasının güncellik bilgisi yok; üyelik varsayılmadı.")

    canonical = _normalize_index(index_code)
    aliases = _INDEX_ALIASES.get(canonical, {canonical})
    cutoff = as_of or datetime.now(timezone.utc).date()
    symbols: list[str] = []
    freshness_evidence = False
    for row in rows:
        if _normalize_index(row.get(index_field, "")) not in aliases:
            continue
        if "active" in fields and row.get("active"):
            freshness_evidence = True
            if not _truthy(row.get("active")):
                continue
        effective_from = _parse_iso_date(row.get("effective_from"))
        effective_to = _parse_iso_date(row.get("effective_to"))
        snapshot_date = _parse_iso_date(row.get("as_of") or row.get("as_of_date"))
        freshness_evidence = freshness_evidence or any((effective_from, effective_to, snapshot_date))
        if effective_from and effective_from > cutoff:
            continue
        if effective_to and effective_to < cutoff:
            continue
        if snapshot_date and (snapshot_date > cutoff or (cutoff - snapshot_date).days > 120):
            continue
        symbol = _normalize_symbol(row.get("symbol", ""))
        if symbol not in symbols:
            symbols.append(symbol)
    if not freshness_evidence:
        raise UniverseCommandError("Endeks üyelik dosyasında doğrulanabilir güncellik değeri yok.")
    if len(symbols) != expected_count:
        raise UniverseCommandError(
            f"{canonical} üyelik dosyası tam değil veya güncel değil: "
            f"{len(symbols)}/{expected_count} doğrulanmış üye. Backtest başlatılmadı."
        )
    return symbols


def _index_membership_path(settings) -> Path:
    configured = getattr(settings, "bist_index_membership_csv_path", "")
    if configured:
        return Path(configured)
    return Path(settings.bist_symbols_csv_path).with_name("bist_index_membership.csv")


def _watchlist_symbols(db, user_id: int) -> list[str]:
    return [
        item.symbol
        for item in db.query(WatchlistItem)
        .filter(WatchlistItem.user_id == user_id)
        .order_by(WatchlistItem.symbol)
        .all()
    ]


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _reserve_run(
    db,
    *,
    user_id: int,
    label: str,
    scope: str,
    sector: str | None,
    timeframe: str,
    start: datetime,
    end: datetime,
    symbols: list[str],
    provider_name: str,
    strategy_version: str,
    settings,
) -> BacktestRun:
    active_count = db.query(BacktestRun).filter(
        BacktestRun.user_id == user_id,
        BacktestRun.run_status.in_(["PENDING", "RUNNING"]),
    ).count()
    limit = max(1, int(settings.backtest_max_concurrent_per_user))
    if active_count >= limit:
        raise UniverseCommandError("Aynı anda izin verilen ağır backtest sınırına ulaşıldı.")
    config = _backtest_config()
    costs = config.transaction_costs
    snapshot = config.snapshot()
    snapshot.update({"universe_symbols": symbols, "universe_scope": scope, "membership_mode": "current_snapshot"})
    record = BacktestRun(
        run_id="btu_" + uuid.uuid4().hex[:24],
        user_id=user_id,
        symbol=label[:16],
        timeframe=timeframe,
        start_date=start,
        end_date=end,
        initial_capital=config.initial_capital,
        commission_percent=costs.commission_bps / 100.0,
        slippage_percent=costs.slippage_bps / 100.0,
        strategy_version=str(strategy_version)[:16],
        strategy_name="existing_signal_engine_universe",
        config_snapshot=json.dumps(_json_safe(snapshot), ensure_ascii=False, sort_keys=True),
        transaction_cost_config=json.dumps(asdict(costs), sort_keys=True),
        provider=provider_name,
        price_adjustment_mode=config.price_adjustment_mode,
        scope=scope,
        sector=sector,
        seed=config.seed,
        status="PENDING",
        run_status="PENDING",
        progress_percent=0.0,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def _mark_failed(run_id: str, user_id: int, detail: str) -> None:
    db = get_session_factory()()
    try:
        record = db.query(BacktestRun).filter_by(run_id=run_id, user_id=user_id).one_or_none()
        if record is not None and record.run_status in {"PENDING", "RUNNING"}:
            record.status = record.run_status = "FAILED"
            record.error_detail = detail[:1000]
            record.finished_at = datetime.now(timezone.utc)
            record.updated_at = record.finished_at
            db.commit()
    finally:
        db.close()


def _aggregate_metrics(result, membership_warning: str | None) -> tuple[dict, list]:
    successful = list(result.results.values())
    trades = [trade for item in successful for trade in item.trades]
    pnl_values = [float(item.net_pnl) for item in trades]
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    symbol_returns = [float(item.metrics.total_return_percent) for item in successful]

    def rate(attribute: str) -> float:
        return round(sum(bool(getattr(item, attribute, False)) for item in trades) / len(trades) * 100, 2) if trades else 0.0

    metrics = {
        "aggregation": "equal_weight_per_symbol_independent_runs",
        "symbols_requested": len(result.request.symbols),
        "symbols_completed": len(successful),
        "symbols_failed": len(result.failures),
        "failed_symbols": sorted(result.failures),
        "trade_count": len(trades),
        "total_return_percent": round(sum(symbol_returns) / len(symbol_returns), 2) if symbol_returns else 0.0,
        "aggregate_net_pnl": round(sum(pnl_values), 2),
        "aggregate_gross_pnl": round(sum(float(item.gross_pnl) for item in trades), 2),
        "total_cost": round(sum(float(item.total_cost) for item in trades), 2),
        "win_rate_percent": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else (None if not gross_profit else "sonsuz"),
        "max_drawdown_percent": min((float(item.metrics.max_drawdown_percent) for item in successful), default=0.0),
        "target_1_hit_rate_percent": rate("target_1_hit"),
        "target_2_hit_rate_percent": rate("target_2_hit"),
        "target_3_hit_rate_percent": rate("target_3_hit"),
        "stop_rate_percent": round(
            sum(str(item.exit_reason or "").upper() == "STOP" for item in trades) / len(trades) * 100,
            2,
        ) if trades else 0.0,
        "sample_warning": None if len(trades) >= 30 else "İstatistiksel değerlendirme için işlem sayısı yetersiz.",
        "membership_warning": membership_warning,
    }
    return metrics, trades


def _persist_universe_result(run_id: str, user_id: int, result, membership_warning: str | None) -> dict:
    db = get_session_factory()()
    try:
        record = db.query(BacktestRun).filter_by(run_id=run_id, user_id=user_id).one()
        metrics, trades = _aggregate_metrics(result, membership_warning)
        now = datetime.now(timezone.utc)
        if not result.results:
            record.status = record.run_status = "FAILED"
            record.progress_percent = 100.0
            record.error_detail = "Hiçbir sembol doğrulanabilir veriyle tamamlanamadı."
            record.metrics_json = json.dumps(metrics, ensure_ascii=False, sort_keys=True)
            record.finished_at = record.updated_at = now
            db.commit()
            return metrics

        record.status = record.run_status = "COMPLETED"
        record.progress_percent = 100.0
        record.metrics_json = json.dumps(_json_safe(metrics), ensure_ascii=False, sort_keys=True)
        versions = "|".join(sorted(item.data_version for item in result.results.values()))
        record.data_version = hashlib.sha256(versions.encode("utf-8")).hexdigest()[:24]
        record.error_detail = (
            f"Tamamlanamayan semboller: {', '.join(sorted(result.failures))}" if result.failures else None
        )
        record.finished_at = record.updated_at = now
        db.flush()
        for item in trades:
            notional = float(item.entry_price) * float(item.quantity)
            db.add(BacktestTrade(
                backtest_run_id=record.id,
                symbol=item.symbol,
                side="BUY",
                entry_time=item.entry_time,
                exit_time=item.exit_time,
                entry_price=item.entry_price,
                exit_price=item.exit_price,
                quantity=item.quantity,
                pnl=item.net_pnl,
                gross_pnl=item.gross_pnl,
                total_cost=item.total_cost,
                net_return_percent=(item.net_pnl / notional * 100 if notional else None),
                exit_reason=item.exit_reason,
                stop_price=item.stop_price,
                target_1=item.target_1,
                target_2=item.target_2,
                target_3=item.target_3,
                target_1_hit=item.target_1_hit,
                target_2_hit=item.target_2_hit,
                target_3_hit=item.target_3_hit,
                mae_percent=item.mae_percent,
                mfe_percent=item.mfe_percent,
                holding_bars=item.holding_bars,
                market_regime=item.market_regime,
                sector=item.sector or record.sector,
                signal_type=item.signal_type,
                raw_signal_score=item.raw_signal_score,
            ))
        db.add(BacktestMetric(
            backtest_run_id=record.id,
            scope_type=record.scope or "universe",
            scope_value=record.sector or record.symbol,
            metrics_json=record.metrics_json,
            sample_count=len(trades),
            evidence_class="DEGERLENDIRILEBILIR" if len(trades) >= 30 else "YETERSIZ_ORNEK",
        ))
        db.commit()
        return metrics
    finally:
        db.close()


def _run_universe_sync(
    *,
    run_id: str,
    user_id: int,
    engine: UniverseBacktestEngine,
    request: UniverseBacktestRequest,
    provider,
    timeframe: str,
    strategy_config: dict,
    timeout_seconds: int,
    membership_warning: str | None,
) -> dict:
    db = get_session_factory()()
    try:
        record = db.query(BacktestRun).filter_by(run_id=run_id, user_id=user_id).one()
        record.status = record.run_status = "RUNNING"
        record.started_at = record.updated_at = datetime.now(timezone.utc)
        record.progress_percent = 1.0
        db.commit()
    finally:
        db.close()

    def load(symbol: str, start: datetime, end: datetime):
        return provider.get_ohlcv(symbol, timeframe, start, end)

    def benchmark(symbol: str, start: datetime, end: datetime):
        try:
            return provider.get_ohlcv(symbol, timeframe, start, end)
        except Exception:  # benchmark yokluğu sembol sonuçlarını yok etmez
            return None

    result = engine.run(
        request,
        data_loader=load,
        benchmark_loader=benchmark,
        signal_provider_factory=lambda symbol: ExistingSignalStrategyAdapter(
            symbol=symbol,
            timeframe=timeframe,
            strategy_config=strategy_config,
            provider_name=provider.name,
        ),
        timeout_seconds=timeout_seconds,
    )
    return _persist_universe_result(run_id, user_id, result, membership_warning)


def _format_completion(label: str, run_id: str, metrics: dict) -> str:
    failed = int(metrics.get("symbols_failed", 0))
    lines = [
        f"🧪 {label} backtest tamamlandı",
        "",
        f"Sembol: {metrics.get('symbols_completed', 0)}/{metrics.get('symbols_requested', 0)} tamamlandı",
        f"İşlem: {metrics.get('trade_count', 0)}",
        f"Eşit ağırlıklı ortalama getiri: %{metrics.get('total_return_percent', 0):.2f}",
        f"Toplam simülasyon net K/Z: {metrics.get('aggregate_net_pnl', 0):.2f} TL",
        f"Kazanma oranı: %{metrics.get('win_rate_percent', 0):.1f}",
    ]
    if failed:
        lines.append(f"Tamamlanamayan sembol: {failed}; diğer sonuçlar korunarak raporlandı.")
    if metrics.get("sample_warning"):
        lines.append(str(metrics["sample_warning"]))
    if metrics.get("membership_warning"):
        lines.append(str(metrics["membership_warning"]))
    lines.extend(["", f"Run ID: {run_id}", "Geçmiş performans gelecek sonucu garanti etmez."])
    return "\n".join(lines)


async def _execute_universe_job(
    *,
    bot,
    chat_id: int,
    user_id: int,
    label: str,
    run_id: str,
    engine: UniverseBacktestEngine,
    request: UniverseBacktestRequest,
    provider,
    timeframe: str,
    strategy_config: dict,
    timeout_seconds: int,
    membership_warning: str | None,
) -> None:
    try:
        metrics = await asyncio.to_thread(
            _run_universe_sync,
            run_id=run_id,
            user_id=user_id,
            engine=engine,
            request=request,
            provider=provider,
            timeframe=timeframe,
            strategy_config=strategy_config,
            timeout_seconds=timeout_seconds,
            membership_warning=membership_warning,
        )
        await bot.send_message(chat_id=chat_id, text=_format_completion(label, run_id, metrics))
    except Exception as exc:  # ayrıntı log/DB'de, Telegram'da secret sızdırılmaz
        logger.exception("Evren backtest işi tamamlanamadı run_id=%s: %s", run_id, type(exc).__name__)
        await asyncio.to_thread(_mark_failed, run_id, user_id, f"{type(exc).__name__}: backtest işi başarısız")
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=f"❌ {label} backtest tamamlanamadı. Veriler uydurulmadı. Run ID: {run_id}",
            )
        except Exception:
            logger.warning("Backtest hata bildirimi Telegram'a gönderilemedi run_id=%s", run_id)


async def _start_universe(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    scope: str,
    label: str,
    sector_code: str | None = None,
    index_code: str | None = None,
    expected_index_count: int | None = None,
) -> None:
    if await _reject_unauthorized(update):
        return
    settings = get_settings()
    args = list(context.args)
    try:
        if sector_code is None and scope == "sector":
            if len(args) != 3:
                raise UniverseCommandError("Kullanım: /backtest_sector XBANK 1g 5y")
            sector_code = _normalize_index(args.pop(0))
            label = sector_code
        timeframe, start, end = _parse_timeframe_period(args)

        db = get_session_factory()()
        try:
            user = get_or_create_user(
                db,
                update.effective_user.id,
                update.effective_user.id in settings.admin_ids,
                settings.default_total_capital,
            )
            if user.kill_switch_active:
                raise UniverseCommandError("Acil durdurma açıkken yeni ağır backtest başlatılmaz.")
            if scope == "watchlist":
                symbols = _watchlist_symbols(db, user.id)
                if not symbols:
                    raise UniverseCommandError("İzleme listen boş; hiçbir sembol uydurulmadı.")
                membership_warning = None
            elif scope == "sector":
                symbols = _load_sector_symbols(Path(settings.bist_symbols_csv_path), sector_code or "")
                membership_warning = (
                    "ℹ️ Sektör kapsamı yalnız yapılandırılmış aktif sembol evrenindeki "
                    "doğrulanmış üyeleri içerir."
                )
            else:
                if index_code is None or expected_index_count is None:
                    raise UniverseCommandError("Endeks kapsamı eksik.")
                symbols = _load_index_symbols(
                    _index_membership_path(settings),
                    index_code,
                    expected_index_count,
                    as_of=end.date(),
                )
                membership_warning = (
                    "⚠️ Güncel üyelik anlık görüntüsü kullanıldı; dönemsel geçmiş üyelik yoksa "
                    "survivorship bias tamamen giderilemez."
                )
            max_symbols = min(100, max(1, int(getattr(settings, "backtest_universe_max_symbols", 100))))
            if len(symbols) > max_symbols:
                raise UniverseCommandError(
                    f"Evren {len(symbols)} sembol; güvenli koşu sınırı {max_symbols}. Liste sessizce kesilmedi."
                )
            if user.id in _ACTIVE_UNIVERSE_TASKS and not _ACTIVE_UNIVERSE_TASKS[user.id].done():
                raise UniverseCommandError("Bu kullanıcı için bir evren backtesti zaten çalışıyor.")
            provider = build_market_data_provider(settings)
            if getattr(settings, "market_data_provider", "") == "mock" or provider.name == "mock":
                raise UniverseCommandError(
                    "Evren backtesti mock veriyle çalıştırılmaz; gerçek CSV veya lisanslı/provider verisi gerekli."
                )
            strategy_config = get_strategy_config()
            record = _reserve_run(
                db,
                user_id=user.id,
                label=label,
                scope=scope,
                sector=sector_code,
                timeframe=timeframe,
                start=start,
                end=end,
                symbols=symbols,
                provider_name=provider.name,
                strategy_version=strategy_config["strategy"]["version"],
                settings=settings,
            )
            user_id = user.id
            run_id = record.run_id
        finally:
            db.close()
    except UniverseCommandError as exc:
        await update.message.reply_text(f"⚠️ {exc}")
        return
    except Exception as exc:
        logger.warning("Evren backtest hazırlığı başarısız: %s", type(exc).__name__)
        await update.message.reply_text("❌ Backtest hazırlanamadı; veri veya sağlayıcı ayarları doğrulanamadı.")
        return

    mapping = {symbol: sector_code or "" for symbol in symbols}
    engine = UniverseBacktestEngine(
        _backtest_config(),
        symbol_to_sector=mapping,
        bist_symbols_path=settings.bist_symbols_csv_path,
    )
    request = UniverseBacktestRequest(
        scope="all_bist" if index_code else scope,
        start_date=start,
        end_date=end,
        symbols=tuple(symbols),
        sector=sector_code,
    )
    task = asyncio.create_task(_execute_universe_job(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        user_id=user_id,
        label=label,
        run_id=run_id,
        engine=engine,
        request=request,
        provider=provider,
        timeframe=timeframe,
        strategy_config=strategy_config,
        timeout_seconds=max(1, int(settings.backtest_timeout_seconds)),
        membership_warning=membership_warning,
    ))
    _ACTIVE_UNIVERSE_TASKS[user_id] = task

    def cleanup(completed: asyncio.Task) -> None:
        if _ACTIVE_UNIVERSE_TASKS.get(user_id) is completed:
            _ACTIVE_UNIVERSE_TASKS.pop(user_id, None)

    task.add_done_callback(cleanup)
    await update.message.reply_text(
        f"🧪 {label} backtest arka planda başladı.\n"
        f"Sembol: {len(symbols)} | Zaman: {timeframe} | Run ID: {run_id}\n"
        "Bir sembolde veri hatası olursa diğerleri çalışmaya devam eder."
    )


async def cmd_backtest_signal_alias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if len(context.args) != 1:
        await update.message.reply_text(
            "Kullanım: /backtest_signal <sinyal_id>\n"
            "Örnek: /backtest_signal 42"
        )
        return
    try:
        signal_id = int(context.args[0])
        if signal_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        await update.message.reply_text("Sinyal ID pozitif bir tam sayı olmalı.")
        return

    db = get_session_factory()()
    try:
        user = db.query(User).filter(User.telegram_user_id == update.effective_user.id).one_or_none()
        source = db.query(Signal).filter(Signal.id == signal_id).one_or_none()
        # Do not reveal whether an inaccessible owned signal exists.
        if source is None or (source.user_id is not None and (user is None or source.user_id != user.id)):
            await update.message.reply_text("Sinyal bulunamadı veya bu sinyale erişim yetkin yok.")
            return
        targets = (
            db.query(SignalTarget)
            .filter(SignalTarget.signal_id == source.id)
            .order_by(SignalTarget.target_number)
            .all()
        )
        plan = build_replay_plan(source, targets)
    except SignalReplayError as exc:
        await update.message.reply_text(f"⚠️ Replay başlatılamadı: {exc}")
        return
    finally:
        db.close()

    try:
        settings = get_settings()
        provider = build_market_data_provider(settings)
        result = await asyncio.to_thread(replay_from_provider, plan, provider, settings)
    except SignalReplayError as exc:
        await update.message.reply_text(f"⚠️ Replay tamamlanamadı: {exc}")
        return
    except Exception as exc:  # provider errors are deliberately not echoed verbatim
        logger.warning("Sinyal tarihsel replay basarisiz: %s", type(exc).__name__)
        await update.message.reply_text(
            "❌ Tarihsel veri alınamadı veya doğrulanamadı. Sağlayıcı, zaman dilimi ve fiyat modu ayarlarını kontrol et."
        )
        return
    await update.message.reply_text(format_signal_replay_report(result))


async def cmd_backtest_history_alias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    await cmd_backtest_ozet(update, context)


async def cmd_backtest_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    db = get_session_factory()()
    try:
        settings = get_settings()
        user = get_or_create_user(
            db,
            update.effective_user.id,
            update.effective_user.id in settings.admin_ids,
            settings.default_total_capital,
        )
        run_query = db.query(BacktestRun).filter(BacktestRun.user_id == user.id)
        total_runs = run_query.count()
        if not total_runs:
            await update.message.reply_text("Henüz sana ait kayıtlı backtest yok.")
            return
        completed = run_query.filter(BacktestRun.run_status == "COMPLETED").count()
        failed = run_query.filter(BacktestRun.run_status.in_(["FAILED", "CANCELLED", "INTERRUPTED"])).count()
        trade_count, wins, net_pnl, gross_pnl, total_cost = db.query(
            func.count(BacktestTrade.id),
            func.coalesce(func.sum(case((BacktestTrade.pnl > 0, 1), else_=0)), 0),
            func.coalesce(func.sum(BacktestTrade.pnl), 0.0),
            func.coalesce(func.sum(BacktestTrade.gross_pnl), 0.0),
            func.coalesce(func.sum(BacktestTrade.total_cost), 0.0),
        ).join(BacktestRun, BacktestRun.id == BacktestTrade.backtest_run_id).filter(
            BacktestRun.user_id == user.id,
            BacktestTrade.pnl.is_not(None),
        ).one()
        trade_count = int(trade_count or 0)
        wins = int(wins or 0)
        win_rate = wins / trade_count * 100 if trade_count else 0.0
        await update.message.reply_text(
            "🧪 BACKTEST İSTATİSTİKLERİ\n\n"
            f"Koşu: {total_runs} | Tamamlandı: {completed} | Başarısız/iptal: {failed}\n"
            f"İşlem: {trade_count} | Kazanma oranı: %{win_rate:.1f}\n"
            f"Brüt simülasyon K/Z: {float(gross_pnl):.2f} TL\n"
            f"Toplam maliyet: {float(total_cost):.2f} TL\n"
            f"Net simülasyon K/Z: {float(net_pnl):.2f} TL\n\n"
            "Yalnız sana ait kayıtlar hesaplandı. Geçmiş performans gelecek sonucu garanti etmez."
        )
    finally:
        db.close()


async def cmd_backtest_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_universe(update, context, scope="watchlist", label="İzleme listesi")


async def cmd_backtest_sector(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_universe(update, context, scope="sector", label="Sektör")


async def cmd_backtest_bist30(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_universe(update, context, scope="index", label="BIST 30", index_code="XU030", expected_index_count=30)


async def cmd_backtest_bist50(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_universe(update, context, scope="index", label="BIST 50", index_code="XU050", expected_index_count=50)


async def cmd_backtest_bist100(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_universe(update, context, scope="index", label="BIST 100", index_code="XU100", expected_index_count=100)


def register_ultra_backtest_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("backtest_signal", cmd_backtest_signal_alias))
    application.add_handler(CommandHandler("backtest_gecmisi", cmd_backtest_history_alias))
    application.add_handler(CommandHandler("backtest_stats", cmd_backtest_stats))
    application.add_handler(CommandHandler("backtest_watchlist", cmd_backtest_watchlist))
    application.add_handler(CommandHandler("backtest_sector", cmd_backtest_sector))
    application.add_handler(CommandHandler("backtest_bist30", cmd_backtest_bist30))
    application.add_handler(CommandHandler("backtest_bist50", cmd_backtest_bist50))
    application.add_handler(CommandHandler("backtest_bist100", cmd_backtest_bist100))
