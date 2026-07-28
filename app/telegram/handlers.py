from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

from app.analysis.indicator_engine import InsufficientDataError
from app.backtest.engine import run_backtest
from app.config.settings import get_settings, get_strategy_config
from app.data.provider_factory import build_market_data_provider
from app.execution.paper_broker import PaperBroker
from app.models.database import get_session_factory
from app.services.analysis_service import AnalysisUnavailableError, run_symbol_analysis
from app.services.portfolio_service import list_positions as list_portfolio_positions, portfolio_summary
from app.services.current_price_service import resolve_portfolio_prices
from app.services.watchlist_service import (
    InvalidSymbolError,
    SymbolAlreadyExistsError,
    SymbolNotFoundError,
    add_symbol,
    get_or_create_user,
    list_symbols,
    remove_symbol,
)
from app.telegram.message_templates import (
    format_full_analysis_message,
    format_help_message,
)

logger = logging.getLogger("mergen_quant.telegram")

_PAPER_BROKER = PaperBroker(starting_balance=100_000.0)


def _is_whitelisted(telegram_user_id: int) -> bool:
    settings = get_settings()
    admins = settings.admin_ids
    # FAZ 1: whitelist bossa herkese acik (gelistirme kolayligi), doluysa sadece listedekiler.
    if not admins:
        return True
    return telegram_user_id in admins


async def _reject_unauthorized(update: Update) -> bool:
    user_id = update.effective_user.id if update.effective_user else None
    if user_id is None or not _is_whitelisted(user_id):
        await update.message.reply_text(
            "Bu bot su anda kapali bir kullanici listesiyle calisiyor ve hesabin yetkili degil."
        )
        return True
    return False


def _get_db():
    session_factory = get_session_factory()
    return session_factory()


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    await update.message.reply_text(
        "🏔️ MONTANA MELİH HİSSE BOT • Akıllı BIST Analiz ve Risk Sistemi'ne hoş geldin.\n"
        "Bu bot yatirim tavsiyesi vermez; kural tabanli, aciklanabilir analiz uretir.\n"
        "Komutlar icin /yardim yazabilirsin."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    await update.message.reply_text(format_help_message())


async def cmd_ekle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /ekle SEMBOL (orn: /ekle THYAO)")
        return

    settings = get_settings()
    db = _get_db()
    try:
        user = get_or_create_user(
            db, update.effective_user.id, update.effective_user.id in settings.admin_ids, settings.default_total_capital
        )
        item = add_symbol(db, user, context.args[0])
        await update.message.reply_text(f"'{item.symbol}' izleme listene eklendi.")
    except (InvalidSymbolError, SymbolAlreadyExistsError) as exc:
        await update.message.reply_text(str(exc))
    finally:
        db.close()


async def cmd_sil(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /sil SEMBOL (orn: /sil THYAO)")
        return

    settings = get_settings()
    db = _get_db()
    try:
        user = get_or_create_user(
            db, update.effective_user.id, update.effective_user.id in settings.admin_ids, settings.default_total_capital
        )
        remove_symbol(db, user, context.args[0])
        await update.message.reply_text(f"'{context.args[0].upper()}' izleme listenden silindi.")
    except (InvalidSymbolError, SymbolNotFoundError) as exc:
        await update.message.reply_text(str(exc))
    finally:
        db.close()


async def cmd_liste(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    settings = get_settings()
    db = _get_db()
    try:
        user = get_or_create_user(
            db, update.effective_user.id, update.effective_user.id in settings.admin_ids, settings.default_total_capital
        )
        items = list_symbols(db, user)
        if not items:
            await update.message.reply_text("Izleme listen bos. /ekle SEMBOL ile hisse ekleyebilirsin.")
            return
        lines = [f"• {item.symbol} (min skor: {item.minimum_signal_score})" for item in items]
        await update.message.reply_text("Izleme listen:\n" + "\n".join(lines))
    finally:
        db.close()


async def cmd_analiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /analiz SEMBOL (orn: /analiz THYAO)")
        return

    symbol = context.args[0].strip().upper()
    settings = get_settings()
    strategy_config = get_strategy_config()
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        timeframe = strategy_config["timeframes"]["primary"]
        outcome = run_symbol_analysis(db, provider, symbol, timeframe, strategy_config)
        signal = outcome.signal

        text = format_full_analysis_message(signal, display_symbol=symbol)

        if outcome.is_cooldown_blocked:
            text += "\n(Not: Bu sinyal cooldown/tekrar kontrolu nedeniyle yeni sayilmadi.)"

        await update.message.reply_text(text)
    except AnalysisUnavailableError as exc:
        await update.message.reply_text(str(exc))
    except InsufficientDataError as exc:
        await update.message.reply_text(f"Yeterli veri yok: {exc}")
    finally:
        db.close()


async def cmd_portfoy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    settings = get_settings()
    provider = build_market_data_provider(settings)
    db = _get_db()
    try:
        user = get_or_create_user(
            db, update.effective_user.id, update.effective_user.id in settings.admin_ids, settings.default_total_capital
        )
        positions = list_portfolio_positions(db, user)
        prices, _ = await asyncio.to_thread(
            resolve_portfolio_prices, provider, [position.symbol for position in positions],
            timezone_name=settings.timezone_name,
        )
        summary = portfolio_summary(db, user, current_prices=prices)
        if not summary["positions"]:
            await update.message.reply_text("Portfoyunde acik pozisyon yok. /pozisyon_ekle ile ekleyebilirsin.")
            return
        lines = [
            f"{p['symbol']}: {p['lot']} lot, maliyet {p['average_cost']}, PnL {p['pnl']} (%{p['pnl_percent']})"
            for p in summary["positions"]
        ]
        text = "Portfoy ozeti:\n" + "\n".join(lines)
        text += f"\n\nToplam PnL: {summary['total_pnl']} (%{summary['total_pnl_percent']})"
        await update.message.reply_text(text)
    finally:
        db.close()


async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if not context.args:
        await update.message.reply_text("Kullanim: /backtest SEMBOL [2y|5y] (orn: /backtest THYAO 2y)")
        return

    symbol = context.args[0].strip().upper()
    period_arg = context.args[1].lower() if len(context.args) > 1 else "1y"
    period_days_map = {"1y": 500, "2y": 900, "5y": 1900}
    period_days = period_days_map.get(period_arg, 500)

    settings = get_settings()
    strategy_config = get_strategy_config()
    provider = build_market_data_provider(settings)

    from datetime import timedelta

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=period_days)
    timeframe = strategy_config["timeframes"]["primary"]

    try:
        df = provider.get_ohlcv(symbol, timeframe, start, end)
        benchmark_df = None
        try:
            benchmark_df = provider.get_ohlcv(settings.xu100_symbol, timeframe, start, end)
        except Exception:  # noqa: BLE001 - XU100 karsilastirmasi olmadan da backtest calismali
            pass

        result = run_backtest(
            df, symbol, timeframe, strategy_config,
            benchmark_df=benchmark_df, benchmark_symbol=settings.xu100_symbol,
        )
        m = result.metrics
        benchmark_text = ""
        if result.benchmark_return_percent is not None:
            benchmark_text = (
                f"XU100 getirisi: %{result.benchmark_return_percent}\n"
                f"Alpha (XU100'e karsi fark): %{result.alpha_vs_benchmark_percent}\n"
            )
        buy_hold_text = (
            f"Buy&Hold getirisi: %{result.buy_and_hold_return_percent}\n"
            if result.buy_and_hold_return_percent is not None else ""
        )
        text = (
            f"Backtest — {symbol} ({timeframe}, {period_arg})\n\n"
            f"Baslangic sermaye: {result.initial_capital}\n"
            f"Bitis equity: {result.final_equity}\n"
            f"Toplam getiri: %{m.total_return_percent}\n"
            f"{buy_hold_text}"
            f"{benchmark_text}"
            f"Maks. dususs: %{m.max_drawdown_percent}\n"
            f"Sharpe: {m.sharpe_ratio}  Sortino: {m.sortino_ratio}\n"
            f"Kazanma orani: %{m.win_rate_percent}  Islem sayisi: {m.trade_count}\n"
            f"Profit factor: {m.profit_factor}\n\n"
            + "\n".join(f"⚠️ {w}" for w in result.warnings)
        )
        await update.message.reply_text(text)
    except InsufficientDataError as exc:
        await update.message.reply_text(f"Backtest yapilamadi, yeterli veri yok: {exc}")


async def cmd_acil_durdur(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    settings = get_settings()
    db = _get_db()
    try:
        user = get_or_create_user(
            db, update.effective_user.id, update.effective_user.id in settings.admin_ids, settings.default_total_capital
        )
        user.kill_switch_active = True
        db.commit()
        await update.message.reply_text(
            "🛑 Kill switch AKTIF. Tum analiz ve islem sureclerin durduruldu. /devam_et ile tekrar acabilirsin."
        )
    finally:
        db.close()


async def cmd_devam_et(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    settings = get_settings()
    db = _get_db()
    try:
        user = get_or_create_user(
            db, update.effective_user.id, update.effective_user.id in settings.admin_ids, settings.default_total_capital
        )
        user.kill_switch_active = False
        db.commit()
        await update.message.reply_text("✅ Kill switch kapatildi, sistem normal calismaya devam ediyor.")
    finally:
        db.close()


async def cmd_durum(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    settings = get_settings()
    provider = build_market_data_provider(settings)
    health = provider.health_check()
    await update.message.reply_text(
        f"Sistem durumu:\nVeri saglayicisi: {health['provider']} -> {health['status']}\n"
        f"Detay: {health.get('detail', '-')}\n"
        f"Piyasa acik mi: {'evet' if provider.is_market_open() else 'hayir'}"
    )
