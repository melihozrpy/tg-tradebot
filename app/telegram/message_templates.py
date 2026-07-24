from __future__ import annotations

from datetime import datetime, timezone

from app.analysis.signal_engine import SignalResult

ISTANBUL_OFFSET_HOURS = 3

SIGNAL_TYPE_LABELS_TR = {
    "STRONG_BUY_CANDIDATE": "Guclu alim adayi",
    "BUY_CANDIDATE": "Alim adayi",
    "WATCH": "Tetik bekleniyor",
    "NEUTRAL": "Notr / bekle",
    "WEAK_RISK": "Risk artti",
    "REDUCE_POSITION": "Pozisyon azaltma adayi",
    "STRONG_RISK": "Satis riski",
}

TREND_LABELS_TR = {
    "up": "Yukselis",
    "down": "Dusus",
    "sideways": "Yatay",
}


def format_price(value: float | None) -> str:
    """Fiyatlari her zaman en fazla 2 ondalik basamakla, TL formatinda gosterir.

    Uzun ondalikli degerler (orn. 140.2118261812036) asla kullaniciya
    gosterilmez; her zaman round(value, 2) uygulanir.
    """
    if value is None:
        return "-"
    return f"{round(float(value), 2):.2f}"


def _to_istanbul(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    from datetime import timedelta

    ist = dt.astimezone(timezone.utc) + timedelta(hours=ISTANBUL_OFFSET_HOURS)
    return ist.strftime("%d.%m.%Y %H:%M") + " (TR)"


def _to_istanbul_date(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    from datetime import timedelta

    ist = dt.astimezone(timezone.utc) + timedelta(hours=ISTANBUL_OFFSET_HOURS)
    return ist.strftime("%d.%m.%Y")


def format_full_analysis_message(signal: SignalResult, display_symbol: str) -> str:
    """Bolum 5 spesifikasyonundaki Telegram analiz mesaj formatini uretir.

    /analiz komutunun ana ciktisidir. Destek/direnc, giris/stop/hedef ve
    baglamsal notlar (kirilim bekleniyor / destekten tepki / destek kirildi)
    dahil edilir. Fiyatlar her zaman 2 ondalik basamakla gosterilir.
    """
    close = signal.extras.get("close")
    trend_direction = signal.extras.get("trend_direction", "sideways")
    trend_label = TREND_LABELS_TR.get(trend_direction, "Belirsiz")
    signal_label = SIGNAL_TYPE_LABELS_TR.get(signal.signal_type, signal.signal_type)

    sr = signal.support_resistance
    support_1 = format_price(sr.support_1) if sr else "-"
    support_2 = format_price(sr.support_2) if sr else "-"
    main_support = format_price(sr.main_support) if sr else "-"
    resistance_1 = format_price(sr.resistance_1) if sr else "-"
    resistance_2 = format_price(sr.resistance_2) if sr else "-"
    main_resistance = format_price(sr.main_resistance) if sr else "-"

    support_line_note = ""
    resistance_line_note = ""
    if sr and not sr.support_reliable:
        support_line_note = f"\n({sr.support_note})"
    if sr and not sr.resistance_reliable:
        resistance_line_note = f"\n({sr.resistance_note})"

    daily_change = signal.daily_change_percent
    daily_change_text = f"%{daily_change:+.2f}" if daily_change is not None else "-"

    positive_reasons = [r for r in signal.reasons if not r.is_risk][:4]
    risk_reasons = [r for r in signal.reasons if r.is_risk][:4]
    reasons_text = "\n".join(f"• {r.description}" for r in positive_reasons) or "• (belirgin ek neden yok)"
    risks_text = "\n".join(f"• {r.description}" for r in risk_reasons) or "• Belirgin ek risk isaretlenmedi"

    contextual_text = ""
    if signal.contextual_notes:
        contextual_text = "\n".join(signal.contextual_notes) + "\n\n"

    entry_low, entry_high = signal.entry_zone
    entry_zone_text = f"{format_price(entry_low)} - {format_price(entry_high)}" if entry_low else "-"

    return f"""📊 {display_symbol} ANALIZI

Son kapanis: {format_price(close)} TL
Gunluk degisim: {daily_change_text}
Veri tarihi: {_to_istanbul(signal.data_timestamp)}
Trend: {trend_label}
Analiz skoru: {signal.score}/100
Sinyal: {signal_label}
Piyasa yapisi: {signal.market_regime}

{contextual_text}Destekler:
• Destek 1: {support_1}
• Destek 2: {support_2}
• Ana destek: {main_support}{support_line_note}

Direncler:
• Direnc 1: {resistance_1}
• Direnc 2: {resistance_2}
• Ana direnc: {main_resistance}{resistance_line_note}

Olasi giris bolgesi: {entry_zone_text}
Tetik seviyesi: {format_price(signal.entry_trigger)}
Stop: {format_price(signal.stop_price)}
Hedef 1: {format_price(signal.target_1)}
Hedef 2: {format_price(signal.target_2)}
Hedef 3: {format_price(signal.target_3)}
Risk/Getiri: {signal.risk_reward if signal.risk_reward is not None else "-"}

Ana nedenler:
{reasons_text}

Riskler:
{risks_text}

Senaryo su fiyat altinda gecersiz: {format_price(signal.stop_price)}
Veri saglayicisi: {signal.provider}
Son islem gunu: {_to_istanbul_date(signal.data_timestamp)}

Bu mesaj yatirim tavsiyesi degildir, kurallı analiz ciktisidir.
"""


def format_buy_candidate_message(signal: SignalResult) -> str:
    reasons = [r for r in signal.reasons if not r.is_risk][:4]
    risks = [r for r in signal.reasons if r.is_risk][:2]

    reasons_text = "\n".join(f"• {r.description}" for r in reasons) or "• (belirgin ek neden yok)"
    risks_text = "\n".join(f"• {r.description}" for r in risks) or "• Belirgin ek risk isaretlenmedi"

    entry_low, entry_high = signal.entry_zone
    emoji = "🟢" if signal.signal_type in ("STRONG_BUY_CANDIDATE", "BUY_CANDIDATE") else "🟡"

    return f"""{emoji} {"GUCLU ALIM ADAYI" if signal.signal_type == "STRONG_BUY_CANDIDATE" else "ALIM ADAYI" if signal.signal_type == "BUY_CANDIDATE" else "IZLEME LISTESI"} — {signal.symbol}

Fiyat: {signal.extras.get("close", "-")}
Sinyal skoru: {signal.score}/100
Guven: {signal.confidence}
Piyasa rejimi: {signal.market_regime}

Olasi giris bolgesi:
{entry_low} - {entry_high}

Stop:
{signal.stop_price if signal.stop_price else "hesaplanamadi"}

Hedef 1:
{signal.target_1 if signal.target_1 else "-"}

Hedef 2:
{signal.target_2 if signal.target_2 else "-"}

Risk/Getiri:
{signal.risk_reward if signal.risk_reward else "-"}

Ana nedenler:
{reasons_text}

Riskler:
{risks_text}

Senaryo su durumda gecersiz:
{signal.invalidation_note}

Veri zamani:
{_to_istanbul(signal.data_timestamp)}

Veri saglayicisi:
{signal.provider}

Bu mesaj yatirim tavsiyesi degil, kurallı analiz ciktisidir.
"""


def format_risk_warning_message(signal: SignalResult, position_cost: float | None = None, position_pnl: float | None = None) -> str:
    risks = [r for r in signal.reasons if r.is_risk]
    main_reason = risks[0].description if risks else "Skor risk esiginin altina dustu"

    action_map = {
        "STRONG_RISK": "Tam cikis adayi",
        "REDUCE_POSITION": "Kismi azaltma",
        "WEAK_RISK": "Yalnizca dikkat",
    }
    action = action_map.get(signal.signal_type, "Yalnizca dikkat")

    cost_line = f"Maliyet: {position_cost}\n" if position_cost is not None else ""
    pnl_line = f"Kar/Zarar: {position_pnl}\n" if position_pnl is not None else ""

    return f"""🔴 RISK UYARISI — {signal.symbol}

Fiyat: {signal.extras.get("close", "-")}
{cost_line}{pnl_line}Risk skoru: {signal.score}/100
Ana neden: {main_reason}
Onerilen aksiyon turu: {action}

Senaryo tekrar su seviyede guclenebilir:
{signal.stop_price if signal.stop_price else "-"}

Veri zamani: {_to_istanbul(signal.data_timestamp)}
Veri saglayicisi: {signal.provider}

Bu mesaj yatirim tavsiyesi degil, kurallı analiz ciktisidir.
"""


def format_kap_placeholder_message(symbol: str) -> str:
    return f"""📰 KAP GELISMESI — {symbol}

KAP saglayicisi bu surumde devre disidir (FAZ 1).
Gercek zamanli/lisansli KAP entegrasyonu FAZ 3'te eklenecektir.

Bu bildirim tek basina islem sinyali degildir.
"""


def format_daily_report(
    market_regime: str,
    xu100_change_percent: float | None,
    top_symbols: list[str],
    rising_risk_symbols: list[str],
    new_signals_count: int,
    cancelled_signals_count: int,
    paper_summary: dict,
    portfolio_daily_change: float | None,
    system_health_ok: bool,
) -> str:
    xu100_line = f"{xu100_change_percent:+.2f}%" if xu100_change_percent is not None else "veri yok"
    top_text = ", ".join(top_symbols) if top_symbols else "-"
    risk_text = ", ".join(rising_risk_symbols) if rising_risk_symbols else "-"
    portfolio_line = f"{portfolio_daily_change:+.2f}%" if portfolio_daily_change is not None else "pozisyon yok"

    return f"""🏹 MERGEN QUANT — GÜNLÜK RAPOR

Piyasa rejimi: {market_regime}
XU100 performansi: {xu100_line}
Izleme listesindeki en guclu hisseler: {top_text}
Risk skoru yukselen hisseler: {risk_text}
Yeni sinyaller: {new_signals_count}
Iptal edilen sinyaller: {cancelled_signals_count}
Paper trading sonucu: getiri %{paper_summary.get("return_percent", 0)} (equity: {paper_summary.get("equity", 0)})
Portfoy gunluk degisimi: {portfolio_line}
Sistem/veri sagligi: {"OK" if system_health_ok else "SORUNLU - loglara bakin"}
"""


def format_help_message() -> str:
    return """MERGEN QUANT — Komutlar

/ekle SEMBOL - Izleme listesine hisse ekle (orn: /ekle THYAO)
/sil SEMBOL - Izleme listesinden hisse cikar
/liste - Izleme listeni goster
/analiz SEMBOL - Anlik teknik analiz ve sinyal (kisa ozet)
/analiz_detay SEMBOL - Detayli analiz (karar, likidite, zaman dilimi, S/D)
/islemplani SEMBOL - Long/short bolgeleri, TP1-TP5 ve cok katmanli SL haritasi
/gunici SEMBOL - Gun ici on analiz (kesin sinyal degildir)
/zaman_dilimleri SEMBOL - Coklu zaman dilimi uyum analizi
/cokluzaman SEMBOL - Haftalik/gunluk/4s/1s/15dk/5dk agirlikli analiz
/likidite SEMBOL - Likidite / islem hacmi analizi
/seviyeler SEMBOL - Gunluk/haftalik/aylik destek-direnc ve cakisan guclu bolgeler
/senaryo SEMBOL - Dusus/yukselis senaryo bolgeleri
/kirilsanaryo SEMBOL - "Bu seviye kirilirsa ne olur?" analizi
/uzunsenaryo SEMBOL - Kosullu uzun vadeli boga/ayi senaryolari
/hedefkontrol SEMBOL FIYAT - Kullanici hedefi gercekcilik kontrolu
/hedefyolu SEMBOL [FIYAT] - Kademeli hedef yol haritasi
/degerleme SEMBOL - GYO ise NAD/iskonto temel degerlemesi
/uzungrafik SEMBOL [FIYAT] - Haftalik ve aylik logaritmik grafik
/sermaye_islemleri SEMBOL - Split/bedelsiz/temettu ve diger islemler
/hedefgecmisi SEMBOL - Kayitli bot hedeflerinin durumu
/hedefbasari [SEMBOL] - Hedef performans ozeti
/anomali SEMBOL - Sembol icin anlik anomali taramasi
/anomaliler [saat] - Izleme listende son N saatteki anomaliler (varsayilan 48)
/haberler SEMBOL - Sembol icin son GDELT haberleri
/haber_detay SEMBOL - Kural tabanli haber etkisi detayi
/haber_radari - Izleme listende onemli haberi olan semboller
/ai_aciklama SEMBOL - Groq (opsiyonel) ile sade Turkce analiz aciklamasi
/sinyaller - Son sinyal gecmisi
/aktif_sinyaller - Acik/takip edilen sinyaller
/performans - Sinyal performans ozeti
/alarm_kur SEMBOL TUR DEGER - Alarm kur (ust/alt/hacim/skor/sinyal/rejim/anomali)
/alarmlar - Kurulu alarmlarini listele
/alarm_sil ID - Alarm sil
/portfoy - Portfoy ozeti
/risk - Risk ayarlarin
/backtest SEMBOL [TARIH TARIH] - Masrafli, nokta-zaman backtest baslat
/backtest_ozet - Backtest durumlarini goster
/sanal_portfoy - Acik sanal islemleri goster
/sanal_performans [SEMBOL] - Sanal performansi goster
/sinyalbasari [SEMBOL] - Tamamlanmis sinyal sonuclarini goster
/kalibrasyon [SEMBOL] - Tarihsel puan kalibrasyonunu goster
/neden SEMBOL - Kararin gercek puan katkilarini goster
/durum - Sistem durumu
/acil_durdur - Kill switch (tum analiz/islemleri durdurur)
/devam_et - Kill switch'i kapat
/ayarlar - Kisisel ayarlarin
/yardim - Bu mesaj

Not: Bu bot yatirim tavsiyesi vermez, kural tabanli analiz ciktisi sunar.
"""
