from __future__ import annotations

"""Structured /temelanaliz output backed by the configured provider chain."""

import asyncio
from urllib.parse import quote

from telegram import Update
from telegram.ext import ContextTypes

from app.fundamentals import FundamentalDataError
from app.fundamentals.factory import build_fundamental_provider
from app.services.company_analysis_service import CompanyAnalysis, analyze_company
from app.telegram.handlers import _reject_unauthorized


def _number(value: float | None, *, suffix: str = "", percentage: bool = False) -> str:
    if value is None:
        return "doğrulanmadı"
    if percentage:
        return f"%{value * 100:.1f}"
    return f"{value:,.2f}{suffix}".replace(",", " ")


def _money(value: float | None, currency: str) -> str:
    if value is None:
        return "doğrulanmadı"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:,.1f} mn {currency}".replace(",", " ")
    return f"{value:,.0f} {currency}".replace(",", " ")


def format_fundamental_analysis(result: CompanyAnalysis, *, average_turnover: float | None = None) -> str:
    """Keep requested fields explicit without inventing unavailable ratios."""

    metrics = result.metrics
    net_debt = None
    debt, cash = metrics.get("total_debt"), metrics.get("total_cash")
    if debt is not None and cash is not None:
        net_debt = debt - cash
    asset_liability = metrics.get("asset_to_liability")
    average = _money(average_turnover, result.currency) if average_turnover is not None else "doğrulanmadı"
    positives = "\n".join(f"• {item}" for item in result.positives[:4]) or "• Doğrulanmış güçlü kalem yok"
    risks = "\n".join(f"• {item}" for item in result.risks[:4]) or "• Ek risk kalemi sınıflandırılamadı"
    warnings = "\n".join(f"• {item}" for item in result.data_warnings[:3]) or "• Ek veri kalite uyarısı yok"
    status_note = {
        "GÜÇLÜ": "Oranlar bu veri döneminde olumlu; değerleme ve güncel KAP akışı ayrıca kontrol edilmeli.",
        "RİSKLİ": "Borç, kârlılık, büyüme veya nakit verilerinden en az biri baskı riski taşıyor.",
        "DENGELİ": "Veriler karışık; tek bir çarpanla ucuz/pahalı hükmü kurulmaz.",
        "VERİ YETERSİZ": "Yeterli doğrulanmış kalem yok; ucuz/pahalı yorumu yapılmadı.",
    }.get(result.status, result.decision_summary)
    isyatirim_url = (
        "https://www.isyatirim.com.tr/tr-tr/analiz/hisse/Sayfalar/"
        f"sirket-karti.aspx?hisse={quote(result.symbol)}"
    )
    return (
        f"🏢 TEMEL ANALİZ — {result.name} ({result.symbol})\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Sektör: {result.sector} / {result.industry}\n"
        f"Temel kalite: {result.status} • {result.score}/100  |  Veri kapsamı: %{result.data_coverage}\n\n"
        "📊 DOĞRULANMIŞ ORANLAR\n"
        f"• F/K: {_number(metrics.get('trailing_pe'), suffix='x')}\n"
        f"• PD/DD: {_number(metrics.get('price_to_book'), suffix='x')}\n"
        f"• FD/FAVÖK: {_number(metrics.get('enterprise_to_ebitda'), suffix='x')}\n"
        f"• A/D (aktif/pasif): {_number(asset_liability, suffix='x')}\n"
        f"• Cari oran: {_number(metrics.get('current_ratio'), suffix='x')}\n"
        f"• Net borç: {_money(net_debt, result.currency)}\n"
        f"• Piyasa değeri: {_money(metrics.get('market_cap'), result.currency)}\n"
        f"• 20g ort. işlem değeri: {average}\n"
        "• Yabancı oranı / halka açıklık / ortaklık yapısı: yapılandırılmış kaynakta doğrulanmadı\n\n"
        "✅ DESTEKLEYEBİLECEK VERİLER\n"
        f"{positives}\n\n"
        "⚠️ UZAK DURMAYI GEREKTİREBİLECEK RİSKLER\n"
        f"{risks}\n\n"
        "🧭 UCUZ MU / PAHALI MI?\n"
        f"{status_note}\n"
        "F/K, PD/DD ve FD/FAVÖK ancak sektör, büyüme, bilanço dönemi ve benzer şirketlerle kıyaslandığında anlamlıdır.\n\n"
        "🔎 VERİ KALİTESİ\n"
        f"{warnings}\n"
        f"🗓️ Son finansal dönem: {result.financial_period or 'doğrulanmadı'}\n"
        f"Kaynak: {result.source}\n"
        f"İş Yatırım şirket kartı: {isyatirim_url}\n"
        f"Resmî KAP araması: {result.kap_url}\n\n"
        "⚠️ Bu temel kalite çerçevesi kişiye özel yatırım tavsiyesi değildir; kaynak ve güncellik işlem öncesi doğrulanmalıdır."
    )[:4096]


async def cmd_temelanaliz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _reject_unauthorized(update) or update.message is None:
        return
    if not context.args:
        await update.message.reply_text("Kullanım: /temelanaliz THYAO")
        return
    symbol = context.args[0].strip().upper().removesuffix(".IS")
    from app.config.settings import get_settings
    from app.data.provider_factory import build_market_data_provider
    from datetime import datetime, timedelta, timezone

    settings = get_settings()
    try:
        provider = build_fundamental_provider(settings)
        result = await asyncio.to_thread(analyze_company, symbol, fundamental_provider=provider)
        turnover = None
        try:
            frame = await asyncio.to_thread(
                build_market_data_provider(settings).get_ohlcv,
                symbol,
                "1d",
                datetime.now(timezone.utc) - timedelta(days=45),
                datetime.now(timezone.utc),
            )
            if frame is not None and not frame.empty and {"close", "volume"}.issubset(frame.columns):
                turnover = float((frame["close"] * frame["volume"]).tail(20).mean())
        except Exception:
            pass
        await update.message.reply_text(
            format_fundamental_analysis(result, average_turnover=turnover),
            disable_web_page_preview=True,
        )
    except FundamentalDataError as exc:
        await update.message.reply_text(
            f"⚠️ {symbol} için doğrulanmış temel veri alınamadı: {exc}\n\n"
            "Fintables/KAP REST veya izin verilen ikincil kaynak ayarını /veri_durumu ile kontrol et; sayı uydurulmadı."
        )
    except Exception:
        await update.message.reply_text(
            "⚠️ Temel analiz şu an tamamlanamadı. Veri sağlayıcısı yanıtı doğrulanamadığı için rapor üretilmedi."
        )
