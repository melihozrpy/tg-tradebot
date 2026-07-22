# MERGEN QUANT — Aşama 5e Teslim Raporu

## Repo inceleme sonucu

Proje sıfırdan yazılmadı. Mevcut provider, günlük/intraday OHLCV, indikatör,
destek/direnç, kısa senaryo, çoklu zaman, portföy/alarm, Telegram, grafik,
SQLAlchemy/Alembic ve test mimarileri korunarak additive geliştirme yapıldı.
Başlangıç test tabanı 381/381 başarılıydı.

## Eklenen özellikler

- Merkezi güncel fiyat / kesinleşmiş kapanış ayrımı ve fallback uyarısı
- İstanbul saatine göre tamamlanmış 1 saatlik mumlardan deterministik 4 saatlik resample
- Config ağırlıklı 6-zaman dilimi analizi ve çelişki/tepki/dönüşüm ayrımı
- Koşullu uzun vadeli boğa/ayı senaryo motoru
- Kullanıcı hedef kontrolü, hedef gerçekçilik ve spekülasyon risk filtresi
- Kademeli hedef yol haritası
- GYO NAD/iskonto-prim değerleme motoru; eksik veride fail-closed davranış
- Sermaye işlemleri, raw/adjusted seri ayrımı ve split/temettü gap filtresi
- Hedef geçmişi, duplicate koruması, durum ve performans takibi
- Haftalık ve aylık logaritmik uzun vadeli candlestick grafik
- Yeni Telegram komutları ve analiz alt menü butonları
- Merkezi güvenli finansal formatter

## Eklenen kaynak dosyaları

- `app/analysis/corporate_actions_engine.py`
- `app/analysis/gyo_valuation_engine.py`
- `app/analysis/long_term_scenario_engine.py`
- `app/analysis/target_realism_engine.py`
- `app/analysis/target_roadmap_engine.py`
- `app/analysis/user_target_engine.py`
- `app/services/current_price_service.py`
- `app/services/stage5e_analysis_service.py`
- `app/services/target_tracking_service.py`
- `app/utils/financial_formatter.py`
- `migrations/versions/0006_stage5e_long_term_targets_valuation.py`

## Değiştirilen ana dosyalar

- `.env.example`, `README.md`
- `app/config/settings.py`, `app/data/provider_factory.py`
- `app/models/database.py`
- `app/analysis/anomaly_engine.py`, `app/analysis/multi_timeframe_engine.py`
- `app/services/analysis_service_v3.py`, `app/services/anomaly_service.py`
- `app/services/chart_service.py`, `app/services/enhanced_alert_service.py`
- `app/services/intraday_service.py`, `app/services/scan_service.py`
- `app/telegram/bot.py`, `app/telegram/handlers.py`, `app/telegram/handlers_v3.py`
- `app/telegram/message_templates.py`, `app/telegram/message_templates_v3.py`
- `mergen_quant.db` (yalnızca additive migration)

## Migration

`0006_stage5e_long_term_targets_valuation`

Eklenen tablolar: `long_term_scenarios`, `user_price_targets`,
`target_roadmap_steps`, `valuation_snapshots`, `corporate_action_events`,
`target_realism_snapshots`, `target_tracking_records`,
`target_performance_summaries`.

Fresh DB, mevcut 0005 DB ve tekrar upgrade senaryoları geçti. Gerçek DB
migration öncesinde `data/backups/mergen_quant_pre_stage5e.db` olarak yerel
güvenlik kopyası alındı. Kullanıcı/sinyal/seviye/kısa senaryo sayıları migration
öncesi ve sonrası aynı kaldı: users=1, signals=3, timeframe_levels=46,
price_scenarios=5.

## Test sonucu

- Önceki test: 381
- Yeni test: 60
- Toplam: 441
- Geçen: 441
- Başarısız: 0
- Uyarı: 3 (FastAPI/Starlette deprecation; işlevsel hata değil)

Doğrulanan smoke sonuçları:

- Güncel fiyat: `completed_5m`; kapanış ayrı ve mevcut
- 4 saat: 33 tamamlanmış örnek mum üretildi
- Hedef kontrol: 12.72 → 70.00 için +%450.31 ve 5.50x
- Değerleme: veri yokken “Veri yetersiz”, değer uydurulmadı
- Sermaye işlemi: 2:1 split, düzeltme faktörü 0.5
- FastAPI root: HTTP 200
- FastAPI health: HTTP 200
- Telegram: tüm Aşama 5e komutları kayıtlı (toplam 69 komut)
- Scheduler: kuruldu, 3 iş kayıtlı, event loop öncesinde başlatılmadı
- Günlük grafik: üretildi
- Haftalık uzun log grafik: üretildi
- Python compileall: başarılı

## Canlı veriyle doğrulanmayan noktalar ve bilinen sınırlamalar

- Testler gerçek ağa bağlanmadı; yfinance/Yahoo yanıtları canlı olarak doğrulanmadı.
- Telegram Bot API'ye gerçek mesaj/fotoğraf gönderilmedi; handler/registration smoke yapıldı.
- Varsayılan fundamental provider devre dışıdır. Gerçek temel veri yoksa GYO
  değerlemesi açıkça “Veri yetersiz” döner.
- Ücretsiz kurumsal işlem kaynağı ağırlıkla split ve temettü sunar. Bedelli,
  bedelsiz, birleşme, sermaye tavanı ve pay değişimi ancak provider gerçekten
  sağladığında gösterilir.
- Kurum yoğunlaşması, ayrıntılı fiilî dolaşım ve lisanslı order-flow verileri
  ücretsiz provider'da yoksa gerçekçilik motoru bu alanları “Veri yetersiz” sayar.
- “Anormal fiyat/hacim davranışı” bir manipülasyon suçlaması değildir.
- Güncel fiyat provider gecikmesine tabidir; alınamazsa son kesinleşmiş kapanış
  açık uyarıyla kullanılır.

## Kurulum

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
python -m alembic upgrade head
python -m pytest -q
python run_bot.py
```

FastAPI:

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
