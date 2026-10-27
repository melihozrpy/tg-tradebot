from __future__ import annotations

"""Conservative BIST education and analysis commands.

These handlers deliberately operate on provider data only.  They are not a
recommendation feed: unavailable or stale inputs result in an explicit
no-analysis response rather than inferred prices, news, or trade levels.
"""

import asyncio
import html
from datetime import datetime, timedelta, timezone
from typing import Literal

import pandas as pd
from telegram import Update
from telegram.ext import ContextTypes

from app.analysis.screener_engine import SymbolTechnicalState, analyze_symbol_frame
from app.config.settings import get_settings
from app.data.provider_factory import build_market_data_provider
from app.telegram.handlers import _reject_unauthorized

_DISCLAIMER = "⚠️ Eğitim amaçlı genel analizdir, yatırım tavsiyesi değildir."


def _price(value: float | None) -> str:
    if value is None:
        return "veri yok"
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _timestamp(value) -> tuple[str, bool]:
    try:
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        stamp = stamp.tz_convert("Europe/Istanbul")
        delayed = datetime.now(timezone.utc) - stamp.to_pydatetime().astimezone(timezone.utc) > timedelta(minutes=15)
        return stamp.strftime("%d.%m.%Y %H:%M TSİ"), delayed
    except Exception:  # noqa: BLE001 - no timestamp means no freshness claim
        return "veri zamanı yok", True


def _load_index_state(timeframe: Literal["15m", "1d"]) -> SymbolTechnicalState:
    settings = get_settings()
    provider = build_market_data_provider(settings)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=58 if timeframe == "15m" else 540)
    frame = provider.get_ohlcv(settings.xu100_symbol, timeframe, start, end)
    return analyze_symbol_frame(
        settings.xu100_symbol,
        frame,
        rsi_overbought=settings.rsi_overbought,
        rsi_oversold=settings.rsi_oversold,
        minimum_confluence=settings.technical_screener_min_confluence,
        ten_indicator_minimum=settings.market_opportunity_minimum_confluence,
        timeframe=timeframe,
    )


def _trend(state: SymbolTechnicalState) -> str:
    if state.price > state.ema20 > state.ema50 > state.ema100 and state.supertrend_direction == "up":
        return "yukarı"
    if state.price < state.ema20 < state.ema50 < state.ema100 and state.supertrend_direction == "down":
        return "aşağı"
    return "nötr / karışık"


def _index_report(state: SymbolTechnicalState, *, module: str, mode: str) -> str:
    stamp, delayed = _timestamp(state.timestamp)
    trend = _trend(state)
    score = min(100, round((state.bullish_ten_confluence / 10) * 100))
    risk_lines = [
        "Takvim, KAP/VBTS, spread ve tavan-tabana kilitlenme verisi bu yanıtta doğrulanmadı.",
        "Bu nedenle işlem seviyesi veya net masraf sonrası R/R üretmiyorum.",
    ]
    if delayed:
        risk_lines.insert(0, "Gecikmeli veri: son bar 15 dakikadan eski olabilir.")
    support = _price(state.support)
    resistance = _price(state.resistance)
    lines = [
        f"<b>XU100 — {module}</b>  <i>(veri zamanı: {stamp})</i>",
        f"Durum: {mode} için yalnız endeks çerçevesi.",
        f"Bias/Trend: {trend}. Fiyat {_price(state.price)} | VWAP {_price(state.vwap)}.",
        f"Bölgeler: destek {support} | direnç {resistance}.",
        f"Uyum skoru: {score}/100 — EMA/VWAP/Supertrend/RSI/MACD/ADX verilerinden türetildi; olasılık değildir.",
        "Riskler: " + " ".join(risk_lines),
        "Modül notu: Bu kural henüz bu bot içinde [BACKTEST] bloğu olmadan test edilmiş sayılmaz.",
        _DISCLAIMER,
    ]
    return "\n".join(lines)


async def _send_index_report(update: Update, *, timeframe: Literal["15m", "1d"], module: str, mode: str) -> None:
    if await _reject_unauthorized(update) or update.message is None:
        return
    try:
        state = await asyncio.to_thread(_load_index_state, timeframe)
        await update.message.reply_text(_index_report(state, module=module, mode=mode), parse_mode="HTML")
    except Exception:  # noqa: BLE001 - a missing provider must fail closed
        await update.message.reply_text(
            f"<b>XU100 — {module}</b>\nVeri alınamadı/tutarsız, analiz üretmiyorum.\n{_DISCLAIMER}",
            parse_mode="HTML",
        )


async def cmd_gunluk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return an intraday market frame, not a personalised trade call."""

    await _send_index_report(update, timeframe="15m", module="Modül A / Gün içi", mode="gün içi")


async def cmd_haftalik(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return a daily-data weekly frame without inventing a candidate list."""

    await _send_index_report(update, timeframe="1d", module="Modül B / Haftalık", mode="3–15 işlem günü")


async def cmd_uzun(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update) or update.message is None:
        return
    await update.message.reply_text(
        "<b>UZUN VADE — Modül C</b>\n"
        "Durum: Şirket adayı için doğrulanmış temel [VERİ] sağlanmadı; aday listesi üretmiyorum.\n"
        "Çerçeve: reel kârlılık, nakit akışı, borçluluk/döviz pozisyonu, sektör çarpanları, temettü sürdürülebilirliği ve USD/TÜFE bazlı karşılaştırma birlikte değerlendirilir.\n"
        "Riskler: tek hisse ve aynı sektör yoğunlaşması, kaldıraç/kredili işlem, bilanço/KAP takvimi.\n"
        "Modül notu: Bu kural henüz bu bot içinde [BACKTEST] bloğu olmadan test edilmiş sayılmaz.\n"
        + _DISCLAIMER,
        parse_mode="HTML",
    )


async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update) or update.message is None:
        return
    args = list(context.args or [])
    if len(args) != 3:
        await update.message.reply_text(
            "<b>RİSK HESAPLAMA</b>\nKullanım: <code>/risk RİSK_TL GİRİŞ STOP</code>\n"
            "Örnek: <code>/risk 1000 50 48</code>\n"
            "Lot = işlemde göze alınan kayıp / (giriş − stop). Kendi işlem başı riskini girmelisin; yaygın eğitim aralığı %0,5–1’dir.\n"
            + _DISCLAIMER,
            parse_mode="HTML",
        )
        return
    try:
        risk_tl, entry, stop = (float(value.replace(",", ".")) for value in args)
        if risk_tl <= 0 or entry <= 0 or stop <= 0 or entry <= stop:
            raise ValueError
        lots = risk_tl / (entry - stop)
    except ValueError:
        await update.message.reply_text("Geçersiz değer. LONG örneğinde giriş stopun üzerinde olmalı.\n" + _DISCLAIMER)
        return
    await update.message.reply_text(
        "<b>RİSK HESAPLAMA</b>\n"
        f"Giriş: {_price(entry)} | Geçersizlik: {_price(stop)}\n"
        f"Birim başı risk: {_price(entry - stop)} TL\n"
        f"Teorik azami lot: <code>{lots:.0f}</code>\n"
        "Komisyon, BSMV, spread ve olası taban kilitlenmesinde stopun çalışmama riski bu hesaba dâhil değildir.\n"
        + _DISCLAIMER,
        parse_mode="HTML",
    )


async def cmd_vbts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update) or update.message is None:
        return
    await update.message.reply_text(
        "<b>VBTS DURUMU</b>\n"
        "Güncel VBTS listesi için doğrulanmış [VBTS_LİSTESİ] verisi bağlı değil; liste uydurmuyorum.\n"
        "Brüt takas, tek fiyat, kredili işlem/açığa satış yasağı veya emir paketi tedbiri varsa gün içi işlem planı üretilmez.\n"
        + _DISCLAIMER,
        parse_mode="HTML",
    )


_LEARN = {
    "fvg": "FVG, üç mumlu yapıda fiyatın hızlı hareketiyle oluşan dengesizlik bölgesidir. Tek başına giriş sinyali değildir; yapı ve kapanış teyidi aranır.",
    "atr": "ATR, fiyatın ortalama hareket genişliğini ölçer. Yüksek ATR daha geniş geçersizlik mesafesi ve daha küçük pozisyon boyutu gerektirebilir.",
    "rvol": "RVOL, güncel hacmin kendi ortalamasına oranıdır. Yüksek RVOL ilgi gösterir; tek başına yön veya kalite kanıtı değildir.",
    "vwap": "VWAP, seans içindeki hacim ağırlıklı ortalama fiyattır. Fiyatın VWAP üstünde olması tek başına alım kararı değildir.",
    "brut takas": "Brüt takasta alış ve satış aynı gün netleşmez; gün içi al-sat pratikte sınırlanır ve işlem planı üretilmez.",
}


async def cmd_ogren(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update) or update.message is None:
        return
    topic = " ".join(context.args or []).strip().casefold()
    explanation = _LEARN.get(topic)
    if explanation is None:
        await update.message.reply_text(
            "<b>ÖĞREN</b>\nKullanım: <code>/ogren FVG</code>, <code>/ogren ATR</code>, <code>/ogren RVOL</code>, <code>/ogren VWAP</code>, <code>/ogren brut takas</code>\n"
            + _DISCLAIMER,
            parse_mode="HTML",
        )
        return
    await update.message.reply_text(f"<b>{html.escape(topic.upper())}</b>\n{explanation}\n{_DISCLAIMER}", parse_mode="HTML")
