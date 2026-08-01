from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.config.instruments import resolve_report_instruments, universe_symbols
from app.config.settings import get_settings
from app.data.provider_factory import build_market_data_provider
from app.models.database import get_session_factory
from app.modules.backtest_engine import (
    SmxmVirtualPortfolioEngine,
    VirtualRiskRules,
    VirtualTradingError,
    run_smxm_backtest,
)
from app.modules.chart_engine import render_equity_curve, render_report_chart
from app.modules.evening_report import (
    build_evening_chart_spec,
    build_evening_report,
    format_evening_report,
)
from app.modules.morning_report import (
    build_morning_chart_spec,
    build_morning_report,
    format_morning_report,
)
from app.services.instrument_universe_service import (
    load_scan_cache,
    save_scan_cache,
    scan_best_entries,
)
from app.services.market_breadth_service import compute_market_breadth
from app.services.watchlist_service import get_or_create_user


PAGE_SIZE = 50
SCAN_CACHE_PATH = Path("data/cache/universe_entry_scan.json")


def _authorized(update: Update) -> bool:
    settings = get_settings()
    return not settings.admin_ids or update.effective_user.id in settings.admin_ids


async def _reject_unauthorized(update: Update) -> bool:
    if _authorized(update):
        return False
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text("Bu botu kullanmak için yetkin yok.")
    return True


def _universe_page(page: int) -> tuple[str, InlineKeyboardMarkup]:
    settings = get_settings()
    symbols = universe_symbols(settings.bist_universe_json_path)
    page_count = max(1, math.ceil(len(symbols) / PAGE_SIZE))
    page = max(1, min(page, page_count))
    start = (page - 1) * PAGE_SIZE
    visible = symbols[start:start + PAGE_SIZE]
    rows = ["📚 TÜM HİSSELER", f"PDF evreni: {len(symbols)} kod • Sayfa {page}/{page_count}", ""]
    for offset in range(0, len(visible), 5):
        rows.append("  •  ".join(visible[offset:offset + 5]))
    rows.extend(
        [
            "",
            "🔎 En kaliteli giriş bölgelerini taramak için: /eniyi50",
            "📊 Tek hisse: /islemplani THYAO",
            "ℹ️ PDF; hisse yanında bazı fon/sertifika kodları da içerir. Veri bulunmayan kodlar taramada elenir.",
        ]
    )
    buttons: list[InlineKeyboardButton] = []
    if page > 1:
        buttons.append(InlineKeyboardButton("⬅️ Önceki", callback_data=f"universe_page_{page - 1}"))
    if page < page_count:
        buttons.append(InlineKeyboardButton("Sonraki ➡️", callback_data=f"universe_page_{page + 1}"))
    keyboard = InlineKeyboardMarkup([buttons] if buttons else [])
    return "\n".join(rows), keyboard


async def cmd_tum_hisseler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    try:
        page = int(context.args[0]) if context.args else 1
    except ValueError:
        page = 1
    text, keyboard = _universe_page(page)
    await update.message.reply_text(text, reply_markup=keyboard)


async def handle_universe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    query = update.callback_query
    await query.answer()
    try:
        page = int((query.data or "").removeprefix("universe_page_"))
    except ValueError:
        page = 1
    text, keyboard = _universe_page(page)
    await query.edit_message_text(text, reply_markup=keyboard)


def _candidate_messages(result) -> list[str]:
    header = (
        "🏆 TÜM BIST • EN İYİ GİRİŞ BÖLGELERİ\n"
        f"Tarandı: {result.symbols_requested} • Başarılı veri: {result.symbols_succeeded} • "
        f"Veri yok/hata: {result.symbols_failed}\n"
        f"Zaman: {result.generated_at:%d.%m.%Y %H:%M} UTC"
        + (" • Önbellek" if result.from_cache else "")
        + "\n\n"
    )
    if not result.candidates:
        return [header + "Minimum kaliteyi geçen ve giriş bölgesi hesaplanan aday bulunamadı."]
    blocks: list[str] = []
    for rank, item in enumerate(result.candidates, start=1):
        icon = "🟢" if item.direction == "LONG" else "🔴"
        blocks.append(
            f"{rank:02d}. {icon} {item.symbol} • {item.direction} • {item.setup_score}/100\n"
            f"    Giriş {item.entry_low:.2f}-{item.entry_high:.2f} • SL {item.stop:.2f} • "
            f"Hedef {item.target:.2f} • {item.risk_reward:.2f}R\n"
            f"    Uzaklık %{item.entry_distance_percent:.2f} • Sıra puanı {item.ranking_score:.1f}"
        )
    messages: list[str] = []
    current = header
    for block in blocks:
        addition = block + "\n\n"
        if len(current) + len(addition) > 3900:
            messages.append(current.rstrip())
            current = "🏆 EN İYİ GİRİŞLER • DEVAM\n\n" + addition
        else:
            current += addition
    current += "ℹ️ Sıralama setup kalitesi, giriş uzaklığı ve RR ile yapılır; yatırım tavsiyesi değildir."
    messages.append(current.rstrip())
    return messages


async def cmd_eniyi50(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    settings = get_settings()
    try:
        requested_n = int(context.args[0]) if context.args else settings.universe_scan_top_n
    except ValueError:
        requested_n = settings.universe_scan_top_n
    top_n = max(1, min(50, requested_n))
    cached = await asyncio.to_thread(
        load_scan_cache,
        SCAN_CACHE_PATH,
        max_age_minutes=settings.universe_scan_cache_minutes,
    )
    if cached is None:
        symbols = universe_symbols(settings.bist_universe_json_path)[
            : settings.universe_scan_max_symbols_per_run
        ]
        status_message = await update.message.reply_text(
            f"🔎 {len(symbols)} kod arka planda taranıyor.\n"
            "Veri bulunamayan veya likit olmayan kodlar otomatik elenecek; bot bu sırada çalışmaya devam eder."
        )
        result = await asyncio.to_thread(
            scan_best_entries,
            lambda: build_market_data_provider(settings),
            symbols,
            top_n=top_n,
            minimum_score=settings.universe_scan_minimum_score,
            max_workers=settings.universe_scan_workers,
            long_only=settings.long_only,
        )
        await asyncio.to_thread(save_scan_cache, result, SCAN_CACHE_PATH)
        try:
            await status_message.edit_text("✅ Tüm Hisseler taraması tamamlandı.")
        except Exception:
            pass
    else:
        result = cached
    for message in _candidate_messages(result):
        await update.message.reply_text(message)


def _run_morning(settings):
    db = get_session_factory()()
    try:
        provider = build_market_data_provider(settings)
        instruments = resolve_report_instruments(settings)
        breadth = compute_market_breadth(
            provider,
            settings.bist_universe_json_path,
            max_symbols=settings.universe_scan_max_symbols_per_run,
            provider_factory=lambda: build_market_data_provider(settings),
            max_workers=settings.universe_scan_workers,
            minimum_signal_score=settings.universe_scan_minimum_score,
            top_n=12,
            cache_minutes=settings.universe_scan_cache_minutes,
        )
        return build_morning_report(provider, settings, instruments, db=db, breadth=breadth)
    finally:
        db.close()


def _run_evening(settings):
    db = get_session_factory()()
    try:
        provider = build_market_data_provider(settings)
        instruments = resolve_report_instruments(settings)
        breadth = compute_market_breadth(
            provider,
            settings.bist_universe_json_path,
            max_symbols=settings.universe_scan_max_symbols_per_run,
            provider_factory=lambda: build_market_data_provider(settings),
            max_workers=settings.universe_scan_workers,
            minimum_signal_score=settings.universe_scan_minimum_score,
            top_n=12,
            cache_minutes=settings.universe_scan_cache_minutes,
        )
        return build_evening_report(provider, settings, instruments, db=db, breadth=breadth)
    finally:
        db.close()


async def cmd_sabah_raporu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    settings = get_settings()
    wait = await update.message.reply_text("🌅 Sabah raporu hazırlanıyor…")
    chart_path: str | None = None
    try:
        report = await asyncio.to_thread(_run_morning, settings)
        primary = report.index_analysis
        provider = build_market_data_provider(settings)
        end = datetime.now(timezone.utc)
        frame = await asyncio.to_thread(
            provider.get_ohlcv, primary.symbol, "1d", end - timedelta(days=520), end
        )
        spec = build_morning_chart_spec(report, primary.symbol)
        chart_path = await asyncio.to_thread(
            render_report_chart,
            frame,
            spec,
            smart_money=primary.smart_money,
            output_dir=settings.report_chart_output_dir,
            dpi=settings.chart_dpi,
        )
        with open(chart_path, "rb") as image:
            await update.message.reply_photo(image, caption=f"🌅 {primary.symbol} • 09:00 SMXM görünümü")
        await update.message.reply_text(format_morning_report(report))
        await wait.delete()
    except Exception as exc:  # noqa: BLE001
        await wait.edit_text(f"Sabah raporu üretilemedi: {type(exc).__name__}. /veri_durumu ile kaynağı kontrol et.")
    finally:
        if chart_path:
            Path(chart_path).unlink(missing_ok=True)


async def cmd_smxm_aksam_raporu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    settings = get_settings()
    wait = await update.message.reply_text("🌙 Kapanış raporu hazırlanıyor…")
    chart_path: str | None = None
    try:
        report = await asyncio.to_thread(_run_evening, settings)
        primary = report.index_analysis
        provider = build_market_data_provider(settings)
        end = datetime.now(timezone.utc)
        frame = await asyncio.to_thread(
            provider.get_ohlcv, primary.symbol, "1d", end - timedelta(days=180), end
        )
        spec = build_evening_chart_spec(report, primary.symbol)
        chart_path = await asyncio.to_thread(
            render_report_chart,
            frame,
            spec,
            smart_money=primary.smart_money,
            output_dir=settings.report_chart_output_dir,
            dpi=settings.chart_dpi,
        )
        with open(chart_path, "rb") as image:
            await update.message.reply_photo(image, caption=f"🌙 {primary.symbol} • 21:00 kapanış görünümü")
        await update.message.reply_text(format_evening_report(report))
        await wait.delete()
    except Exception as exc:  # noqa: BLE001
        await wait.edit_text(f"Akşam raporu üretilemedi: {type(exc).__name__}. /veri_durumu ile kaynağı kontrol et.")
    finally:
        if chart_path:
            Path(chart_path).unlink(missing_ok=True)


async def cmd_sanal_portfoy_olustur(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if len(context.args) != 2:
        await update.message.reply_text("Kullanım: /sanal_portfoy_olustur Ana 10000")
        return
    settings = get_settings()
    db = get_session_factory()()
    try:
        user = get_or_create_user(
            db,
            update.effective_user.id,
            update.effective_user.id in settings.admin_ids,
            settings.default_total_capital,
        )
        portfolio = SmxmVirtualPortfolioEngine(
            db, VirtualRiskRules.from_settings(settings)
        ).create_portfolio(
            user_id=user.id,
            name=context.args[0],
            starting_balance=float(context.args[1].replace(",", ".")),
        )
        await update.message.reply_text(
            f"✅ Sanal portföy oluşturuldu\n#{portfolio.id} {portfolio.name} • {portfolio.current_balance:.2f} TRY\n"
            "Gerçek emir veya para içermez."
        )
    except (ValueError, VirtualTradingError) as exc:
        await update.message.reply_text(str(exc))
    finally:
        db.close()


async def cmd_sanal_portfoyler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    settings = get_settings()
    db = get_session_factory()()
    try:
        user = get_or_create_user(
            db,
            update.effective_user.id,
            update.effective_user.id in settings.admin_ids,
            settings.default_total_capital,
        )
        portfolios = SmxmVirtualPortfolioEngine(
            db, VirtualRiskRules.from_settings(settings)
        ).list_portfolios(user.id)
        if not portfolios:
            await update.message.reply_text("Sanal portföy yok. /sanal_portfoy_olustur Ana 10000")
            return
        lines = ["💼 SMXM SANAL PORTFÖYLER", ""]
        lines.extend(
            f"#{item.id} {item.name} • Başlangıç {item.starting_balance:.2f} • Güncel {item.current_balance:.2f} TRY"
            for item in portfolios
        )
        await update.message.reply_text("\n".join(lines))
    finally:
        db.close()


async def cmd_smxm_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update):
        return
    if len(context.args) != 4:
        await update.message.reply_text(
            "Kullanım: /smxm_backtest EURUSD 2025-01-01 2025-06-01 10000"
        )
        return
    try:
        symbol = context.args[0].upper().removesuffix(".IS")
        start = datetime.strptime(context.args[1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(context.args[2], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        balance = float(context.args[3].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Tarih veya bakiye hatalı. Örnek: /smxm_backtest THYAO 2025-01-01 2025-06-01 10000")
        return
    settings = get_settings()
    wait = await update.message.reply_text(f"🧪 {symbol} SMXM backtest başlatıldı…")
    chart_path: str | None = None
    try:
        provider = build_market_data_provider(settings)
        frame = await asyncio.to_thread(
            provider.get_ohlcv, symbol, "1d", start - timedelta(days=420), end + timedelta(days=2)
        )
        result = await asyncio.to_thread(
            run_smxm_backtest,
            frame,
            instrument=symbol,
            start_date=start,
            end_date=end,
            starting_balance=balance,
            rules=VirtualRiskRules.from_settings(settings),
            long_only=settings.long_only,
        )
        if len(result.equity_values) >= 2:
            chart_path = await asyncio.to_thread(
                render_equity_curve,
                result.equity_timestamps,
                result.equity_values,
                title=f"{symbol} • SMXM Equity Curve",
                output_dir=settings.report_chart_output_dir,
                dpi=settings.chart_dpi,
            )
            with open(chart_path, "rb") as image:
                await update.message.reply_photo(image, caption=f"🧪 {symbol} sanal backtest equity curve")
        await update.message.reply_text(
            "🧪 SMXM BACKTEST SONUCU\n\n"
            f"{symbol} • {start:%Y-%m-%d} → {end:%Y-%m-%d}\n"
            f"Başlangıç: {result.starting_balance:.2f}\nBitiş: {result.ending_balance:.2f}\n"
            f"Toplam getiri: %{result.total_return_percent:+.2f}\nİşlem: {len(result.trades)}\n"
            f"Win rate: %{result.win_rate:.2f}\nOrtalama plan RR: {result.average_rr:.2f}\n"
            f"Maksimum düşüş: %{result.max_drawdown_percent:.2f}\n\n"
            "Geçmiş performans gelecek sonucu garanti etmez; gerçek emir üretilmez."
        )
        await wait.delete()
    except Exception as exc:  # noqa: BLE001
        await wait.edit_text(f"SMXM backtest tamamlanamadı: {type(exc).__name__}: {str(exc)[:220]}")
    finally:
        if chart_path:
            Path(chart_path).unlink(missing_ok=True)
