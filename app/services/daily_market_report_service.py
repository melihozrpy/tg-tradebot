from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.analysis.indicator_engine import ema, rsi


@dataclass(frozen=True)
class DailyMarketReport:
    date: str
    close: float
    daily_change: float
    direction: str
    rsi: float
    above_ema20: bool
    above_ema50: bool
    breadth_text: str
    policy_rate: float | None
    risk: str


def build_daily_market_report(provider, settings) -> DailyMarketReport:
    end = datetime.now(timezone.utc)
    df = provider.get_ohlcv(settings.xu100_symbol, "1d", end - timedelta(days=180), end)
    data = df.sort_values("timestamp").reset_index(drop=True)
    if len(data) < 55:
        raise ValueError("XU100 yön raporu için yeterli veri yok.")
    close = data["close"].astype(float)
    last = float(close.iloc[-1]); previous = float(close.iloc[-2])
    change = ((last / previous) - 1) * 100 if previous else 0.0
    ema20 = float(ema(close, 20).iloc[-1]); ema50 = float(ema(close, 50).iloc[-1])
    rsi_value = float(rsi(close, 14).iloc[-1])
    if last > ema20 > ema50:
        direction = "YUKARI"
    elif last < ema20 < ema50:
        direction = "AŞAĞI"
    else:
        direction = "KARIŞIK / YATAY"
    risk = "YÜKSEK" if rsi_value > 72 or rsi_value < 28 or abs(change) > 3 else "ORTA" if direction == "KARIŞIK / YATAY" else "NORMAL"
    breadth_text = "Veri yok"
    try:
        from app.services.market_breadth_service import compute_market_breadth
        breadth = compute_market_breadth(provider, settings.bist_symbols_csv_path)
        advancers = getattr(breadth, "advancers", None)
        decliners = getattr(breadth, "decliners", None)
        if advancers is not None and decliners is not None:
            breadth_text = f"Yükselen {advancers} / Düşen {decliners}"
    except Exception:
        pass
    timestamp = data.iloc[-1]["timestamp"]
    return DailyMarketReport(
        date=timestamp.strftime("%d.%m.%Y"), close=round(last, 2), daily_change=round(change, 2),
        direction=direction, rsi=round(rsi_value, 1), above_ema20=last > ema20, above_ema50=last > ema50,
        breadth_text=breadth_text, policy_rate=getattr(settings, "tcmb_policy_rate_percent", None), risk=risk,
    )


def format_daily_market_report(report: DailyMarketReport) -> str:
    rate = f"%{report.policy_rate:.2f}" if report.policy_rate is not None else "Veri bağlı değil"
    return (
        f"☀️ GÜNLÜK PİYASA BRİFİNGİ\n"
        f"Dünün kapanışı: {report.date}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📊 XU100: {report.close:.2f}  |  %{report.daily_change:+.2f}\n"
        f"🧭 Ana yön: {report.direction}\n"
        f"⚡ RSI: {report.rsi:.1f}  |  Risk: {report.risk}\n"
        f"📈 EMA20: {'Üstünde' if report.above_ema20 else 'Altında'}\n"
        f"📉 EMA50: {'Üstünde' if report.above_ema50 else 'Altında'}\n"
        f"🌍 Piyasa genişliği: {report.breadth_text}\n"
        f"🏦 TCMB politika faizi: {rate}\n\n"
        "Bugünün okuması:\n"
        f"• Yön {report.direction.lower()}\n"
        f"• Günlük risk seviyesi {report.risk.lower()}\n"
        "• Açılışta ilk 30 dakikanın teyidi beklenmeli\n\n"
        "ℹ️ Teknik piyasa özetidir; yatırım tavsiyesi değildir."
    )
