"""Conservative daily screen and VIOP information commands."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

from app.analysis.screener_engine import (
    format_daily_top_picks_report,
    run_daily_top_picks_scan,
    run_market_opportunity_scan,
)
from app.analysis.viop_engine import (
    analyze_viop_spot_underlying,
    estimate_viop_contract_risk,
    find_viop_underlying,
    horizon_guidance,
    load_viop_universe,
    parse_viop_horizon,
    priority_viop_symbols,
    spot_direction_label,
)
from app.config.instruments import universe_symbols
from app.config.settings import get_settings
from app.data.provider_factory import build_market_data_provider
from app.fundamentals.factory import build_fundamental_provider
from app.telegram.handlers import _reject_unauthorized

logger = logging.getLogger("mergen_quant.telegram.basic_viop")


async def cmd_basitalsat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the existing daily technical + verified-fundamental quality screen."""

    if await _reject_unauthorized(update) or update.message is None:
        return
    settings = get_settings()
    strict_settings = settings.model_copy(update={"daily_top_picks_minimum_confirmations": 8})
    await update.message.reply_text(
        "📊 GÜNLÜK TEKNİK + TEMEL TARAMA başlıyor.\n"
        "Yalnız güçlü teknik teyit, gerçek direnç potansiyeli ve doğrulanabilir temel veriyle uyumlu adaylar gösterilir; kriteri geçmeyen hisse zorla eklenmez."
    )

    def scan():
        return run_daily_top_picks_scan(
            symbols=universe_symbols(settings.bist_universe_json_path),
            provider_factory=lambda: build_market_data_provider(settings),
            fundamental_provider_factory=lambda: build_fundamental_provider(settings),
            settings=strict_settings,
        )

    try:
        report = await asyncio.to_thread(scan)
        text = format_daily_top_picks_report(report, timezone_name=settings.timezone_name)
        if text:
            await update.message.reply_text(text, disable_web_page_preview=True)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Basit al-sat taraması tamamlanamadı: %s", exc)
        await update.message.reply_text(
            "⚠️ Günlük tarama doğrulanabilir veriyle tamamlanamadı; tahmini aday gönderilmedi."
        )


def _format_number(value: float) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _parse_capital(value: str | None) -> float | None:
    """Accept 5000, 5.000, 5,000 and 10k without guessing another currency."""

    if not value:
        return None
    raw = str(value).strip().casefold().replace("tl", "").replace("try", "").replace(" ", "")
    multiplier = 1.0
    if raw.endswith("k"):
        raw, multiplier = raw[:-1], 1_000.0
    if raw.count(",") == 1 and raw.count(".") == 0:
        left, right = raw.split(",")
        raw = left + right if len(right) == 3 else f"{left}.{right}"
    elif raw.count(".") >= 1 and raw.count(",") == 0:
        parts = raw.split(".")
        raw = "".join(parts) if len(parts[-1]) == 3 else raw
    else:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        result = float(raw) * multiplier
    except ValueError:
        return None
    return result if result > 0 else None


async def _xu100_spot_reference(settings) -> str:
    """Use a clearly-labelled spot reference, never a futures quote."""

    try:
        provider = build_market_data_provider(settings)
        symbol = str(settings.xu100_symbol).upper().removesuffix(".IS").removeprefix("^")
        frame = await asyncio.to_thread(
            provider.get_ohlcv,
            symbol,
            "1d",
            datetime.now(timezone.utc) - timedelta(days=7),
            datetime.now(timezone.utc),
        )
        if len(frame) >= 2:
            close = float(frame.iloc[-1]["close"])
            previous = float(frame.iloc[-2]["close"])
            change = ((close / previous) - 1) * 100 if previous else 0.0
            return f"XU100 spot referansı: {_format_number(close)} (%{change:+.2f}); VİOP kontrat fiyatı değildir."
    except Exception as exc:  # noqa: BLE001
        logger.info("VİOP spot referansı alınamadı: %s", type(exc).__name__)
    return "XU100 spot referansı şu an alınamadı; bu bir emir sinyali değildir."


async def cmd_viop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Teach VIOP with dated official-source context and risk gates."""

    if await _reject_unauthorized(update) or update.message is None:
        return
    settings = get_settings()
    try:
        universe = load_viop_universe(settings.viop_underlyings_json_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("VİOP izleme evreni yüklenemedi: %s", exc)
        await update.message.reply_text("⚠️ VİOP izleme listesi yüklenemedi; işlem önerisi üretilmedi.")
        return
    reference = await _xu100_spot_reference(settings)
    first_group = [item.symbol for item in universe.underlyings if item.market_maker_group == 1]
    second_group = [item.symbol for item in universe.underlyings if item.market_maker_group == 2]
    await update.message.reply_text(
        "📉 VİOP BAŞLANGIÇ REHBERİ — MONTANA FİNANS ROBOTU\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "VİOP, dayanak varlığın gelecekteki fiyatına bağlı vadeli sözleşmelerin piyasasıdır. Kaldıraç hem kârı hem zararı büyütür; stop çalışmazsa zarar teminatın üstüne çıkabilir.\n\n"
        "1️⃣ EMİR ÖNCESİ ZORUNLU 6 KONTROL\n"
        "• Aktif sözleşme kodu ve vade\n"
        "• Kontrat çarpanı (pay kontratında standartta 100 pay; özsermaye haliyle değişebilir)\n"
        "• Aracı kurumun anlık başlangıç/sürdürme teminatı\n"
        "• Kontratın alış-satış farkı, derinliği ve açık pozisyonu\n"
        "• Spot-vadeli farkı (baz)\n"
        "• Pay vadeli kontratlarında fiziki teslimat ve son işlem günü\n\n"
        "2️⃣ 5–10 BİN TL İÇİN GERÇEKÇİ ÇERÇEVE\n"
        "• Önce aracı kurum ekranında tek kontratın başlangıç teminatını gör. Yetmiyorsa işlem YOK.\n"
        "• Bot varsayılan olarak sermayenin yalnız %0,5'ini stop riski kabul eder; 5.000 TL'de 25 TL, 10.000 TL'de 50 TL.\n"
        "• Bu risk bütçesi bir standart kontratın stop zararını karşılamıyorsa sonuç 0 kontrattır; kaldıraç için stop genişletilmez.\n"
        "• Yeni başlayan için aynı anda tek açık pozisyon, ekleme/martingale yok, teminat tamponu olmadan taşıma yok.\n\n"
        f"3️⃣ VİOP UYGUNLUK İZLEME EVRENİ (kaynak kontrolü: {universe.verified_on})\n"
        f"🟢 Grup 1 öncelikli izleme: {', '.join(first_group)}\n"
        f"🟡 Grup 2: {', '.join(second_group)}\n"
        "Grup sınıfı piyasa yapıcılık sınıflamasıdır; canlı likidite garantisi değildir.\n\n"
        "4️⃣ SÜRE YÖNETİMİ\n"
        "• Gün içi: vade/akşam seansı/boşluk riski kontrol edilmeden taşıma yapılmaz.\n"
        "• Haftalık: her günlük kapanışta stop, teminat ve vade yeniden değerlendirilir.\n"
        "• Aylık: küçük teminat veya yeni kullanıcı için varsayılan tercih değildir; vade ve fiziki teslimat riski ayrıca yönetilir.\n\n"
        f"ℹ️ {reference}\n"
        "📌 Kullanım: /viopislem THYAO gunici 5000  |  /viopislem THYAO haftalik 10000  |  /viopislem liste\n"
        f"Resmî kaynak: {universe.source_url}",
        disable_web_page_preview=True,
    )
    await update.message.reply_text(
        "⚠️ KIRMIZI ÇİZGİLER\n"
        "• Bu bot spot dayanak verisini analiz eder; canlı VİOP kontrat fiyatı, teminat, açık pozisyon ve baz bağlı değilken uydurulmaz.\n"
        "• LONG/SHORT etiketi yalnız koşullu teknik senaryodur; emir talimatı değildir.\n"
        "• Kâr hedefi garanti değildir. Vade sonu, bilanço/KAP, devre kesici, gap ve teminat tamamlama çağrısı riski ayrıca vardır.\n"
        "• İşlem açmadan önce aracı kurumun sözleşme özellikleri, güncel risk bildirim formu ve teminatını doğrula.\n\n"
        f"Not: {universe.notice}",
        disable_web_page_preview=True,
    )


def _format_viop_watchlist(report, *, universe, horizon: str, maximum: int) -> str:
    candidates = list(report.al_sat_uygun[:maximum])
    if not candidates:
        return (
            "🧭 VİOP FIRSAT LİSTESİ — BEKLE\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{report.scanned} dayanak tarandı; 8/10 bağımsız teknik uyum eşiğini geçen yok.\n"
            "Zorla LONG/SHORT listesi oluşturulmadı."
        )
    lines = [
        "🧭 VİOP FIRSAT LİSTESİ — SPOT DAYANAKLI",
        "━━━━━━━━━━━━━━━━━━",
        f"{report.scanned} resmi izleme dayanağı • 8/10 teknik uyum • {horizon}",
        "Her satır koşullu izleme içindir; aktif vade/teminat/derinlik aracı kurumdan doğrulanır.",
        "",
    ]
    for index, state in enumerate(candidates, start=1):
        underlying = find_viop_underlying(universe, state.symbol)
        group = f"Grup {underlying.market_maker_group}" if underlying else "İzleme"
        reasons: list[str] = []
        if state.adx >= 20:
            reasons.append(f"ADX {state.adx:.0f}")
        if state.relative_volume >= 1:
            reasons.append(f"hacim {state.relative_volume:.1f}x")
        reasons.append("Supertrend ↑" if state.supertrend_direction == "up" else "Supertrend ↓")
        lines.extend(
            [
                f"{index}. {state.symbol} • {spot_direction_label(state)} • {max(state.bullish_ten_confluence, state.bearish_ten_confluence)}/10",
                f"   {group} • Spot: {_format_number(state.price)} • RSI {state.rsi:.1f}",
                f"   Neden: {' • '.join(reasons)}",
                f"   Ayrıntılı senaryo: /viopislem {state.symbol} gunici",
            ]
        )
    return "\n".join(lines)[:4096]


async def cmd_viopislem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return a confirmation-first spot-underlying VIOP plan or strict watchlist."""

    if await _reject_unauthorized(update) or update.message is None:
        return
    args = list(context.args or [])
    settings = get_settings()
    try:
        universe = load_viop_universe(settings.viop_underlyings_json_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("VİOP izleme evreni yüklenemedi: %s", exc)
        await update.message.reply_text("⚠️ VİOP izleme listesi yüklenemedi; işlem planı üretilmedi.")
        return
    if not args:
        await update.message.reply_text(
            "Kullanım:\n"
            "/viopislem THYAO gunici 5000\n"
            "/viopislem THYAO haftalik 10000\n"
            "/viopislem liste\n\n"
            "Sermaye opsiyoneldir; yazılırsa %0,5 stop-risk bütçesiyle teorik kontrat sınırı gösterilir. Canlı teminat doğrulanmadan emir önerilmez."
        )
        return
    first = args[0].casefold()
    if first in {"liste", "listele", "firsatlar", "fırsatlar"}:
        await update.message.reply_text("🔎 Grup 1 VİOP izleme dayanakları 1 saatlik kapanmış mumlarla taranıyor...")
        maximum = max(3, min(15, int(settings.viop_watchlist_max_results)))
        strict_settings = settings.model_copy(
            update={"market_opportunity_minimum_confluence": 8, "market_opportunity_max_results": maximum}
        )
        symbols = priority_viop_symbols(universe, maximum=15)
        try:
            report = await asyncio.to_thread(
                run_market_opportunity_scan,
                symbols=symbols,
                provider_factory=lambda: build_market_data_provider(settings),
                settings=strict_settings,
                timeframe="1h",
            )
            await update.message.reply_text(
                _format_viop_watchlist(report, universe=universe, horizon="gün içi", maximum=maximum),
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("VİOP fırsat listesi üretilemedi: %s", exc)
            await update.message.reply_text("⚠️ VİOP dayanak taraması tamamlanamadı; tahmini LONG/SHORT listesi gönderilmedi.")
        return

    horizon = parse_viop_horizon(args[1] if len(args) >= 2 else None)
    if horizon is None:
        await update.message.reply_text("Süre: gunici, haftalik veya aylik olmalı. Örnek: /viopislem THYAO haftalik 10000")
        return
    capital = _parse_capital(args[2] if len(args) >= 3 else None)
    if len(args) >= 3 and capital is None:
        await update.message.reply_text("Sermaye sayı olmalı. Örnek: 5000, 10.000 veya 10k.")
        return
    underlying = find_viop_underlying(universe, args[0])
    if underlying is None:
        symbols = ", ".join(priority_viop_symbols(universe, maximum=15))
        await update.message.reply_text(
            f"{args[0].upper()} bu tarihli VİOP izleme evreninde yok. Öncelikli izleme: {symbols}\n"
            "Aracı kurum ekranındaki aktif dayanağı ve sözleşmeyi doğrula; güncel liste değişebilir."
        )
        return
    await update.message.reply_text(
        f"🔎 {underlying.symbol} için {horizon} spot-dayanak senaryosu hesaplanıyor; canlı VİOP kontrat verisi yerine geçmez..."
    )
    try:
        analysis = await asyncio.to_thread(
            analyze_viop_spot_underlying,
            underlying,
            provider=build_market_data_provider(settings),
            settings=settings,
            horizon=horizon,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("VİOP spot senaryosu üretilemedi symbol=%s: %s", underlying.symbol, exc)
        await update.message.reply_text("⚠️ Kapanmış ve yeterli spot mum verisi alınamadı; VİOP planı uydurulmadı.")
        return

    state, scenario = analysis.state, analysis.scenario
    lines = [
        f"📉 VİOP SPOT-DAYANAK PLANI — {underlying.symbol}",
        "━━━━━━━━━━━━━━━━━━",
        f"Ufuk: {horizon.upper()} • Piyasa yapıcılık grubu: {underlying.market_maker_group}",
        f"Spot son fiyat: {_format_number(state.price)} • Teknik durum: {spot_direction_label(state)}",
        f"10 gösterge uyumu: LONG {state.bullish_ten_confluence}/10 • SHORT {state.bearish_ten_confluence}/10",
        f"RSI {state.rsi:.1f} • ADX {state.adx:.1f} • Hacim {state.relative_volume:.2f}x • Supertrend {'yukarı' if state.supertrend_direction == 'up' else 'aşağı'}",
        "",
    ]
    if scenario is None:
        lines.extend(
            [
                "⏸️ GİRİŞ YOK — 8/10 teknik uyum ve retest koşulu birlikte oluşmadı.",
                "Yapılacak: fiyatı kovalamak yerine /viopislem liste ile taramayı veya yeni kapanmış mumları izle.",
                horizon_guidance(horizon),
            ]
        )
    else:
        direction = "LONG" if scenario.direction == "bullish" else "SHORT"
        trigger = "yeşil kapanış + hacim" if scenario.direction == "bullish" else "kırmızı kapanış + hacim"
        lines.extend(
            [
                f"🎯 ANA SENARYO: {direction} — yalnız {scenario.entry_low:.2f}-{scenario.entry_high:.2f} spot retest bölgesinde {trigger} teyidi gelirse.",
                f"Giriş bölgesi (spot referans): {_format_number(scenario.entry_low)} - {_format_number(scenario.entry_high)}",
                f"Geçersizlik/stop (spot referans): {_format_number(scenario.stop)}",
                f"Hedef 1 / 2 (spot referans): {_format_number(scenario.tp1)} / {_format_number(scenario.tp2)} • R/R: 1:{scenario.rr:.2f}",
                f"Neden: {' • '.join(scenario.reasons[:4]) or 'Teknik uyum'}",
                "⚠️ Bu fiyatlar VİOP kontrat emri değildir; kontratın bazını, vadesini ve derinliğini aracı kurum ekranında eşleştir.",
                horizon_guidance(horizon),
            ]
        )
        if capital is not None:
            estimate = estimate_viop_contract_risk(
                capital=capital,
                entry_spot=(scenario.entry_low + scenario.entry_high) / 2,
                stop_spot=scenario.stop,
                multiplier=underlying.standard_contract_multiplier,
                risk_percent=float(settings.viop_risk_percent),
            )
            lines.extend(
                [
                    "",
                    f"🧮 { _format_number(estimate.capital) } TL İÇİN STOP-RİSK HESABI",
                    f"Risk bütçesi: %{estimate.risk_percent:.1f} = {_format_number(estimate.risk_budget)} TL",
                    f"Standart {estimate.multiplier} pay çarpanıyla tahmini stop zararı/kontrat: {_format_number(estimate.estimated_loss_per_contract)} TL",
                    f"Stop riskine göre üst sınır: {estimate.maximum_contracts_by_stop} kontrat",
                    "Bu sonuç canlı başlangıç/sürdürme teminatını içermez. Aracı kurumun anlık teminatı sermayeni aşıyorsa veya stop sınırı 0 ise işlem YOK.",
                ]
            )
    await update.message.reply_text("\n".join(lines)[:4096], disable_web_page_preview=True)
