from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.analysis.advanced_scoring import AdvancedScoreBreakdown
from app.analysis.decision_engine import (
    DECISION_BUY,
    DECISION_REDUCE_RISK,
    DECISION_SELL,
    DECISION_STRONG_BUY,
    DECISION_STRONG_SELL_RISK,
    DECISION_WAIT_TRIGGER,
)
from app.analysis.market_state import MODE_INTRADAY_PREVIEW
from app.analysis.position_advice_engine import PositionAdviceResult, evaluate_position
from app.analysis.relative_strength_engine import RelativeStrengthResult
from app.analysis.relative_strength_periods_engine import (
    PERIOD_1AY,
    PERIOD_1HAFTA,
    PERIOD_3AY,
    PERIOD_6AY,
    classification_label,
)
from app.analysis.signal_engine import SignalResult
from app.telegram.message_templates import SIGNAL_TYPE_LABELS_TR, TREND_LABELS_TR, format_price
from app.telegram.formatters import (
    MissingDataCollector,
    detailed_scenario_lines,
    enforce_message_limit,
    optional_text,
    price_text,
    public_label,
    score_text,
)
from app.utils.financial_formatter import (
    format_multiple as format_multiple_safe,
    format_percent as format_percent_safe,
    format_price as format_price_safe,
    format_try_compact,
)

ISTANBUL_OFFSET_HOURS = 3

# Portfoyunde hisse olmayan bir kullaniciya asla "sat" / "pozisyon azalt"
# denmez; bu sinif/kararlar sadece BILGI/RISK UYARISI olarak yeniden ifade edilir.
_SELL_ORIENTED_SIGNAL_TYPES = {"REDUCE_POSITION", "STRONG_RISK"}
_SELL_ORIENTED_DECISION_CLASSES = {DECISION_SELL, DECISION_STRONG_SELL_RISK, DECISION_REDUCE_RISK}

_NO_POSITION_SIGNAL_LABELS = {
    "REDUCE_POSITION": "Risk arttı (yeni pozisyon önerilmiyor)",
    "STRONG_RISK": "Zayıf görünüm (yeni pozisyon önerilmiyor)",
}

_NO_POSITION_DECISION_LABELS = {
    DECISION_REDUCE_RISK: "RİSK ARTTI (yeni pozisyon önerilmiyor)",
    DECISION_SELL: "ZAYIF GÖRÜNÜM (yeni pozisyon önerilmiyor)",
    DECISION_STRONG_SELL_RISK: "GÜÇLÜ RİSK UYARISI (yeni pozisyon açma)",
}

_NO_POSITION_NOTE = (
    "ℹ️ Portföyünde bu hisse görünmüyor; bu değerlendirme yalnızca izleme/risk "
    "uyarısı amaçlıdır, elde olmayan bir hisse için satış talimatı değildir."
)


def resolve_signal_label(signal_type: str, holds_position: bool) -> str:
    """Sinyal turu etiketini portfoy durumuna gore uyarlar.

    Kullanicinin portfoyunde ilgili hisse yoksa 'pozisyon azaltma adayi' /
    'satis riski' gibi ELDE OLAN bir pozisyonu varsayan ifadeler asla kullanilmaz.
    """
    if signal_type in _SELL_ORIENTED_SIGNAL_TYPES and not holds_position:
        return _NO_POSITION_SIGNAL_LABELS.get(signal_type) or SIGNAL_TYPE_LABELS_TR.get(signal_type) or signal_type
    return SIGNAL_TYPE_LABELS_TR.get(signal_type) or signal_type


def resolve_decision_label(decision, holds_position: bool) -> str:
    """DecisionEngine sonucundaki nihai karar etiketini portfoy durumuna gore uyarlar."""
    if decision is None:
        return ""
    if decision.decision_class in _SELL_ORIENTED_DECISION_CLASSES and not holds_position:
        return _NO_POSITION_DECISION_LABELS.get(decision.decision_class) or decision.decision_label_tr or ""
    return decision.decision_label_tr or ""


def _portfolio_note(signal_type: str, decision, holds_position: bool) -> str:
    is_sell_oriented = signal_type in _SELL_ORIENTED_SIGNAL_TYPES or (
        decision is not None and decision.decision_class in _SELL_ORIENTED_DECISION_CLASSES
    )
    if is_sell_oriented and not holds_position:
        return _NO_POSITION_NOTE
    return ""


# ---------------------------------------------------------------------------
# Asama 5c, Bolum 1: pozisyonu olan/olmayan kullanici icin ayri yorum.
# ---------------------------------------------------------------------------

_ENTRY_SUITABILITY_LABELS = {
    DECISION_STRONG_BUY: "Alım adayı (güçlü)",
    DECISION_BUY: "Alım adayı",
    DECISION_WAIT_TRIGGER: "Tetik bekleniyor",
}


def _entry_suitability_block(signal: SignalResult, decision, liquidity_line: str) -> str:
    """Pozisyonu OLMAYAN kullanici icin yeni giris uygunlugu yorumu.
    TUT/KAR AL/AZALT/CIKIS gibi elde-olan-pozisyon ifadeleri KESINLIKLE kullanilmaz.
    """
    decision_class = decision.decision_class if decision is not None else None
    label = _ENTRY_SUITABILITY_LABELS.get(decision_class, "İşlem yok")
    lines = [f"Yeni giriş uygunluğu: {label}"]
    if signal.entry_zone and signal.entry_zone[0] is not None:
        lines.append(f"Alım bölgesi: {format_price(signal.entry_zone[0])} - {format_price(signal.entry_zone[1])}")
    lines.append(f"Tetik seviyesi: {format_price(signal.entry_trigger)}")
    if signal.stop_price is not None and signal.extras.get("close"):
        stop_distance_pct = (signal.extras['close'] - signal.stop_price) / signal.extras['close'] * 100
        lines.append(f"Stop mesafesi: {format_price(signal.stop_price)} (yaklaşık %{stop_distance_pct:.1f})")
    else:
        lines.append("Stop mesafesi: hesaplanamadı")
    lines.append(f"Risk/getiri oranı: {signal.risk_reward if signal.risk_reward is not None else '-'}")
    lines.append(liquidity_line)
    lines.append(f"Piyasa rejimi uygunluğu: {signal.market_regime}")
    return "\n".join(lines)


def _position_detail_block(position, current_price: float, decision, trend_direction: str, sr, portfolio_weight_pct=None) -> str:
    """Pozisyonu OLAN kullanici icin lot/maliyet/kar-zarar/teknik plan ve
    TUT/KISMI KAR AL/POZISYON AZALT/STOP-TAM CIKIS karari.
    Karar TEKNIK YAPIYA gore hesaplanir (bkz. app.analysis.position_advice_engine);
    kullanicinin maliyeti yalnizca kar/zarar gorunurlugu icindir.
    """
    advice: PositionAdviceResult = evaluate_position(
        lot=position.lot,
        average_cost=position.average_cost,
        current_price=current_price,
        technical_stop=position.stop_price if position.stop_price is not None else None,
        target_1=position.target_1,
        target_2=position.target_2,
        target_3=position.target_3,
        main_resistance=sr.main_resistance if sr else None,
        decision_class=decision.decision_class if decision is not None else None,
        trend_direction=trend_direction,
        portfolio_weight_pct=portfolio_weight_pct,
    )
    pnl_sign = "+" if advice.pnl_amount >= 0 else ""
    weight_txt = f"%{advice.portfolio_weight_pct:.1f}" if advice.portfolio_weight_pct is not None else "hesaplanamadı"
    partial_txt = (
        f"{format_price(advice.partial_profit_zone[0])} - {format_price(advice.partial_profit_zone[1])}"
        if advice.partial_profit_zone else "-"
    )
    reduce_txt = (
        f"{format_price(advice.reduce_zone[0])} - {format_price(advice.reduce_zone[1])}"
        if advice.reduce_zone else "-"
    )
    loss_txt = f"{advice.estimated_loss_if_stopped:+.2f} TL" if advice.estimated_loss_if_stopped is not None else "-"
    rationale_txt = "\n".join(f"• {r}" for r in advice.rationale)

    return f"""💼 POZİSYON DURUMU
Lot: {advice.lot}
Ortalama maliyet: {format_price(advice.average_cost)} TL
Güncel kâr/zarar: {pnl_sign}{advice.pnl_amount:.2f} TL (%{advice.pnl_percent:+.2f})
Portföy ağırlığı: {weight_txt}
Teknik stop: {format_price(advice.technical_stop)}
Hedef 1: {format_price(advice.target_1)}
Hedef 2: {format_price(advice.target_2)}
Hedef 3: {format_price(advice.target_3)}
Kısmi kâr alma bölgesi: {partial_txt}
Pozisyon azaltma bölgesi: {reduce_txt}
Tam çıkış seviyesi (stop): {format_price(advice.full_exit_level)}
Stop gerçekleşirse tahmini zarar: {loss_txt}

Karar: {advice.decision_label}
{rationale_txt}

(Bu karar kullanıcının maliyetine göre değil, teknik yapıya göre hesaplanmıştır.)"""


def _sr_confidence_line(label: str, price, confidence, touches: int, sources: list[str]) -> str:
    price_txt = format_price(price)
    if price is None:
        return f"• {label}: -"
    conf_txt = f"{confidence}/100" if confidence is not None else "-"
    src_txt = ", ".join(sources) if sources else "-"
    return f"• {label}: {price_txt} TL (güven: {conf_txt}, temas: {touches}, kaynaklar: {src_txt})"


def _trade_plan_block(
    entry_zone,
    entry_trigger,
    stop_price,
    target_1,
    target_2,
    target_3,
    risk_reward,
    invalidation_note: str,
    contextual_notes: list[str],
    trigger_label: str = "Tetik seviyesi",
    include_entry_zone: bool = True,
) -> str:
    """Gecerli bir islem plani varsa giris/tetik/stop/hedef 1-2-3/risk-getiriyi
    gosterir; yoksa fiyat UYDURMAZ, bunun yerine nedenini acikca aciklar."""
    if stop_price is None:
        reason = invalidation_note or "Guvenilir stop/hedef hesaplanamadi."
        notes_txt = ("\n" + "\n".join(f"• {n}" for n in contextual_notes)) if contextual_notes else ""
        return f"⚠️ Geçerli bir işlem planı hesaplanamadı: {reason}{notes_txt}"

    entry_low, entry_high = entry_zone if entry_zone else (None, None)
    lines = []
    if include_entry_zone:
        lines.append(f"Olası giriş bölgesi: {format_price(entry_low)} - {format_price(entry_high)}")
    lines.extend([
        f"{trigger_label}: {format_price(entry_trigger)}",
        f"Stop: {format_price(stop_price)}",
        f"Hedef 1: {format_price(target_1)}",
        f"Hedef 2: {format_price(target_2)}",
        f"Hedef 3: {format_price(target_3)}",
        f"Risk/Getiri: {risk_reward if risk_reward is not None else '-'}",
    ])
    if contextual_notes:
        lines.append("\n".join(f"• {n}" for n in contextual_notes))
    return "\n".join(lines)


def _decision_block(decision, holds_position: bool) -> str:
    if decision is None:
        return ""
    label = resolve_decision_label(decision, holds_position)
    lines = [f"Nihai karar: {label} (güven: {decision.confidence})"]
    if decision.liquidity is not None and not decision.liquidity.allow_strong_signal:
        lines.append("• Likidite yetersiz olduğu için güçlü sinyal sınıfı engellendi.")
    if decision.multi_timeframe is not None and decision.multi_timeframe.counter_trend_warning:
        lines.append("• Zaman dilimleri arasında karşı trend uyarısı var.")
    for reason in decision.gating_reasons[:4]:
        lines.append(f"• {reason}")
    return "\n".join(lines)


def _liquidity_summary_line(liquidity) -> str:
    if liquidity is None or not liquidity.available:
        return "Likidite: veri yetersiz"
    return f"Likidite: {liquidity.score}/100 ({liquidity.liquidity_class})"


def _multi_timeframe_summary_line(mtf) -> str:
    if mtf is None:
        return "Çoklu zaman dilimi: veri yetersiz"
    conflict_txt = " — çelişki var" if mtf.conflict else ""
    return f"Zaman dilimi uyumu: {mtf.confluence_score}/100{conflict_txt}"


def _data_quality_line(signal) -> str:
    quality = getattr(signal, "extras", {}).get("data_quality")
    if quality is None:
        return "Veri kalitesi: ölçülmedi"
    source_note = ""
    if getattr(quality, "cache_used", False):
        source_note = " — cache"
    elif getattr(quality, "fallback_used", False):
        source_note = " — fallback"
    status = getattr(getattr(quality, "status", None), "value", getattr(quality, "status", "-"))
    return f"Veri kalitesi: {status} {quality.score}/100 ({quality.provider}){source_note}"


# ---------------------------------------------------------------------------
# V3.2 (Asama 3): Anormal hareket / anomali mesajlari
# ---------------------------------------------------------------------------


def format_anomaly_scan(result, display_symbol: str) -> str:
    if not result.available:
        return f"🚨 {display_symbol} — ANOMALİ TARAMASI\n\n{result.note}"
    if not result.events:
        return f"🚨 {display_symbol} — ANOMALİ TARAMASI\n\nAnormal hareket tespit edilmedi. Sinyal/hacim/volatilite normal aralıkta."

    lines = []
    for event in result.events:
        lines.append(f"• [{event.severity.upper()}] {event.label_tr}: {event.description}")
    rel_vol_txt = f"\nGöreceli hacim: {result.relative_volume}" if result.relative_volume is not None else ""
    return (
        f"🚨 {display_symbol} — ANOMALİ TARAMASI\n\n"
        + "\n".join(lines)
        + rel_vol_txt
        + "\n\nBu mesaj yatırım tavsiyesi değildir."
    )


def format_anomaly_list(anomalies, since_hours: int) -> str:
    if not anomalies:
        return f"Son {since_hours} saatte izleme listende kayıtlı anomali yok."
    lines = []
    for a in anomalies:
        ts = a.detected_at.strftime("%d.%m %H:%M") if a.detected_at else "-"
        lines.append(f"• {ts} — {a.symbol} [{a.severity.upper()}] {a.description}")
    return f"🚨 SON {since_hours} SAAT — ANOMALİLER\n\n" + "\n".join(lines)


def _to_istanbul(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ist = dt.astimezone(timezone.utc) + timedelta(hours=ISTANBUL_OFFSET_HOURS)
    return ist.strftime("%d.%m.%Y %H:%M") + " (TR)"


def _rs_line(rs: RelativeStrengthResult, label: str) -> str:
    if not rs.available:
        return f"{label}: veri yetersiz ({rs.note})"
    return f"{label}: {rs.relative_score}/100 — {rs.classification} (20g fark: %{rs.diff_20d:+.2f}, 60g fark: %{rs.diff_60d:+.2f})"


def _news_block(news, ai_explanation: str | None = None) -> str:
    """V3.2 (Asama 4, bolum 4): /analiz ve /analiz_detay icin kompakt haber ozeti.

    `news` None ise veya haber yoksa bos dize doner (mesaja hicbir sey eklenmez)."""
    if news is None or not getattr(news, "available", False):
        return ""

    lines = ["📰 Haber özeti:"]
    lines.append(f"Son 24 saat: {news.count_24h} haber | Son 7 gün: {news.count_7d} haber")
    if news.impact_score is not None:
        lines.append(f"Haber etkisi: {news.impact_score:+.0f}/100 (güven: {news.confidence_score:.0f}/100)")
        lines.append(f"Skora katkısı: {news.score_contribution:+.2f} puan (en fazla ±3)")
    for event in news.top_events[:3]:
        lines.append(f"• {event.category_label_tr}: {event.impact_score:+.0f} — {event.rationale}")
    if ai_explanation:
        lines.append(f"🤖 AI özeti: {ai_explanation}")
    lines.append("(Haber etkisi tek başına AL/SAT sinyali oluşturmaz.)")
    return "\n".join(lines)



def format_short_summary(
    signal: SignalResult,
    display_symbol: str,
    mode: str,
    advanced_score: AdvancedScoreBreakdown,
    xu100_rs: RelativeStrengthResult,
    decision=None,
    holds_position: bool = False,
    news=None,
    position=None,
    portfolio_weight_pct=None,
) -> str:
    close = signal.extras.get("analysis_close", signal.extras.get("close"))
    current_price = signal.extras.get("current_price", close)
    trend_label = TREND_LABELS_TR.get(signal.extras.get("trend_direction", "sideways"), "Belirsiz")
    signal_label = resolve_signal_label(signal.signal_type, holds_position)
    header = "GÜN İÇİ ÖN ANALİZ" if mode == MODE_INTRADAY_PREVIEW else "KAPANIŞ ANALİZİ"

    sr = signal.support_resistance
    support_txt = format_price(sr.support_1) if sr else "-"
    resistance_txt = format_price(sr.resistance_1) if sr else "-"
    xu100_txt = f"{xu100_rs.relative_score}/100" if xu100_rs.available else "veri yok"

    intraday_note = ""
    if mode == MODE_INTRADAY_PREVIEW:
        intraday_note = "\n⚠️ Gün içi ön analiz: kesin sinyal değildir, kapanışa kadar değişebilir.\n"

    plan_block = _trade_plan_block(
        signal.entry_zone, signal.entry_trigger, signal.stop_price,
        signal.target_1, signal.target_2, signal.target_3, signal.risk_reward,
        signal.invalidation_note, signal.contextual_notes,
        trigger_label="Tetik", include_entry_zone=False,
    )
    decision_block = _decision_block(decision, holds_position)
    portfolio_note = _portfolio_note(signal.signal_type, decision, holds_position)
    news_block = _news_block(news)

    position_block = ""
    if holds_position and position is not None and current_price:
        position_block = _position_detail_block(
            position, current_price, decision, signal.extras.get("trend_direction", "sideways"), sr,
            portfolio_weight_pct=portfolio_weight_pct,
        )
    elif not holds_position:
        position_block = _entry_suitability_block(signal, decision, _liquidity_summary_line(decision.liquidity if decision else None))

    extra_blocks = "\n\n".join(b for b in [position_block, decision_block, portfolio_note, news_block] if b)
    extra_blocks_txt = f"\n\n{extra_blocks}" if extra_blocks else ""

    daily_change = signal.extras.get("daily_change_percent")
    daily_change_text = f"%{daily_change:+.2f}" if daily_change is not None else "Veri bulunamadı"
    fallback_note = "\n⚠️ Güncel fiyat alınamadı; son kesinleşmiş kapanış kullanılıyor." if not signal.extras.get("is_live_price", False) else ""

    text = f"""📊 {display_symbol} — {header}
{intraday_note}
Güncel fiyat: {format_price(current_price)} TL
Son kapanış: {format_price(close)} TL
Gün içi değişim: {daily_change_text}
Fiyat kaynağı: {signal.extras.get('current_price_source', signal.provider)}{fallback_note}
Sinyal: {signal_label}
Skor: {advanced_score.total}/100
{_data_quality_line(signal)}
Trend: {trend_label}
Piyasa rejimi: {signal.market_regime}
XU100 göreceli güç: {xu100_txt}

Destek: {support_txt}
Direnç: {resistance_txt}

{plan_block}{extra_blocks_txt}

Bu mesaj yatırım tavsiyesi değildir."""
    return enforce_message_limit(text)


def format_detailed_analysis(
    signal: SignalResult,
    display_symbol: str,
    mode: str,
    advanced_score: AdvancedScoreBreakdown,
    xu100_rs: RelativeStrengthResult,
    sector_rs,
    sector_name: str,
    intraday_quote: dict | None,
    warnings: list[str],
    decision=None,
    holds_position: bool = False,
    news=None,
    position=None,
    portfolio_weight_pct=None,
) -> str:
    close = signal.extras.get("analysis_close", signal.extras.get("close"))
    current_price = signal.extras.get("current_price", close)
    trend_label = TREND_LABELS_TR.get(signal.extras.get("trend_direction", "sideways"), "Belirsiz")
    signal_label = resolve_signal_label(signal.signal_type, holds_position)
    header = "GÜN İÇİ ÖN ANALİZ" if mode == MODE_INTRADAY_PREVIEW else "KESİNLEŞMİŞ KAPANIŞ ANALİZİ"

    sr = signal.support_resistance
    main_support = format_price(sr.main_support) if sr else "-"
    main_resistance = format_price(sr.main_resistance) if sr else "-"

    if sr is not None:
        support_lines = "\n".join([
            _sr_confidence_line("Destek 1", sr.support_1, sr.support_1_confidence, sr.support_1_touches, sr.support_1_sources),
            f"• Destek 2: {format_price(sr.support_2)}",
            f"• Ana destek: {main_support}" + (f" (güven: {sr.main_support_confidence}/100)" if sr.main_support_confidence is not None else ""),
        ])
        resistance_lines = "\n".join([
            _sr_confidence_line("Direnç 1", sr.resistance_1, sr.resistance_1_confidence, sr.resistance_1_touches, sr.resistance_1_sources),
            f"• Direnç 2: {format_price(sr.resistance_2)}",
            f"• Ana direnç: {main_resistance}" + (f" (güven: {sr.main_resistance_confidence}/100)" if sr.main_resistance_confidence is not None else ""),
        ])
    else:
        support_lines = "• Guvenilir seviye hesaplanamadi."
        resistance_lines = "• Guvenilir seviye hesaplanamadi."

    positive_reasons = [r for r in signal.reasons if not r.is_risk][:5]
    risk_reasons = [r for r in signal.reasons if r.is_risk][:5]
    reasons_text = "\n".join(f"• {r.description}" for r in positive_reasons) or "• (belirgin ek neden yok)"
    risks_text = "\n".join(f"• {r.description}" for r in risk_reasons) or "• Belirgin ek risk isaretlenmedi"

    intraday_block = ""
    if mode == MODE_INTRADAY_PREVIEW and intraday_quote:
        intraday_block = f"""
⚠️ GÜN İÇİ ÖN ANALİZ — piyasa açık, son mum henüz kesinleşmedi.
Gün içi fiyat (gecikmeli olabilir): {format_price(intraday_quote.get('price'))} TL
Bu analiz son TAMAMLANMIŞ kapanışa göre hesaplanmıştır.
Destek/direnç/stop/hedefler kapanışa kadar değişebilir.
"""

    warnings_text = "\n".join(f"⚠️ {w}" for w in warnings) if warnings else ""

    score_detail = f"""Skor detayı:
Trend: {advanced_score.trend}/20
Momentum: {advanced_score.momentum}/10
Hacim: {advanced_score.volume}/15
Destek/Direnç: {advanced_score.support_resistance}/15
XU100 gücü: {advanced_score.xu100_strength}/15{"  (veri yok, nötr)" if not advanced_score.xu100_strength_available else ""}
Sektör gücü: {advanced_score.sector_strength}/10{"  (veri yok, nötr)" if not advanced_score.sector_strength_available else ""}
Piyasa rejimi: {advanced_score.regime}/10
Risk/Getiri: {advanced_score.risk_reward}/5
Haber etkisi: {advanced_score.news_adjustment:+.2f} (en fazla ±3)
Toplam: {advanced_score.total}/100"""

    sector_line = f"Sektör: {sector_name}\n" + _rs_line(sector_rs, "Sektöre göre güç") if sector_rs else f"Sektör: {sector_name}"

    plan_block = _trade_plan_block(
        signal.entry_zone, signal.entry_trigger, signal.stop_price,
        signal.target_1, signal.target_2, signal.target_3, signal.risk_reward,
        signal.invalidation_note, signal.contextual_notes,
    )
    decision_block = _decision_block(decision, holds_position)
    portfolio_note = _portfolio_note(signal.signal_type, decision, holds_position)
    liquidity_line = _liquidity_summary_line(decision.liquidity if decision else None)
    mtf_line = _multi_timeframe_summary_line(decision.multi_timeframe if decision else None)
    news_block = _news_block(news)

    position_block = ""
    if holds_position and position is not None and current_price:
        position_block = _position_detail_block(
            position, current_price, decision, signal.extras.get("trend_direction", "sideways"), sr,
            portfolio_weight_pct=portfolio_weight_pct,
        )
    elif not holds_position:
        position_block = _entry_suitability_block(signal, decision, liquidity_line)

    extra_blocks = "\n\n".join(b for b in [position_block, decision_block, portfolio_note, news_block] if b)
    extra_blocks_txt = f"\n\n{extra_blocks}" if extra_blocks else ""

    current_ts = signal.extras.get("current_price_timestamp")
    current_ts_text = _to_istanbul(current_ts) if current_ts else "Veri bulunamadı"
    daily_change = signal.extras.get("daily_change_percent")
    daily_change_text = f"%{daily_change:+.2f}" if daily_change is not None else "Veri bulunamadı"
    fallback_note = "\n⚠️ Güncel fiyat alınamadı; son kesinleşmiş kapanış kullanılıyor." if not signal.extras.get("is_live_price", False) else ""

    return f"""📊 {display_symbol} — {header}
{intraday_block}
Güncel fiyat: {format_price(current_price)} TL
Son kapanış: {format_price(close)} TL
Son kesinleşmiş kapanış: {format_price(close)} TL
Gün içi değişim: {daily_change_text}
Güncel fiyat zamanı: {current_ts_text}
Fiyat kaynağı: {signal.extras.get('current_price_source', signal.provider)}{fallback_note}
Trend: {trend_label}
Sinyal: {signal_label}
Güven: {signal.confidence}
Piyasa yapısı: {signal.market_regime}
{liquidity_line}
{mtf_line}
{_data_quality_line(signal)}

{_rs_line(xu100_rs, "XU100'e göre durum")}
{sector_line}

Destekler:
{support_lines}

Dirençler:
{resistance_lines}

{plan_block}{extra_blocks_txt}

Ana nedenler:
{reasons_text}

Riskler:
{risks_text}

{score_detail}

{warnings_text}

Veri sağlayıcısı: {signal.provider}
Veri tarihi: {_to_istanbul(signal.data_timestamp)}

Bu mesaj yatırım tavsiyesi değildir, kurallı analiz çıktısıdır."""


def format_score_detail(signal: SignalResult, display_symbol: str, advanced_score: AdvancedScoreBreakdown) -> str:
    return f"""🔍 {display_symbol} — SKOR DETAYI

Trend: {advanced_score.trend}/20
Momentum: {advanced_score.momentum}/10
Hacim: {advanced_score.volume}/15
Destek/Direnç: {advanced_score.support_resistance}/15
XU100 gücü: {advanced_score.xu100_strength}/15
Sektör gücü: {advanced_score.sector_strength}/10
Piyasa rejimi: {advanced_score.regime}/10
Risk/Getiri: {advanced_score.risk_reward}/5

Toplam: {advanced_score.total}/100
Sinyal: {SIGNAL_TYPE_LABELS_TR.get(signal.signal_type, signal.signal_type)}
"""


def format_sector_info(symbol: str, sector_name: str, sector_index: str, rs: RelativeStrengthResult) -> str:
    return f"""🏭 {symbol} — Sektör Bilgisi

Sektör: {sector_name}
Sektör endeksi: {sector_index}

{_rs_line(rs, "Sektöre göre güç")}
"""


def format_sector_not_found(symbol: str) -> str:
    return f"'{symbol}' için sektör eşleştirmesi bulunamadı. /sektor_ayarla {symbol} <ENDEKS.IS> <Sektör Adı> ile ekleyebilirsin."


# ---------------------------------------------------------------------------
# Asama 5c, Bolum 2: /guc SEMBOL - gelismis XU100/sektor donemsel goreceli guc.
# ---------------------------------------------------------------------------

_PERIOD_LABELS = {
    PERIOD_1HAFTA: "1 hafta",
    PERIOD_1AY: "1 ay",
    PERIOD_3AY: "3 ay",
    PERIOD_6AY: "6 ay",
}

_TREND_TXT = {
    "guclenen": "📈 Güçleniyor",
    "zayiflayan": "📉 Zayıflıyor",
    "yatay": "➡️ Yatay",
    "veri_yetersiz": "Veri yetersiz",
}


def _format_periods_block(periods_result, benchmark_label: str) -> str:
    if periods_result is None:
        return f"{benchmark_label}: veri yetersiz (eşleştirme yok)."
    lines = [f"{benchmark_label} ({periods_result.benchmark_symbol}):"]
    missing_periods: list[str] = []
    for period_key in (PERIOD_1HAFTA, PERIOD_1AY, PERIOD_3AY, PERIOD_6AY):
        p = periods_result.periods.get(period_key)
        label = _PERIOD_LABELS[period_key]
        if p is None or not p.available:
            missing_periods.append(label)
            continue
        lines.append(
            f"  • {label}: hisse %{p.stock_return_pct:+.2f} | endeks %{p.benchmark_return_pct:+.2f} | "
            f"fark %{p.diff_pct:+.2f} | {p.strength_score}/100 — {classification_label(p.classification)}"
        )
    trend_txt = _TREND_TXT.get(periods_result.overall_trend, periods_result.overall_trend)
    lines.append(f"  Trend: {trend_txt}")
    if missing_periods:
        lines.append("  Veri eksikleri: " + ", ".join(missing_periods))
    return "\n".join(lines)


def format_guc(symbol: str, result) -> str:
    """`/guc SEMBOL` komutunun ciktisi: XU100 ve (varsa) sektore gore
    5/20/60/120 islem gunu (1 hafta/1 ay/3 ay/6 ay) donemsel goreceli guc.
    """
    xu100_block = _format_periods_block(result.xu100, "XU100'e göre")
    sector_label = f"{result.sector_name} sektörüne göre" if result.sector_name else "Sektöre göre"
    sector_block = _format_periods_block(result.sector, sector_label)
    quality = getattr(result, "data_quality", None)
    quality_line = (
        f"Veri: {quality.status.value} ({quality.score}/100) | {quality.provider}"
        if quality else "Veri: kalite bilgisi yok"
    )
    return f"""💪 {symbol} — GÖRECELİ GÜÇ (donemsel)

{quality_line}

{xu100_block}

{sector_block}

Not: Ortak işlem günü sayısı yetersiz olan dönemler için uydurma skor üretilmez, "veri yetersiz" olarak işaretlenir.
Bu mesaj yatırım tavsiyesi değildir."""


def format_market_breadth(breadth) -> str:
    if not breadth.available:
        return f"📈 Piyasa genişliği hesaplanamadı: {breadth.note}"
    def candidate_lines(rows, empty: str) -> str:
        if not rows:
            return empty
        return "\n".join(
            f"• {row.symbol} • {row.score}/100 • %{row.change_percent:+.2f} • "
            f"hacim {row.relative_volume:.1f}x\n  ↳ {', '.join(row.reasons[:3])}"
            for row in rows[:8]
        )

    ema200 = (
        f"%{breadth.above_ema200_ratio:.1f}"
        if breadth.above_ema200_ratio is not None else "veri yetersiz"
    )
    volume_ratio = (
        f"{breadth.up_down_volume_ratio:.2f}"
        if breadth.up_down_volume_ratio is not None else "hesaplanamadı"
    )
    return f"""📊 BIST • 571 HİSSE PİYASA RADARI
━━━━━━━━━━━━━━━━━━━━
🧭 Piyasa puanı: {breadth.breadth_score}/100 • {breadth.regime}
🔮 Sonraki seans eğilimi: {breadth.tomorrow_bias}

📡 VERİ KAPSAMI
• Doğrulanan: {breadth.scanned}/{breadth.universe_size} (%{breadth.coverage_ratio:.1f})
• Veri alınamayan/yetersiz: {breadth.failed}

📈 PİYASA GENİŞLİĞİ
• Yükselen: {breadth.advancers}  • Düşen: {breadth.decliners}  • Yatay: {breadth.unchanged}
• Net genişlik: {breadth.net_breadth:+d}  • Yükselen/Düşen: {breadth.advance_decline_ratio:.2f}
• Ortalama değişim: %{breadth.average_change_percent:+.2f}
• Medyan değişim: %{breadth.median_change_percent:+.2f}

📐 TREND VE HACİM
• EMA20 üstü: %{breadth.above_ema20_ratio:.1f}
• EMA50 üstü: %{breadth.above_ema50_ratio:.1f}
• EMA200 üstü: {ema200}
• Yeni 20 günlük zirve/dip: {breadth.new_20d_highs}/{breadth.new_20d_lows}
• Hacmi ortalamanın üstünde: %{breadth.rising_volume_ratio:.1f}
• Yükselen/Düşen hacim oranı: {volume_ratio}

🟢 LONG ADAYLARI • {breadth.long_count}
{candidate_lines(breadth.long_candidates, '• Puan eşiğini geçen teyitli aday yok.')}

🔴 SHORT/RİSK ADAYLARI • {breadth.short_count}
{candidate_lines(breadth.short_candidates, '• Puan eşiğini geçen teknik zayıflık adayı yok.')}

⚪ Nötr/teyitsiz: {breadth.neutral_count}

ℹ️ {breadth.note}
⚠️ Bu sınıflama kapanmış günlük barlardan üretilir; tek başına emir değildir. Giriş için seviye, kapanış ve risk/getiri teyidi gerekir."""


def format_breadth_candidate_messages(breadth, direction: str = "tum") -> list[str]:
    """İstenirse puan eşiğini geçen bütün LONG/SHORT-RİSK adlarını sayfalar."""

    normalized = str(direction or "tum").strip().casefold()
    groups = []
    if normalized in {"tum", "tüm", "long"}:
        groups.append(("🟢 LONG ADAYLARI", breadth.long_candidates))
    if normalized in {"tum", "tüm", "short", "risk"}:
        groups.append(("🔴 SHORT/RİSK ADAYLARI", breadth.short_candidates))
    messages: list[str] = []
    for title, rows in groups:
        header = f"{title} • {len(rows)} hisse\n━━━━━━━━━━━━━━━━━━\n"
        current = header
        if not rows:
            messages.append(current + "Puan eşiğini geçen aday yok.")
            continue
        for rank, row in enumerate(rows, start=1):
            block = (
                f"{rank:03d}. {row.symbol} • {row.score}/100 • %{row.change_percent:+.2f} • "
                f"hacim {row.relative_volume:.1f}x\n"
                f"     {', '.join(row.reasons[:4])}\n"
            )
            if len(current) + len(block) > 3900:
                messages.append(current.rstrip())
                current = f"{title} • DEVAM\n━━━━━━━━━━━━━━━━━━\n{block}"
            else:
                current += block
        current += "\n⚠️ İzleme sınıfıdır; teyitsiz emir değildir."
        messages.append(current.rstrip())
    return messages


def format_performance_report(report) -> str:
    if not report.is_reliable:
        return f"📊 PERFORMANS RAPORU ({report.period_days} gün)\n\n{report.note}\n(Örnek sayısı: {report.sample_size})"

    return f"""📊 PERFORMANS RAPORU ({report.period_days} gün)

Toplam sinyal: {report.total_signals}  Aktif: {report.active_signals}  Örneklem: {report.sample_size}

Hedef 1 başarı: %{report.target_1_hit_rate}
Hedef 2 başarı: %{report.target_2_hit_rate}
Hedef 3 başarı: %{report.target_3_hit_rate}
Stop oranı: %{report.stop_hit_rate}

Ortalama getiri: %{report.average_return_percent}
Ortalama zarar: %{report.average_loss_percent}
Ortalama R: {report.average_r_multiple}
Profit factor: {report.profit_factor}
Beklenen değer: {report.expected_value}
Ortalama süre: {report.average_duration_days} gün

En iyi sinyal türü: {report.best_signal_type}
En kötü sinyal türü: {report.worst_signal_type}

Bu rapor yatırım tavsiyesi değildir.
"""


TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def split_long_message(text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Telegram mesaj sinirini asan metinleri birden fazla mesaja boler.
    Satir sinirlarindan bolmeye calisir (kelime/satir ortasindan kesmez).
    """
    if len(text) <= max_length:
        return [text]

    parts: list[str] = []
    lines = text.split("\n")
    current = ""
    for line in lines:
        candidate = (current + "\n" + line) if current else line
        if len(candidate) > max_length:
            if current:
                parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def format_evening_report(
    scan_date: str,
    market_regime: str,
    xu100_daily_change: float | None,
    symbols_scanned: int,
    symbols_succeeded: int,
    symbols_failed: int,
    top_candidates: list[dict],
    top_risks: list[dict],
    upcoming_breakouts: list[str],
) -> str:
    xu100_line = f"%{xu100_daily_change:+.2f}" if xu100_daily_change is not None else "veri yok"

    candidates_text = ""
    for i, c in enumerate(top_candidates, 1):
        candidates_text += f"""{i}. {c['symbol']} — Skor {c['score']}/100
   Sinyal: {c['signal_type']}
   Son kapanış: {format_price(c.get('close'))}
   Tetik: {format_price(c.get('entry_trigger'))}
   Stop: {format_price(c.get('stop_price'))}
   Hedef 1: {format_price(c.get('target_1'))}
   Risk/Getiri: {c.get('risk_reward', '-')}
   XU100'e göre güç: {c.get('xu100_score', '-')}
"""
    if not candidates_text:
        candidates_text = "(Bu taramada güçlü aday bulunamadı.)\n"

    risks_text = ""
    for i, r in enumerate(top_risks, 1):
        risks_text += f"""{i}. {r['symbol']}
   Risk skoru: {r['score']}/100
   Kırılan destek: {r.get('broken_support', '-')}
   Ana neden: {r.get('main_reason', '-')}
"""
    if not risks_text:
        risks_text = "(Bu taramada belirgin risk artışı bulunamadı.)\n"

    breakouts_text = "\n".join(f"• {b}" for b in upcoming_breakouts) or "(Yarın için özel bir kırılım takibi yok.)"

    return f"""🏔️ MONTANA FİNANS ROBOTU HİSSE BOT — KAPANIŞ RAPORU

Tarih: {scan_date}
Piyasa rejimi: {market_regime}
XU100 günlük değişim: {xu100_line}
Taranan hisse: {symbols_scanned}
Başarılı analiz: {symbols_succeeded}
Veri alınamayan: {symbols_failed}

En güçlü adaylar:

{candidates_text}
En çok risk artanlar:

{risks_text}
Yarın takip edilecek kırılımlar:
{breakouts_text}

Bu rapor yatırım tavsiyesi değildir.
"""


def format_position_size_result(
    symbol: str, sizing, entry: float, stop: float, target_1: float | None,
    risk_reward: float | None, sector_warning: str | None
) -> str:
    sector_line = f"\n⚠️ {sector_warning}" if sector_warning else ""
    return f"""📐 {symbol} — POZİSYON BÜYÜKLÜĞÜ

Giriş: {format_price(entry)}
Stop: {format_price(stop)}
Risk tutarı: {sizing.risk_amount}
Lot: {sizing.lot}
Tahmini pozisyon tutarı: {sizing.position_value}
Portföy ağırlığı: %{sizing.position_percent_of_capital}
Hedef 1: {format_price(target_1)}
Risk/Getiri: {risk_reward if risk_reward is not None else "-"}{sector_line}
"""


# ---------------------------------------------------------------------------
# V3.1: /gunici - gun ici on analiz (bolum 3)
# ---------------------------------------------------------------------------


def _significant_news_alert_line(news) -> str:
    """/gunici icin: yalnizca COK ONEMLI ve GUNCEL bir haber varsa kisa uyari
    doner (bolum 4 spesifikasyonu); aksi halde bos dize doner."""
    if news is None or not getattr(news, "available", False) or not news.top_events:
        return ""
    top = news.top_events[0]
    is_recent = top.news_age_hours is not None and top.news_age_hours <= 24
    is_significant = abs(top.impact_score) >= 50 and top.confidence_score >= 60
    if is_recent and is_significant:
        return f"\n📰 Önemli güncel haber: {top.category_label_tr} ({top.impact_score:+.0f}) — {top.rationale}\n"
    return ""


def format_intraday_preview(result, display_symbol: str, holds_position: bool = False, news=None) -> str:
    from app.services.intraday_service import CLASS_VERI_YETERSIZ

    if result.classification == CLASS_VERI_YETERSIZ:
        detail = "\n".join(f"• {w}" for w in result.warnings)
        return (
            f"⚡ {display_symbol} — GÜN İÇİ ÖN ANALİZ\n\n"
            "Yeterli gün içi veri olmadığı için ön analiz üretilemedi.\n"
            f"{detail}"
        )

    change_txt = (
        f"%{result.daily_change_percent:+.2f}" if result.daily_change_percent is not None else "-"
    )
    delay_txt = "⚠️ Gün içi veri gecikmeli olabilir." if result.is_delayed else ""
    last_update_txt = result.last_update.strftime("%d.%m.%Y %H:%M") if result.last_update else "-"

    sr = result.support_resistance
    if sr is not None:
        sr_block = "\n".join([
            _sr_confidence_line("Destek 1", sr.support_1, sr.support_1_confidence, sr.support_1_touches, sr.support_1_sources),
            _sr_confidence_line("Direnç 1", sr.resistance_1, sr.resistance_1_confidence, sr.resistance_1_touches, sr.resistance_1_sources),
        ])
    else:
        sr_block = "• Guvenilir seviye hesaplanamadi."

    plan_block = _trade_plan_block(
        result.entry_zone, result.entry_trigger, result.stop_price,
        result.target_1, result.target_2, result.target_3, result.risk_reward,
        result.invalidation_note, result.contextual_notes,
    )

    decision = result.decision
    classification_label = result.classification
    if decision is not None:
        classification_label = resolve_decision_label(decision, holds_position)
    decision_block = _decision_block(decision, holds_position)
    portfolio_note = ""
    if result.classification == "Gün içi risk arttı" and not holds_position:
        portfolio_note = _NO_POSITION_NOTE
    liquidity_line = _liquidity_summary_line(result.liquidity)
    mtf_line = _multi_timeframe_summary_line(result.multi_timeframe)

    extra_blocks = "\n\n".join(b for b in [decision_block, portfolio_note] if b)
    extra_blocks_txt = f"\n\n{extra_blocks}" if extra_blocks else ""
    news_alert_line = _significant_news_alert_line(news)
    quality_line = "Veri kalitesi: -"
    if getattr(result, "data_quality", None) is not None:
        quality = result.data_quality
        source_note = " | fallback" if quality.fallback_used else (" | cache" if quality.cache_used else "")
        quality_line = f"Veri kalitesi: {quality.status.value} ({quality.score}/100) | {quality.provider}{source_note}"

    price_context = getattr(result, "current_price_context", None)
    price_metadata = ""
    if price_context is not None:
        price_metadata = "\n".join(_stage5e_price_block(price_context, getattr(result, "data_quality", None))) + "\n\n"

    return f"""⚡ {display_symbol} — GÜN İÇİ ÖN ANALİZ
{news_alert_line}
{price_metadata}Durum: {result.state} (kesinleşmiş kapanış sinyali DEĞİLDİR)
Sınıf: {classification_label}
{liquidity_line}
{mtf_line}

Gün içi son fiyat: {format_price(result.last_price)}
Bugünkü açılış: {format_price(result.today_open)}
Gün içi en yüksek: {format_price(result.today_high)}
Gün içi en düşük: {format_price(result.today_low)}
Önceki kapanış: {format_price(result.previous_close)}
Günlük değişim: {change_txt}
Gün içi hacim: {result.today_volume if result.today_volume is not None else "-"}
Ortalama hacme oran: {result.relative_volume if result.relative_volume is not None else "-"}
Son güncelleme: {last_update_txt}
{quality_line}
{delay_txt}

Gün içi trend: {result.intraday_trend}
Günlük ana trend: {result.daily_main_trend}

Destek/Direnç:
{sr_block}

{plan_block}{extra_blocks_txt}

Gün içi VWAP: {format_price(result.vwap)}
Gün içi RSI: {result.rsi if result.rsi is not None else "-"}
Gün içi MACD histogram: {result.macd_histogram if result.macd_histogram is not None else "-"}
Anormal hareket durumu: {"Evet" if result.is_anomalous else "Hayır"}

Bu mesaj yatırım tavsiyesi değildir.
"""


# ---------------------------------------------------------------------------
# V3.1: /zaman_dilimleri - coklu zaman dilimi analizi (bolum 4)
# ---------------------------------------------------------------------------


def format_multi_timeframe(result, display_symbol: str, price_context=None) -> str:
    tf_labels = {
        "1wk": "Haftalık", "1d": "Günlük", "4h": "4 Saatlik",
        "1h": "1 Saatlik", "15m": "15 Dakika", "5m": "5 Dakika",
    }

    missing = MissingDataCollector()

    def _line(tf: str) -> str:
        snap = result.snapshots.get(tf)
        if snap is None or not snap.available:
            missing.add(tf_labels[tf])
            return f"{tf_labels[tf]}: Veri yok"
        return f"{tf_labels[tf]}: {public_label(snap.trend_class)} · {score_text(snap.trend_strength)} · Veri uygun"

    current_price = getattr(price_context, "current_price", None)
    analysis_close = getattr(price_context, "analysis_close", None)
    source = getattr(price_context, "current_price_source", "Veri bulunamadı")
    price_ts = getattr(price_context, "current_price_timestamp", None)
    if current_price is None:
        for tf in ("5m", "15m", "1h", "4h", "1d", "1wk"):
            snap = result.snapshots.get(tf)
            if snap is not None and snap.available and snap.close is not None:
                current_price = snap.close
                source = f"{tf} tamamlanmış mum"
                price_ts = snap.last_bar_timestamp
                break
    if analysis_close is None:
        daily = result.snapshots.get("1d")
        analysis_close = daily.close if daily and daily.available else None

    available_count = sum(
        1 for tf in tf_labels if (result.snapshots.get(tf) is not None and result.snapshots[tf].available)
    )
    confluence = score_text(result.confluence_score) if available_count else "Veri yetersiz"
    reversal_txt = "Aday; kapanış teyidi bekleniyor" if getattr(result, "trend_reversal", False) else "Yok"
    lines = [
        f"🕰 MONTANA FİNANS ROBOTU HİSSE BOT — {display_symbol} ÇOKLU ZAMAN DİLİMİ",
        f"Güncel fiyat: {price_text(current_price)}",
        f"Son kapanış: {price_text(analysis_close)}",
        f"Fiyat kaynağı: {source}",
        f"Fiyat zamanı: {_to_istanbul(price_ts) if price_ts is not None else '-'}",
        "",
        "UZUN VADE",
        _line("1wk"),
        _line("1d"),
        "",
        "ORTA VADE",
        _line("4h"),
        _line("1h"),
        "",
        "KISA VADE",
        _line("15m"),
        _line("5m"),
        "",
        "SONUÇ",
        f"Ana yön: {public_label(result.primary_direction)}",
        f"Kısa yön: {public_label(result.short_term_direction)}",
        f"Uyum skoru: {confluence}",
        f"Çelişki: {'Var' if result.conflict else 'Yok'}",
        f"Dönüşüm teyidi: {reversal_txt}",
        f"Veri kalitesi: {optional_text(getattr(result, 'data_quality', None), missing='Kısmi')}",
    ]
    lines.extend(missing.lines())
    lines.append("Bu mesaj yatırım tavsiyesi değildir.")
    return enforce_message_limit("\n".join(lines))


# ---------------------------------------------------------------------------
# V3.1: /likidite - likidite filtresi (bolum 5)
# ---------------------------------------------------------------------------


def format_liquidity(result, display_symbol: str) -> str:
    from app.analysis.liquidity_engine import LIQUIDITY_LOW, LIQUIDITY_VERY_LOW

    if not result.available:
        return f"💧 {display_symbol} — LİKİDİTE\n\n{result.risk_note or 'Likidite hesaplamak için yeterli veri yok.'}"

    class_labels = {
        "cok_yuksek": "Çok yüksek likidite",
        "yuksek": "Yüksek likidite",
        "orta": "Orta likidite",
        "dusuk": "Düşük likidite",
        "cok_dusuk": "Çok düşük likidite",
        "veri_yetersiz": "Veri yetersiz",
    }
    class_label = class_labels.get(result.liquidity_class, result.liquidity_class)
    risk_line = f"\nRisk: {result.risk_note}" if result.liquidity_class in (LIQUIDITY_LOW, LIQUIDITY_VERY_LOW) else ""
    reasons_txt = "\n".join(f"• {r}" for r in result.reasons)

    return f"""💧 {display_symbol} — LİKİDİTE

Likidite: {result.score}/100 — {class_label}{risk_line}

20g ortalama hacim: {result.avg_volume_20d}
60g ortalama hacim: {result.avg_volume_60d}
20g ortalama işlem tutarı: {result.avg_turnover_20d_try} TL
Son işlem tutarı: {result.last_turnover_try} TL
Göreceli hacim: {result.relative_volume}
Hacim istikrarı: {result.volume_stability}
Hacim düşüşü: {"Evet" if result.volume_declining else "Hayır"}
Anormal hacim: {"Evet" if result.abnormal_volume else "Hayır"}
Fiyat-hacim uyumu: {"Uyumlu" if result.price_volume_harmony else "Zayıf"}
Gap sıklığı: {result.gap_frequency}
Günlük ATR yüzdesi: %{result.atr_percent if result.atr_percent is not None else "-"}
Ani fiyat sıçraması: {"Evet" if result.sudden_price_jump else "Hayır"}
Manipülasyon riski: {"Evet" if result.manipulation_risk else "Hayır"}
Güçlü sinyale izin: {"Evet" if result.allow_strong_signal else "Hayır"}

Gerekçeler:
{reasons_txt}

Bu mesaj yatırım tavsiyesi değildir.
"""


# ---------------------------------------------------------------------------
# V3.2 (Asama 4): Haber radari, haber detayi, haber taramasi, AI aciklama.
# ---------------------------------------------------------------------------


def format_news_list(symbol: str, articles: list) -> str:
    """/haberler SEMBOL icin kisa haber listesi."""
    if not articles:
        return f"📰 {symbol} — HABERLER\n\nSon dönemde takip edilen bir haber bulunamadı."

    lines = [f"📰 {symbol} — SON HABERLER\n"]
    for a in articles:
        date_txt = a.published_at.strftime("%d.%m.%Y %H:%M") if a.published_at else "tarih bilinmiyor"
        dup_note = f" (+{a.duplicate_source_count - 1} kaynak)" if (a.duplicate_source_count or 1) > 1 else ""
        lines.append(f"• [{date_txt}] {a.title}{dup_note}\n  Kaynak: {a.source or '-'} | {a.url}")
    lines.append("\nDetaylı etki analizi için /haber_detay " + symbol)
    return "\n".join(lines)


def format_news_detail(symbol: str, summary_24h, summary_7d) -> str:
    """/haber_detay SEMBOL icin haber etkisi detayi (NewsImpactEngine ciktisi)."""
    lines = [f"📰 {symbol} — HABER ETKİSİ DETAYI\n"]

    def _summary_block(label: str, summary) -> str:
        if summary is None or not summary.available:
            return f"{label}: haber bulunamadı."
        block = [f"{label}: {summary.article_count} haber"]
        if summary.impact_score is not None:
            block.append(f"Ortalama etki: {summary.impact_score:+.0f}/100 (güven: {summary.confidence_score:.0f}/100)")
            block.append(f"Skora katkı: {summary.score_contribution:+.2f} puan (en fazla ±3)")
        else:
            block.append("Şirket eşleşme güveni düşük olduğu için skora dahil edilmedi.")
        for a in summary.top_assessments[:3]:
            block.append(f"  • {a.category_label_tr}: {a.impact_score:+.0f} (güven {a.confidence_score:.0f}/100) — {a.rationale}")
        return "\n".join(block)

    lines.append(_summary_block("Son 24 saat", summary_24h))
    lines.append("")
    lines.append(_summary_block("Son 7 gün", summary_7d))
    lines.append("\n(Haber etkisi tek başına AL/SAT sinyali oluşturmaz; kurallı ve açıklanabilir bir yardımcı göstergedir.)")
    return "\n".join(lines)


def format_news_radar(scan_results: list[tuple[str, object]]) -> str:
    """/haber_radari icin izleme listesindeki semboller arasinda ONEMLI haberi
    olanlarin ozeti. `scan_results`: (symbol, NewsImpactSummary|None) listesi."""
    significant = [(s, r) for s, r in scan_results if r is not None and r.available and r.impact_score is not None and abs(r.impact_score) >= 30]

    if not significant:
        return "📡 HABER RADARI\n\nİzleme listendeki semboller için önemli bir haber tespit edilmedi."

    lines = ["📡 HABER RADARI — Önemli haberler\n"]
    for symbol, summary in sorted(significant, key=lambda x: abs(x[1].impact_score), reverse=True):
        lines.append(f"• {symbol}: {summary.impact_score:+.0f}/100 etki, {summary.article_count} haber (güven {summary.confidence_score:.0f}/100)")
    lines.append("\nDetay için /haber_detay SEMBOL yazabilirsin.")
    return "\n".join(lines)


def format_ai_explanation(symbol: str, explanation: str, is_fallback: bool) -> str:
    """/ai_aciklama SEMBOL icin Groq (veya deterministik yedek) aciklamasi."""
    source_note = "kural tabanlı özet (Groq kullanılmadı)" if is_fallback else "Groq AI özeti"
    return (
        f"🤖 {symbol} — AI AÇIKLAMA ({source_note})\n\n"
        f"{explanation}\n\n"
        "Not: Bu açıklama fiyat/hedef/stop üretmez ve AL/SAT kararını değiştirmez; "
        "yalnızca mevcut kurallı analiz çıktısını sade dille anlatır.\n"
        "Bu mesaj yatırım tavsiyesi değildir."
    )


# ---------------------------------------------------------------------------
# MERGEN QUANT - Asama 5: /seviyeler (gunluk/haftalik/aylik destek-direnc +
# cakisan guclu bolgeler)
# ---------------------------------------------------------------------------

_TIMEFRAME_LABELS_TR = {"gunluk": "Günlük", "haftalik": "Haftalık", "aylik": "Aylık"}


def _format_level_line(label: str, level) -> str:
    if level is None:
        return f"{label}: —"
    range_text = level.as_range_text()
    last_test = getattr(level, "last_test_date", None) or "-"
    distance = getattr(level, "distance_percent", 0.0)
    strength = getattr(level, "strength_class", "-")
    next_text = ""
    if getattr(level, "next_zone_low", None) is not None:
        next_text = f" | Kırılırsa sonraki: {level.next_zone_low:.2f}-{level.next_zone_high:.2f} TL"
    return (
        f"{label}: {range_text} (güven {level.confidence:.0f}/100, {strength}, "
        f"{level.touches} temas, uzaklık %{distance:.2f}, son test {last_test}){next_text}"
    )


def _format_timeframe_block(tf_result) -> list[str]:
    label = _TIMEFRAME_LABELS_TR.get(tf_result.timeframe, tf_result.timeframe.title())
    lines = [f"— {label} Seviyeler —"]
    if not tf_result.reliable:
        lines.append(tf_result.note or "Güvenilir seviye hesaplanamadı.")
        return lines
    support = tf_result.main_support or tf_result.support_1
    resistance = tf_result.main_resistance or tf_result.resistance_1
    lines.append(_format_level_line(f"{label} Ana Destek", support))
    lines.append(_format_level_line(f"{label} Ana Direnç", resistance))
    return lines


def format_seviyeler(
    symbol: str,
    current_price: float,
    levels_result,
    confluence_supports: list,
    confluence_resistances: list,
    price_context=None,
    quality=None,
) -> str:
    """/seviyeler SEMBOL icin gunluk/haftalik/aylik destek-direnc ve
    cakisan guclu bolgeler mesaji."""
    lines = [f"📐 MONTANA FİNANS ROBOTU HİSSE BOT — {symbol} SEVİYELER"]
    if price_context is not None:
        lines.extend(_stage5e_price_block(price_context, quality))
    else:
        lines.append(f"Son fiyat: {format_price(current_price)} TL")
    lines.append("")

    for tf_result in (levels_result.daily, levels_result.weekly, levels_result.monthly):
        lines.extend(_format_timeframe_block(tf_result))
        lines.append("")

    lines.append("— Çakışan Güçlü Bölgeler —")
    if confluence_supports:
        best = confluence_supports[0]
        tf_text = ", ".join(sorted(best.timeframes))
        lines.append(
            f"GÜÇLÜ DESTEK BÖLGESİ: {best.low:.2f}–{best.high:.2f} TL\n"
            f"Güven: {best.confidence:.0f}/100 | Çakışan zaman dilimleri: {tf_text}"
        )
    else:
        lines.append("Güçlü ortak destek bölgesi tespit edilmedi.")

    if confluence_resistances:
        best = confluence_resistances[0]
        tf_text = ", ".join(sorted(best.timeframes))
        lines.append(
            f"GÜÇLÜ DİRENÇ BÖLGESİ: {best.low:.2f}–{best.high:.2f} TL\n"
            f"Güven: {best.confidence:.0f}/100 | Çakışan zaman dilimleri: {tf_text}"
        )
    else:
        lines.append("Güçlü ortak direnç bölgesi tespit edilmedi.")

    lines.append("\nBu mesaj yatırım tavsiyesi değildir; kural tabanlı, açıklanabilir seviye analizidir.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MERGEN QUANT - Asama 5b: /senaryo (dusus/yukselis senaryo bolgeleri)
# ---------------------------------------------------------------------------


def _format_scenario_line(label: str, zone) -> list[str]:
    if zone is None:
        return []
    return [f"- {label}: {price_text(zone.low)}–{price_text(zone.high)} · Teknik destek {score_text(zone.confidence)}"]


def format_price_scenarios(symbol: str, current_price: float, result, price_context=None, quality=None) -> str:
    """/senaryo SEMBOL icin dusus/yukselis senaryo bolgeleri mesaji."""
    lines = [f"🧭 MONTANA FİNANS ROBOTU HİSSE BOT — {symbol} SENARYOLAR"]
    if price_context is not None:
        lines.extend(_stage5e_price_block(price_context, quality))
    else:
        lines.append(f"Son fiyat: {format_price(current_price)} TL")
    lines.append("")

    if not result.reliable:
        lines.append(result.note or "Güvenilir senaryo hesaplanamadı.")
        lines.append("\nBu mesaj yatırım tavsiyesi değildir.")
        return "\n".join(lines)

    lines.append("DÜŞÜŞ SENARYOLARI")
    lines.extend(_format_scenario_line("Yakın geri çekilme", result.decline_near))
    lines.extend(_format_scenario_line("Ana dip bölgesi", result.decline_main))
    lines.extend(_format_scenario_line("Aşırı negatif senaryo", result.decline_extreme))
    if result.decline_near is None and result.decline_main is None and result.decline_extreme is None:
        lines.append("Düşüş senaryosu için yeterli teknik dayanak bulunamadı.")
        lines.append("")

    lines.append("YÜKSELİŞ SENARYOLARI")
    lines.extend(_format_scenario_line("Yakın hedef", result.rise_near))
    lines.extend(_format_scenario_line("Ana hedef", result.rise_main))
    lines.extend(_format_scenario_line("Güçlü kırılım senaryosu", result.rise_breakout))
    # Varsayılan mesajda en fazla üç yükseliş bölgesi gösterilir.
    if result.rise_near is None and result.rise_main is None and result.rise_breakout is None and result.rise_extreme is None:
        lines.append("Yükseliş senaryosu için yeterli teknik dayanak bulunamadı.")
        lines.append("")

    lines.append(
        "Not: Bu bölgeler teknik olarak izlenen senaryolardır, kesin fiyat tahmini değildir; "
        "her biri belirtilen koşul gerçekleşirse güçlenir."
    )
    lines.append("Bu mesaj yatırım tavsiyesi değildir.")
    return enforce_message_limit("\n".join(lines))


# ---------------------------------------------------------------------------
# MERGEN QUANT - Asama 5b: /kirilsanaryo ("bu seviye kirilirsa ne olur?")
# ---------------------------------------------------------------------------


def _format_breakout_case(case) -> list[str]:
    if case is None:
        return ["İlgili seviye için yeterli teknik dayanak bulunamadı.", ""]
    if case.kind == "direnc_kirilimi":
        event = "kırıldı" if case.level_already_broken else "kırılırsa"
        lines = [
            f"🔺 {case.level_low:.2f}-{case.level_high:.2f} direnci {event} → "
            f"olası hedef: {case.target_1:.2f} ({case.target_1_reason})",
            f"Teyit: {case.confirmation_close_level:.2f} üzerinde hacimli tamamlanmış mum kapanışı",
            f"- Gerekli hacim: {case.min_volume_note}",
            f"- TP1: {case.target_1:.2f} TL • {case.target_1_reason}",
            f"- TP2: {case.target_2:.2f} TL • {case.target_2_reason}",
            f"- {case.failure_level:.2f} TL altına dönüş: sahte kırılım riski",
            f"- Sahte kırılım riski: {case.false_breakout_risk} ({case.false_breakout_note})",
            "",
        ]
    else:
        event = "kırıldı" if case.level_already_broken else "kırılırsa"
        lines = [
            f"🔻 {case.level_low:.2f}-{case.level_high:.2f} desteği {event} → "
            f"olası hedef: {case.target_1:.2f} ({case.target_1_reason})",
            f"Teyit: {case.confirmation_close_level:.2f} altında hacimli tamamlanmış mum kapanışı",
            f"- Gerekli hacim: {case.min_volume_note}",
            f"- TP1: {case.target_1:.2f} TL • {case.target_1_reason}",
            f"- TP2: {case.target_2:.2f} TL • {case.target_2_reason}",
            f"- {case.failure_level:.2f} TL üzerine dönüş: senaryo yeniden pozitife döner",
            f"- Sahte kırılım riski: {case.false_breakout_risk} ({case.false_breakout_note})",
            "",
        ]
    return lines


def format_breakout_scenarios(symbol: str, current_price: float, result, price_context=None, quality=None) -> str:
    """/kirilsanaryo SEMBOL icin 'bu seviye kirilirsa ne olur?' mesaji."""
    lines = [f"⚡ MONTANA FİNANS ROBOTU HİSSE BOT — {symbol} KIRILIM SENARYOLARI"]
    if price_context is not None:
        lines.extend(_stage5e_price_block(price_context, quality))
    else:
        lines.append(f"Son fiyat: {format_price(current_price)} TL")
    lines.append("")

    if not result.reliable:
        lines.append(result.note or "Güvenilir seviye hesaplanamadı.")
        lines.append("\nBu mesaj yatırım tavsiyesi değildir.")
        return "\n".join(lines)

    lines.append("DİRENÇ KIRILIRSA:")
    lines.extend(_format_breakout_case(result.resistance_breakout))

    lines.append("DESTEK KIRILIRSA:")
    lines.extend(_format_breakout_case(result.support_breakdown))

    lines.append("Not: Hedefler kesin tahmin değildir; hacim ve kapanış doğrulaması olmadan kırılım güçlü sayılmaz.")
    lines.append("Bu mesaj yatırım tavsiyesi değildir.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MERGEN QUANT — Aşama 5e mesajları
# ---------------------------------------------------------------------------


def _stage5e_price_block(price, quality=None) -> list[str]:
    timestamp = _to_istanbul(price.current_price_timestamp) if price.current_price_timestamp else "Veri bulunamadı"
    change = format_percent_safe(price.daily_change_percent)
    lines = [
        f"Güncel fiyat: {format_price_safe(price.current_price)}",
        f"Son kesinleşmiş kapanış: {format_price_safe(price.analysis_close)}",
        f"Gün içi değişim: {change}",
        f"Güncel fiyat zamanı: {timestamp}",
        f"Fiyat kaynağı: {price.current_price_source}",
    ]
    if quality is not None:
        lines.append(f"Veri kalitesi: {quality.status.value} ({quality.score}/100) | {quality.provider}")
    if price.warning:
        lines.append(price.warning)
    return lines


def format_price_metadata(price, quality=None) -> str:
    return "\n".join(_stage5e_price_block(price, quality))


def format_long_term_scenarios(symbol: str, price, result, quality=None) -> str:
    missing = MissingDataCollector()
    lines = [
        f"🧭 MONTANA FİNANS ROBOTU HİSSE BOT — {symbol} UZUN VADE",
        "",
        f"Güncel fiyat: {price_text(price.current_price)}",
        f"Son kesinleşmiş kapanış: {price_text(price.analysis_close)}",
        f"Uzun vadeli ana trend: {public_label(getattr(result, 'long_term_trend', None))}",
        f"Kanıt gücü: {score_text(getattr(result, 'evidence_strength', None))}",
        f"Veri kalitesi: {score_text(getattr(quality, 'score', None))}",
        f"Fiyat kaynağı: {public_label(getattr(price, 'current_price_source', None))}",
    ]
    if not result.reliable:
        lines.extend(["", result.note or "Uzun vadeli senaryo için veri yetersiz."])
        missing.extend(getattr(result, "missing_data", None))
        lines.extend(missing.lines())
        return enforce_message_limit("\n".join(lines))

    def distinct_zones(items):
        selected = []
        seen = set()
        for label, zone in items:
            if zone is None:
                continue
            key = round(zone.mid, 2)
            if key in seen:
                continue
            seen.add(key)
            selected.append((label, zone))
        return selected[:3]

    bull = distinct_zones(
        [
            ("İlk önemli bölge", result.medium_term_target or result.short_term_target),
            ("Ana hedef bölgesi", result.long_term_main_target),
            ("Uzak boğa bölgesi", result.extreme_bull or result.strong_bull),
        ]
    )
    bear = distinct_zones(
        [
            ("İlk önemli destek", result.near_pullback),
            ("Ana dip bölgesi", result.medium_term_support or result.long_term_bottom),
            ("Aşırı negatif bölge", result.extreme_negative),
        ]
    )
    lines.extend(["", "BOĞA SENARYOSU"])
    lines.extend(
        f"- {label}: {price_text(zone.low)}–{price_text(zone.high)} · {score_text(zone.evidence_strength)}"
        for label, zone in bull
    )
    if bull:
        condition = next(iter(bull[0][1].activation_conditions), "Kapanış teyidi bekleniyor")
        lines.append(f"Ana koşul: {condition}")
    else:
        lines.append("- Yeterli bağımsız teknik bölge yok.")
    if result.extreme_bull is None:
        lines.append(result.extreme_bull_note)

    lines.extend(["", "AYI SENARYOSU"])
    lines.extend(
        f"- {label}: {price_text(zone.low)}–{price_text(zone.high)} · {score_text(zone.evidence_strength)}"
        for label, zone in bear
    )
    if bear:
        condition = next(iter(bear[0][1].activation_conditions), "Kapanış teyidi bekleniyor")
        lines.append(f"Ana koşul: {condition}")
    else:
        lines.append("- Yeterli bağımsız teknik bölge yok.")

    main_zone = result.long_term_main_target or result.medium_term_target or result.short_term_target
    invalidation = next(iter(main_zone.invalidation_conditions), "Ana teknik desteğin kaybı") if main_zone else "Ana teknik desteğin kaybı"
    lines.extend(
        [
            "",
            "SONUÇ",
            f"Ana görünüm: {public_label(result.long_term_trend)}; bölgeler kapanış teyidi olmadan doğrulanmış sayılmaz.",
            "Dönüşüm teyidi: Haftalık ve aylık trendin aynı yönde kapanış üretmesi.",
            f"Ana geçersizlik: {invalidation}",
            "",
            "Kanıt gücü olasılık değildir; teknik ve temel dayanakların bileşik puanıdır.",
            "Bu bölgeler koşullu senaryolardır; kesin fiyat tahminleri değildir.",
            "Bu mesaj yatırım tavsiyesi değildir.",
        ]
    )
    missing.extend(getattr(result, "missing_data", None))
    lines.extend(missing.lines())
    return enforce_message_limit("\n".join(lines))


def format_long_term_scenario_detail(symbol: str, price, result, direction: str) -> str:
    direction = direction.casefold()
    if direction in {"boğa", "boga", "bull"}:
        title = "BOĞA DETAYI"
        zones = (
            ("İlk önemli bölge", result.short_term_target),
            ("Orta bölge", result.medium_term_target),
            ("Uzun vadeli ana hedef", result.long_term_main_target),
            ("Güçlü boğa", result.strong_bull),
            ("Aşırı boğa", result.extreme_bull),
        )
    else:
        title = "AYI DETAYI"
        zones = (
            ("İlk önemli destek", result.near_pullback),
            ("Orta vadeli destek", result.medium_term_support),
            ("Uzun vadeli dip", result.long_term_bottom),
            ("Aşırı negatif", result.extreme_negative),
        )
    lines = [f"🧭 MONTANA FİNANS ROBOTU HİSSE BOT — {symbol} {title}", f"Güncel fiyat: {price_text(price.current_price)}"]
    for label, zone in zones:
        detail = detailed_scenario_lines(label, zone)
        if detail:
            lines.extend([""] + detail)
    if len(lines) == 2:
        lines.extend(["", "Bu yönde yeterli kanıt bulunamadı."])
    lines.append("\nKanıt gücü gerçekleşme olasılığı değildir.")
    return enforce_message_limit("\n".join(lines))


def format_user_target_check(evaluation, price, quality=None) -> str:
    realism = evaluation.realism
    roadmap = evaluation.roadmap
    route = " → ".join(f"{step.mid:.2f}" for step in roadmap.steps) or "Veri yetersiz"
    lines = ["🎯 MONTANA FİNANS ROBOTU HİSSE BOT — HEDEF KONTROLÜ", f"KULLANICI HEDEFİ: {evaluation.user_target:.2f} TL", ""]
    lines.extend([
        f"Güncel fiyat: {price_text(price.current_price)}",
        f"Kullanıcı hedefi: {evaluation.user_target:.2f} TL",
        f"Gereken yükseliş: {format_percent_safe(realism.required_change_percent)}",
        f"Gereken fiyat katı: {format_multiple_safe(realism.required_price_multiple)}",
        "",
        f"Teknik sınıf: {evaluation.technical_class}",
        f"Temel destek: {evaluation.fundamental_valuation_class}",
        f"Likidite riski: {realism.liquidity_risk}",
        f"Spekülasyon riski: {realism.speculation_risk}",
        f"Kanıt gücü: {score_text(evaluation.evidence_strength)}",
        "",
        "Ana yol:",
        route,
        "",
        "Kısa sonuç:",
        (
            "Bu hedef mevcut durumda aşırı agresif ve henüz doğrulanmış değildir."
            if realism.speculation_risk == "Yüksek" or evaluation.risk_class == "Çok yüksek"
            else "Bu hedef koşulludur; ara teknik bölgeler aşılmadan doğrulanmış sayılmaz."
        ),
        "",
        "Bu kullanıcı hedefi botun teknik hedefi veya AL sinyali değildir; bağımsız gerçekçilik kontrolüdür.",
    ])
    return enforce_message_limit("\n".join(lines))


def format_target_roadmap(symbol: str, roadmap, price, quality=None) -> str:
    lines = [f"🛣 MONTANA FİNANS ROBOTU HİSSE BOT — {symbol} HEDEF YOLU", ""]
    lines.extend(_stage5e_price_block(price, quality))
    if not roadmap.reliable:
        lines.extend(["", roadmap.note])
        return "\n".join(lines)
    route = [price.current_price] + [step.mid for step in roadmap.steps]
    lines.extend(["", "Ana yol: " + " → ".join(f"{value:.2f}" for value in route if value is not None)])
    for step in roadmap.steps:
        retest = f"{step.retest_zone[0]:.2f}–{step.retest_zone[1]:.2f}" if step.retest_zone else "Belirsiz"
        source = ", ".join(step.evidence[:3]) if step.evidence else "Kaynak yok"
        lines.extend([
            "", f"{step.sequence}. {step.price_low:.2f}–{step.price_high:.2f} TL — {step.level_type}",
            f"Kaynak: {source} | Kanıt gücü: {score_text(step.evidence_strength)}",
            f"Durum: {step.status} | Ufuk: {step.estimated_duration}",
            f"Kırılım: {step.breakout_condition}",
            f"Retest: {retest} TL | Sonraki bölge: {f'{step.next_target:.2f} TL' if step.next_target else 'Hedef sonu'}",
            f"Geçersizlik: {step.invalidation_level:.2f} TL" if step.invalidation_level is not None else "Geçersizlik: Belirsiz",
        ])
    lines.extend(["", "Hedef yolu koşulludur; ara seviyeler aşılmadan sonraki kademe doğrulanmış sayılmaz."])
    return enforce_message_limit("\n".join(lines))


def format_valuation(result, price, quality=None) -> str:
    lines = [f"🏢 MONTANA FİNANS ROBOTU HİSSE BOT — {result.symbol} DEĞERLEME", ""]
    lines.extend(_stage5e_price_block(price, quality))
    lines.extend(["", f"Değerleme sonucu: {result.classification}"])
    if not result.applicable:
        lines.extend(result.warnings)
        return "\n".join(lines)
    fields = [
        ("Güncel piyasa değeri", format_try_compact(result.current_market_cap)),
        ("Toplam pay sayısı", f"{result.shares_outstanding:,.0f}" if result.shares_outstanding is not None else "Veri bulunamadı"),
        ("Hisse başına defter değeri", format_price_safe(result.book_value_per_share)),
        ("Net aktif değer", format_try_compact(result.net_asset_value)),
        ("Hisse başına NAD", format_price_safe(result.nav_per_share)),
        ("Piyasa değeri / NAD", f"{result.market_cap_to_nav:.2f}x" if result.market_cap_to_nav is not None else "Veri bulunamadı"),
        ("NAD iskonto/prim", format_percent_safe(result.nav_discount_premium_percent)),
        ("Toplam varlık", format_try_compact(result.total_assets)),
        ("Toplam borç", format_try_compact(result.total_debt)),
        ("Net borç", format_try_compact(result.net_debt)),
        ("Finansman giderleri", format_try_compact(result.financing_expenses)),
        ("Kira gelirleri", format_try_compact(result.rental_income)),
        ("Gayrimenkul portföy değeri", format_try_compact(result.property_portfolio_value)),
        ("Son dönem kâr/zarar", format_try_compact(result.latest_profit_loss)),
        ("Öz kaynak değişimi", format_percent_safe(result.equity_change_percent)),
        ("Son finansal dönem", str(result.financial_period_date) if result.financial_period_date else "Veri bulunamadı"),
    ]
    missing = MissingDataCollector()
    for label, value in fields:
        if value in {"Veri bulunamadı", "Veri yetersiz"}:
            missing.add(label)
        else:
            lines.append(f"{label}: {value}")
    if result.warnings:
        lines.append("Veri bulunamadı: bazı temel alanlar sağlayıcı tarafından sunulmadı.")
    lines.extend(missing.lines())
    return enforce_message_limit("\n".join(lines))


def format_corporate_actions(symbol: str, events: list, price, quality=None) -> str:
    lines = [f"🏛 MONTANA FİNANS ROBOTU HİSSE BOT — {symbol} SERMAYE İŞLEMLERİ", ""]
    lines.extend(_stage5e_price_block(price, quality))
    lines.append("")
    if not events:
        lines.append("Veri bulunamadı; sağlayıcı bu sembol için sermaye işlemi sunmadı.")
        return "\n".join(lines)
    for event in reversed(events[-15:]):
        lines.append(f"• {event.effective_date or 'Tarih bulunamadı'} — {event.corporate_action_type}")
        lines.append(
            f"  Oran: {event.share_ratio if event.share_ratio is not None else 'Veri bulunamadı'} | "
            f"Düzeltme faktörü: {event.adjustment_factor if event.adjustment_factor is not None else 'Veri bulunamadı'}"
        )
    lines.extend(["", "Teknik geçmişte ayarlı seri; Telegram güncel fiyatında gerçek işlem fiyatı kullanılır."])
    return "\n".join(lines)


def format_target_history(symbol: str, rows: list) -> str:
    lines = [f"📚 MONTANA FİNANS ROBOTU HİSSE BOT — {symbol} HEDEF GEÇMİŞİ", ""]
    if not rows:
        return "\n".join(lines + ["Kayıtlı hedef bulunamadı."])
    for row in rows:
        lines.append(
            f"• {row.data_timestamp:%d.%m.%Y} | {row.target_type} | "
            f"{row.target_low:.2f}–{row.target_high:.2f} TL | {row.status} | Güven: {row.confidence if row.confidence is not None else '-'}"
        )
    return "\n".join(lines)


def format_target_performance_stage5e(report) -> str:
    scope = report.symbol or "Tüm semboller"
    lines = [
        f"📈 MONTANA FİNANS ROBOTU HİSSE BOT — HEDEF BAŞARISI ({scope})", "",
        f"Toplam hedef: {report.total_targets}",
        f"Hedefe ulaşıldı: {report.reached_targets}",
        f"Kısmen ulaşıldı: {report.partially_reached_targets}",
        f"Geçersiz: {report.invalidated_targets}",
        f"Süresi doldu: {report.expired_targets}",
        f"Başarı oranı: {format_percent_safe(report.success_rate, signed=False)}",
        f"Ortalama hedefe ulaşma süresi: {report.average_days_to_target if report.average_days_to_target is not None else 'Veri yetersiz'} gün",
        f"Ortalama maksimum düşüş: {format_percent_safe(report.average_max_drawdown_percent)}",
        f"Ortalama maksimum yükseliş: {format_percent_safe(report.average_max_upside_percent)}",
        f"Geçersizlik oranı: {format_percent_safe(report.invalidation_rate, signed=False)}",
        f"Aşırı boğa başarı oranı: {format_percent_safe(report.extreme_bull_success_rate, signed=False)}",
    ]
    if report.by_horizon:
        lines.extend(["", "Vade bazında:"])
        for horizon, values in report.by_horizon.items():
            lines.append(f"• {horizon}: {values['total']} hedef | {format_percent_safe(values['success_rate'], signed=False)}")
    return "\n".join(lines)
