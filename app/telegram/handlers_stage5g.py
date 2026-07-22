from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.analysis.indicator_engine import MIN_BARS_FOR_FULL_ANALYSIS
from app.analysis.score_calibration_engine import DISCLAIMER
from app.backtest.engine_v5g import BacktestConfig, TransactionCostConfig
from app.backtest.strategy_adapter import ExistingSignalStrategyAdapter
from app.config.settings import get_settings, get_strategy_config
from app.data.provider_factory import build_market_data_provider
from app.execution.paper_trading_engine import PaperTradingEngine, PaperTradingError
from app.models.database import (
    BacktestDailyEquity, BacktestRun, BacktestTrade, ScoreCalibrationBin,
    ScoreCalibrationModel, Signal, SignalFeatureSnapshot, SignalOutcome,
    SignalScoreContribution,
    get_session_factory,
)
from app.services.backtest_chart_service import delete_backtest_chart, generate_persisted_equity_chart
from app.services.backtest_job_service import BacktestJobError, BacktestJobService
from app.services.current_price_service import resolve_current_price
from app.services.watchlist_service import get_or_create_user, normalize_symbol

logger = logging.getLogger("mergen_quant.telegram.stage5g")
_JOB_SERVICE: BacktestJobService | None = None


def _jobs() -> BacktestJobService:
    global _JOB_SERVICE
    if _JOB_SERVICE is None:
        settings = get_settings()
        _JOB_SERVICE = BacktestJobService(
            get_session_factory(),
            max_concurrent_per_user=settings.backtest_max_concurrent_per_user,
            timeout_seconds=settings.backtest_timeout_seconds,
        )
        _JOB_SERVICE.mark_interrupted_runs(get_session_factory())
    return _JOB_SERVICE


def _current_user(db, update: Update):
    settings = get_settings()
    telegram_id = update.effective_user.id
    return get_or_create_user(
        db, telegram_id, telegram_id in settings.admin_ids, settings.default_total_capital
    )


def _backtest_config() -> BacktestConfig:
    settings = get_settings()
    return BacktestConfig(
        initial_capital=settings.backtest_initial_capital,
        max_position_pct=settings.backtest_max_position_pct,
        entry_model=settings.backtest_entry_model,
        intrabar_policy=settings.backtest_intrabar_policy,
        transaction_costs=TransactionCostConfig(
            commission_bps=settings.backtest_commission_bps,
            slippage_bps=settings.backtest_slippage_bps,
            spread_bps=settings.backtest_spread_bps,
            bsmv_bps=settings.backtest_bsmv_bps,
            minimum_cost=settings.backtest_minimum_cost,
        ),
        minimum_history_bars=MIN_BARS_FOR_FULL_ANALYSIS,
        minimum_sample_size=settings.backtest_minimum_sample_size,
    )


def _parse_dates(args: list[str]) -> tuple[datetime, datetime]:
    if len(args) >= 3:
        start = datetime.strptime(args[1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(args[2], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=730)
    if start >= end:
        raise ValueError("Baslangic tarihi bitis tarihinden once olmali.")
    return start, end


def _run_keyboard(run_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Detayli Metrikler", callback_data=f"stage5g_btmetric_{run_id}"),
         InlineKeyboardButton("Islem Listesi", callback_data=f"stage5g_bttrades_{run_id}")],
        [InlineKeyboardButton("Rejim Sonuclari", callback_data=f"stage5g_btregime_{run_id}"),
         InlineKeyboardButton("Puan Kalibrasyonu", callback_data="stage5g_calibration_all")],
        [InlineKeyboardButton("Equity Grafigi", callback_data=f"stage5g_btequity_{run_id}")],
    ])


def _format_run(record: BacktestRun) -> str:
    metrics = json.loads(record.metrics_json or "{}")
    costs = json.loads(record.transaction_cost_config or "{}")
    warning = metrics.get("sample_warning") or "Ornek yeterli."
    return (
        "MERGEN QUANT - BACKTEST\n\n"
        f"Sembol: {record.symbol}\n"
        f"Donem: {record.start_date:%Y-%m-%d} / {record.end_date:%Y-%m-%d}\n"
        f"Islem: {metrics.get('trade_count', 0)}\n"
        f"Net getiri: %{metrics.get('total_return_percent', 0):g}\n"
        f"XU100: %{metrics.get('benchmark_return_percent', 0) or 0:g}\n"
        f"Benchmark farki: %{metrics.get('alpha_vs_benchmark_percent', 0) or 0:g}\n"
        f"Kazanma orani: %{metrics.get('win_rate_percent', 0):g}\n"
        f"Profit factor: {metrics.get('profit_factor', 0)}\n"
        f"Maksimum dusus: %{metrics.get('max_drawdown_percent', 0):g}\n"
        f"Islem basi beklenti: {metrics.get('expected_value', 0):g} TRY\n"
        f"Masraflar: komisyon {costs.get('commission_bps', 0)} bps, "
        f"spread {costs.get('spread_bps', 0)} bps, slippage {costs.get('slippage_bps', 0)} bps\n"
        f"Ornek yeterliligi: {warning}\n"
        f"Run ID: {record.run_id}\n\n"
        "Gecmis performans gelecek sonucu garanti etmez."
    )


async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Kullanim: /backtest SEMBOL [YYYY-AA-GG YYYY-AA-GG]")
        return
    try:
        symbol = normalize_symbol(context.args[0])
        start, end = _parse_dates(context.args)
    except Exception:
        await update.message.reply_text("Sembol veya tarih formati gecersiz. Ornek: /backtest THYAO 2024-01-01 2026-01-01")
        return
    settings = get_settings()
    strategy_config = get_strategy_config()
    provider = build_market_data_provider(settings)
    timeframe = strategy_config["timeframes"]["primary"]
    db = get_session_factory()()
    try:
        user = _current_user(db, update)
        user_id = user.id
    finally:
        db.close()
    adapter = ExistingSignalStrategyAdapter(
        symbol=symbol, timeframe=timeframe, strategy_config=strategy_config,
        provider_name=provider.name,
    )
    chat_id = update.effective_chat.id

    async def notify(run_id: str, status: str) -> None:
        session = get_session_factory()()
        try:
            record = session.query(BacktestRun).filter_by(run_id=run_id, user_id=user_id).one_or_none()
            if status == "COMPLETED" and record is not None:
                await context.bot.send_message(chat_id, _format_run(record), reply_markup=_run_keyboard(run_id))
            else:
                await context.bot.send_message(chat_id, f"Backtest tamamlanamadi. Durum: {status}. Run ID: {run_id}")
        finally:
            session.close()

    try:
        run_id = await _jobs().start(
            user_id=user_id, symbol=symbol, timeframe=timeframe,
            start_date=start, end_date=end,
            bars_loader=lambda: provider.get_ohlcv(symbol, timeframe, start, end),
            benchmark_loader=lambda: provider.get_ohlcv(settings.xu100_symbol, timeframe, start, end),
            signal_provider=adapter, config=_backtest_config(),
            strategy_version=strategy_config["strategy"]["version"],
            provider=provider.name, on_complete=notify,
        )
        await update.message.reply_text(
            f"Backtest baslatildi. Bot kullanilmaya devam edilebilir.\nRun ID: {run_id}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Backtest Iptal", callback_data=f"stage5g_btcancel_{run_id}")
            ]]),
        )
    except BacktestJobError as exc:
        await update.message.reply_text(str(exc))


async def cmd_backtest_ozet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = get_session_factory()()
    try:
        user = _current_user(db, update)
        runs = db.query(BacktestRun).filter_by(user_id=user.id).order_by(BacktestRun.id.desc()).limit(5).all()
        if not runs:
            await update.message.reply_text("Kayitli backtest yok.")
            return
        lines = ["BACKTEST OZETI", ""]
        lines.extend(f"{item.run_id} | {item.symbol} | {item.run_status} | %{item.progress_percent or 0:.0f}" for item in runs)
        buttons = [
            [InlineKeyboardButton(f"#{item.id} Detay", callback_data=f"stage5g_paper_detail_{item.id}"),
             InlineKeyboardButton(f"#{item.id} Kapat", callback_data=f"stage5g_paper_closeask_{item.id}")]
            for item in trades[:3]
        ]
        await update.message.reply_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons) if buttons else None
        )
    finally:
        db.close()


async def cmd_sanal_portfoy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = get_session_factory()()
    try:
        user = _current_user(db, update)
        engine = PaperTradingEngine(db, initial_capital=get_settings().paper_trading_initial_capital)
        summary = engine.performance(user.id)
        trades = engine.list_trades(user.id, active_only=True)
        lines = [
            "SANAL PORTFOY", "",
            f"Sanal nakit: {summary['cash_balance']:.2f} TRY",
            f"Gerceklesmis K/Z: {summary['realized_pnl']:.2f} TRY",
            f"Gerceklesmemis K/Z: {summary['unrealized_pnl']:.2f} TRY",
            f"Acik sanal islem: {summary['active_trade_count']}", "",
        ]
        lines.extend(f"#{item.id} {item.symbol} | kalan {item.remaining_quantity:g} | {item.status}" for item in trades[:8])
        lines.append("\nBu portfoy sanaldir; gercek para/emir icermez.")
        await update.message.reply_text("\n".join(lines))
    finally:
        db.close()


async def cmd_sanal_performans(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    symbol = normalize_symbol(context.args[0]) if context.args else None
    db = get_session_factory()()
    try:
        user = _current_user(db, update)
        result = PaperTradingEngine(db, initial_capital=get_settings().paper_trading_initial_capital).performance(user.id, symbol=symbol)
        await update.message.reply_text(
            "SANAL PERFORMANS\n\n"
            f"Kapsam: {symbol or 'Tum semboller'}\n"
            f"Islem: {result['trade_count']}\n"
            f"Gerceklesmis K/Z: {result['realized_pnl']:.2f} TRY\n"
            f"Gerceklesmemis K/Z: {result['unrealized_pnl']:.2f} TRY\n\n"
            "Sonuclar gercek islem degildir."
        )
    finally:
        db.close()


async def cmd_sinyalbasari(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    symbol = normalize_symbol(context.args[0]) if context.args else None
    db = get_session_factory()()
    try:
        query = db.query(SignalOutcome).join(SignalFeatureSnapshot)
        if symbol:
            query = query.filter(SignalFeatureSnapshot.symbol == symbol)
        outcomes = query.filter(SignalOutcome.data_sufficiency == "YETERLI").all()
        successful = sum(item.outcome_class in {"BASARILI", "KISMEN_BASARILI"} for item in outcomes)
        rate = successful / len(outcomes) * 100 if outcomes else 0.0
        await update.message.reply_text(
            "SINYAL BASARI TAKIBI\n\n"
            f"Kapsam: {symbol or 'Tum BIST'}\n"
            f"Tamamlanmis ufuk: {len(outcomes)}\n"
            f"Basarili/kismen basarili: %{rate:.1f}\n\n"
            + ("Istatistiksel degerlendirme icin islem sayisi yetersiz.\n" if len(outcomes) < 30 else "")
            + DISCLAIMER
        )
    finally:
        db.close()


async def cmd_kalibrasyon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    symbol = normalize_symbol(context.args[0]) if context.args else None
    db = get_session_factory()()
    try:
        query = db.query(ScoreCalibrationModel)
        if symbol:
            query = query.filter(
                (ScoreCalibrationModel.scope_type == "symbol") & (ScoreCalibrationModel.scope_value == symbol)
            )
        model = query.order_by(ScoreCalibrationModel.id.desc()).first()
        await update.message.reply_text(_calibration_text(db, model))
    finally:
        db.close()


async def cmd_neden(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Kullanim: /neden SEMBOL")
        return
    symbol = normalize_symbol(context.args[0])
    db = get_session_factory()()
    try:
        await update.message.reply_text(_reason_text(db, symbol))
    finally:
        db.close()


async def handle_stage5g_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    settings = get_settings()
    db = get_session_factory()()
    chart_path: Path | None = None
    try:
        user = _current_user(db, update)
        if data.startswith("stage5g_paper_preview_"):
            symbol = normalize_symbol(data.rsplit("_", 1)[1])
            signal = db.query(Signal).filter_by(symbol=symbol).order_by(Signal.id.desc()).first()
            if signal is None or signal.stop_price is None:
                await query.message.reply_text("Sanal islem icin gecerli stop/hedefli sinyal yok.")
                return
            provider = build_market_data_provider(settings)
            current = await asyncio.to_thread(resolve_current_price, provider, symbol, timezone_name=settings.timezone_name)
            price = float(current.current_price)
            risk_budget = settings.paper_trading_initial_capital * settings.risk_per_trade_percent / 100.0
            risk_per_share = max(price - signal.stop_price, 0.01)
            quantity = max(1, math.floor(min(risk_budget / risk_per_share, settings.paper_trading_initial_capital * settings.maximum_position_percent / 100.0 / price)))
            targets = (signal.target_1, signal.target_2, signal.target_3)
            preview = PaperTradingEngine(db, initial_capital=settings.paper_trading_initial_capital).preview(
                symbol=symbol, quantity=quantity, current_price=price,
                stop_price=signal.stop_price, targets=targets,
            )
            context.user_data["stage5g_paper_proposal"] = {
                "symbol": symbol, "quantity": quantity, "current_price": price,
                "stop_price": signal.stop_price, "targets": targets, "signal_id": signal.id,
                "provider": current.current_price_source,
            }
            await query.message.reply_text(
                "SANAL ISLEM ONAYI\n\n"
                f"Sembol: {symbol}\nGuncel fiyat: {price:.2f}\nSanal lot: {quantity}\n"
                f"Stop: {signal.stop_price:.2f}\nHedefler: {targets}\n"
                f"Maksimum sanal risk: {preview.maximum_virtual_risk:.2f} TRY\n"
                f"Risk/getiri: {preview.risk_reward}\n\n{preview.warning}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Sanal Islemi Onayla", callback_data="stage5g_paper_confirm"),
                    InlineKeyboardButton("Iptal", callback_data="stage5g_paper_cancel"),
                ]]),
            )
            return
        if data == "stage5g_paper_confirm":
            proposal = context.user_data.pop("stage5g_paper_proposal", None)
            if not proposal:
                await query.message.reply_text("Sanal islem onayi suresi dolmus veya bulunamadi.")
                return
            trade = PaperTradingEngine(db, initial_capital=settings.paper_trading_initial_capital).open_trade(
                user_id=user.id, data_quality="VALID", **proposal,
            )
            await query.message.reply_text(f"Sanal islem acildi: #{trade.id} {trade.symbol}. Gercek emir gonderilmedi.")
            return
        if data == "stage5g_paper_cancel":
            context.user_data.pop("stage5g_paper_proposal", None)
            await query.message.reply_text("Sanal islem iptal edildi; emir olusmadi.")
            return
        if data.startswith("stage5g_paper_detail_"):
            trade_id = int(data.rsplit("_", 1)[1])
            trade = PaperTradingEngine(db).get_trade(user.id, trade_id)
            await query.message.reply_text(
                f"SANAL ISLEM #{trade.id}\n{trade.symbol} | {trade.status}\n"
                f"Giris: {trade.entry_price:.2f} | Kalan lot: {trade.remaining_quantity:g}\n"
                f"Stop: {trade.stop_price} | Hedefler: {trade.target_1}, {trade.target_2}, {trade.target_3}\n"
                f"Gerceklesen K/Z: {trade.realized_pnl or 0:.2f}\nGerceklesmemis K/Z: {trade.unrealized_pnl or 0:.2f}\n"
                "Bu gercek islem degildir."
            )
            return
        if data.startswith("stage5g_paper_closeask_"):
            trade_id = int(data.rsplit("_", 1)[1])
            PaperTradingEngine(db).get_trade(user.id, trade_id)
            await query.message.reply_text(
                f"Sanal islem #{trade_id} guncel fiyatla kapatilsin mi?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Kapatmayi Onayla", callback_data=f"stage5g_paper_closeconfirm_{trade_id}"),
                    InlineKeyboardButton("Vazgec", callback_data="stage5g_paper_cancel"),
                ]]),
            )
            return
        if data.startswith("stage5g_paper_closeconfirm_"):
            trade_id = int(data.rsplit("_", 1)[1])
            engine = PaperTradingEngine(db)
            trade = engine.get_trade(user.id, trade_id)
            provider = build_market_data_provider(settings)
            current = await asyncio.to_thread(
                resolve_current_price, provider, trade.symbol, timezone_name=settings.timezone_name
            )
            closed = engine.close_manually(
                user_id=user.id, trade_id=trade_id, current_price=float(current.current_price)
            )
            await query.message.reply_text(
                f"Sanal islem #{closed.id} manuel kapandi. Gercek emir gonderilmedi."
            )
            return
        if data.startswith("stage5g_reason_"):
            symbol = data.rsplit("_", 1)[1]
            await query.message.reply_text(_reason_text(db, symbol))
            return
        if data == "stage5g_calibration_all":
            model = db.query(ScoreCalibrationModel).order_by(ScoreCalibrationModel.id.desc()).first()
            await query.message.reply_text(_calibration_text(db, model))
            return
        if data.startswith("stage5g_btcancel_"):
            run_id = data.removeprefix("stage5g_btcancel_")
            cancelled = _jobs().cancel(user_id=user.id, run_id=run_id)
            await query.message.reply_text(
                "Backtest iptal istegi alindi." if cancelled else "Backtest iptal edilemedi veya zaten tamamlandi."
            )
            return

        payload = data.removeprefix("stage5g_")
        if "_" not in payload:
            await query.message.reply_text("Islem bilgisi eksik.")
            return
        action, run_id = payload.split("_", 1)
        record = db.query(BacktestRun).filter_by(run_id=run_id, user_id=user.id).one_or_none()
        if record is None:
            await query.message.reply_text("Backtest bulunamadi veya bu kullaniciya ait degil.")
            return
        if action == "btmetric":
            await query.message.reply_text(_format_run(record))
        elif action == "bttrades":
            trades = db.query(BacktestTrade).filter_by(backtest_run_id=record.id).order_by(BacktestTrade.entry_time).limit(20).all()
            lines = ["ISLEM LISTESI", ""] + [
                f"{item.entry_time:%Y-%m-%d} | {item.entry_price:.2f}->{item.exit_price or 0:.2f} | {item.pnl or 0:.2f} | {item.exit_reason}"
                for item in trades
            ]
            await query.message.reply_text("\n".join(lines) if trades else "Bu kosuda islem olusmadi.")
        elif action == "btregime":
            trades = db.query(BacktestTrade).filter_by(backtest_run_id=record.id).all()
            groups: dict[str, list[float]] = {}
            for item in trades:
                groups.setdefault(item.market_regime or "unknown", []).append(float(item.pnl or 0.0))
            lines = ["REJIM SONUCLARI", ""] + [f"{key}: n={len(values)}, net={sum(values):.2f}" for key, values in groups.items()]
            await query.message.reply_text("\n".join(lines))
        elif action == "btequity":
            points = db.query(BacktestDailyEquity).filter_by(backtest_run_id=record.id).order_by(BacktestDailyEquity.trading_date).all()
            chart_path = await asyncio.to_thread(generate_persisted_equity_chart, record.symbol, points)
            with open(chart_path, "rb") as handle:
                await query.message.reply_photo(handle, caption=f"{record.symbol} backtest equity / XU100 / drawdown")
    except (PaperTradingError, ValueError) as exc:
        await query.message.reply_text(str(exc))
    except Exception as exc:
        logger.exception("5g Telegram callback hatasi: %s", exc)
        await query.message.reply_text("Islem su anda tamamlanamadi; teknik ayrinti loglara guvenli bicimde kaydedildi.")
    finally:
        if chart_path is not None:
            delete_backtest_chart(chart_path)
        db.close()


def _calibration_text(db, model: ScoreCalibrationModel | None) -> str:
    if model is None:
        return "Kalibrasyon icin tamamlanmis sinyal ornegi henuz yetersiz.\n" + DISCLAIMER
    bins = db.query(ScoreCalibrationBin).filter_by(calibration_model_id=model.id).order_by(ScoreCalibrationBin.score_min).all()
    lines = [
        "PUAN KALIBRASYONU", "",
        f"Kapsam: {model.scope_type} / {model.scope_value}",
        f"Ornek: {model.sample_count}",
        f"Brier: {model.brier_score:.4f}",
        f"Calibration error: {model.calibration_error:.4f}", "",
    ]
    lines.extend(f"{item.score_min}-{item.score_max}: %{item.calibrated_success_rate:.1f} (n={item.sample_count})" for item in bins)
    lines.extend(["", DISCLAIMER])
    return "\n".join(lines)


def _reason_text(db, symbol: str) -> str:
    snapshot = db.query(SignalFeatureSnapshot).filter_by(symbol=symbol).order_by(SignalFeatureSnapshot.signal_time.desc()).first()
    if snapshot is None:
        return "Bu sembol icin kayitli point-in-time sinyal aciklamasi yok."
    items = db.query(SignalScoreContribution).filter_by(signal_snapshot_id=snapshot.id).all()
    positives = sorted((item for item in items if item.contribution > 0), key=lambda item: abs(item.contribution), reverse=True)
    negatives = sorted((item for item in items if item.contribution < 0), key=lambda item: abs(item.contribution), reverse=True)
    lines = [f"KARARIN NEDENLERI - {symbol}", "", "PUANI ARTIRANLAR"]
    lines.extend(f"+ {item.description}: +{item.contribution:g}" for item in positives[:6])
    if not positives:
        lines.append("Olumlu katkı yok.")
    lines.extend(["", "PUANI DUSURENLER"])
    lines.extend(f"- {item.description}: {item.contribution:g}" for item in negatives[:6])
    if not negatives:
        lines.append("Olumsuz katkı yok.")
    lines.extend([
        "", "Baslangic puani: 50",
        f"Pozitif toplam: +{sum(item.contribution for item in positives):g}",
        f"Negatif toplam: {sum(item.contribution for item in negatives):g}",
        f"Ham nihai skor: {snapshot.raw_signal_score:g}/100",
    ])
    return "\n".join(lines)
