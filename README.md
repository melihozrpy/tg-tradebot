# MERGEN QUANT — Akıllı BIST Analiz ve Risk Sistemi

Ücretsiz veri kaynaklarıyla çalışan, BIST hisselerini izleyen, tarayan,
sinyal geçmişini takip eden, portföy riski hesaplayan ve backtest yapan
Telegram botu ve API. Uygulama adı, paket adı (`mergen-quant`), veritabanı
dosya adı (`mergen_quant.db`), Docker kullanıcı/servis adları ve logger
isimleri dahil proje genelinde MERGEN QUANT markasına tam olarak geçirilmiştir.

> ⚠️ **Yasal uyarı**: Bu sistem yatırım tavsiyesi vermez. Çıktılar kural
> tabanlı, açıklanabilir analiz sonuçlarıdır. Tüm yatırım kararlarının
> sorumluluğu kullanıcıya aittir. **Canlı/otomatik emir gönderimi hiçbir
> zaman eklenmemiştir ve eklenmeyecektir** — yalnızca analiz, uyarı,
> tarama, alarm, backtest ve paper trading vardır.
>
> Bu proje yalnızca **ücretsiz** araçlarla çalışır: yfinance, Telegram Bot
> API, yerel CSV/JSON dosyaları ve kamuya açık veriler. Ücretli API,
> ücretli veri sağlayıcı veya broker entegrasyonu **yoktur**.

---

## MERGEN QUANT — Aşama 5d + Veri Güvenilirliği (bu teslimat)

**Aşama 5d (tamamlandı):**

- Kalıcı kurallar, tamamlanmış mum doğrulaması, kapanış/hacim/ATR/likidite
  teyidi, aynı mum dedup ve cooldown içeren gelişmiş alarm sistemi.
- yfinance ana kaynak → ayrı Yahoo Chart adapter → izin verilen yaştaki
  yerel disk cache sıralaması; retry/backoff ve circuit breaker.
- `HEALTHY`, `DEGRADED`, `STALE`, `INCOMPLETE`, `INVALID`, `PROVIDER_DOWN`
  durumlarını ve 0–100 puanı üreten genişletilmiş veri kalite motoru.
- Swing/pivot/EMA/VWAP/volume-profile/gap/Fibonacci/Bollinger gibi doğrulanmış
  adayları ATR tabanlı stabil fiyat bölgelerinde birleştiren seviye motoru.
- Gerçek Open–Close mum gövdeleri ve High–Low fitilleri, dört panelli günlük
  grafik, üç panelli 15 dakikalık grafik, tema ve dosya cache'i.
- `/veri_durumu [SEMBOL]`, `/alarm_durdur`, `/alarm_ac`, `/alarm_detay` ve
  `/health/data`, `/health/providers`, `/health/scheduler` uçları.
- Additive Alembic migration: `0005_stage5d_reliability_alerts_charts`.

**Aşama 5a (tamamlandı):** marka MERGEN QUANT, çok-zamanlı (günlük/haftalık/
aylık) destek-direnç ve çakışan güçlü bölge motorları (`/seviyeler SEMBOL`).

**Aşama 5b (tamamlandı):** düşüş/yükseliş senaryo bölgeleri
(`/senaryo SEMBOL`) ve "bu seviye kırılırsa ne olur?" kırılım senaryosu
motoru (`/kirilsanaryo SEMBOL`).

**Aşama 5c (tamamlandı):**
- Pozisyonu olan/olmayan kullanıcı için ayrı analiz ve yorum sistemi
  (`/analiz`, `/analiz_detay` artık pozisyona göre farklı gövde üretir).
- Gelişmiş, dönemsel (1 hafta/1 ay/3 ay/6 ay) XU100 ve sektör göreceli güç
  sistemi, `relative_strength_periods` tablosuna kayıt ile birlikte.
- `/guc SEMBOL` Telegram komutu.
- Profesyonel günlük grafik yenilemesi (EMA20/50/100/200, Bollinger,
  çok-zamanlı destek/direnç, çakışan bölgeler, işlem planı, hacim, RSI,
  MACD, bilgi kutusu) ve gün içi grafik yenilemesi (VWAP, EMA20/50,
  günlük destek/direnç, hacim, RSI).
- Ayrıca: `_holds_position` fonksiyonundaki var olan bir hata (`p.quantity`
  yerine olması gereken `p.lot`) düzeltildi — bu hata, pozisyonu olan bir
  kullanıcı `/analiz` çalıştırdığında çökmeye yol açıyordu.

Her ikisi de (ve tüm önceki aşamalar) deterministik Python koduyla
hesaplanır; Groq/LLM fiyat, stop, hedef veya AL/SAT/TUT kararı üretmez.

---

## V3'te Yeni Olanlar

1. **Gün içi / kesinleşmiş kapanış ayrımı** — Piyasa açıkken tamamlanmamış
   günlük mumdan kesin sinyal üretilmez; "GÜN İÇİ ÖN ANALİZ" başlığıyla
   yalnızca bilgi amaçlı gösterilir. Piyasa açık/kapalı tespiti sabit saate
   değil; Europe/Istanbul yerel saati + hafta sonu + son mumun tarihi +
   kapanış tarama saati birlikte değerlendirilerek yapılır.
2. **XU100 piyasa rejimi** — Yapılandırılabilir sembolle (`XU100_SYMBOL`)
   EMA/RSI/MACD/ADX/ATR ve 20-60 günlük getiriye dayalı rejim sınıflandırması.
   XU100 verisi alınamazsa analiz çökmez, "veri alınamadı" olarak işaretlenir.
3. **XU100'e göre göreceli güç** — 5/20/60 günlük getiri farkı, 0-100 skor,
   "çok güçlü/güçlü/nötr/zayıf/çok zayıf" sınıflandırması. Hisse ve endeks
   verisi **ortak işlem günlerinde hizalanır**; tarihler uyumsuzsa hesaplama
   yapılmaz.
4. **Sektör karşılaştırması** — `app/config/sector_map.yaml` üzerinden
   yapılandırılabilir eşleştirme + Telegram'dan `/sektor_ayarla` ile ekleme.
   Eşleştirme yoksa **asla tahmin üretilmez**, açıkça "bulunamadı" denir.
5. **Otomatik akşam taraması** — APScheduler ile kapanış sonrası otomatik
   tarama (yalnızca izleme listesindeki hisseler). Tek sembol hatası tüm
   taramayı durdurmaz. `/tara`, `/tarama_durumu`, `/aksam_raporu`.
6. **Gelişmiş sinyal geçmişi ve yaşam döngüsü** — PREVIEW → WAITING_TRIGGER →
   ACTIVE → TARGET_1/2/3_HIT / STOP_HIT → EXPIRED. Aynı gün mumda hem stop
   hem hedef görülürse **muhafazakâr** (stop öncelikli) yöntem kullanılır
   (config ile değiştirilebilir).
7. **Sinyal başarı raporu** — `/performans [gün]`. Yeterli örnek yoksa
   ("minimum_sample_size", varsayılan 20) yanıltıcı yüzde **üretilmez**.
8. **Genişletilmiş portföy yönetimi** — pozisyon güncelleme, portföy riski,
   sektör yoğunlaşması, maksimum zarar senaryosu, nakit/sermaye ayarları.
9. **Gelişmiş alarm sistemi** — fiyat/hacim/skor/sinyal/rejim alarmları,
   cooldown + idempotency ile tekrar önleme.
10. **Telegram grafikleri** — matplotlib ile fiyat ve göreceli güç grafikleri;
    gönderim sonrası geçici dosyalar **güvenli şekilde silinir**.
11. **Gelişmiş backtest** — XU100 ve buy&hold karşılaştırması, 2y/5y periyot.
12. **Piyasa genişliği** — yerel sembol evreni (`data/symbols/bist_symbols.csv`)
    üzerinden ücretsiz hesaplama, `/piyasa`.
13. **Kamuya açık bildirim arayüzü** — KAP'ın ücretli API'si kullanılmaz;
    yalnızca arayüz + varsayılan `disabled` sağlayıcı mevcuttur (sahte
    bildirim üretilmez).
14. **8 kategorili gelişmiş skor sistemi** — Trend(20) + Momentum(10) +
    Hacim(15) + Destek/Direnç(15) + XU100 gücü(15) + Sektör gücü(10) +
    Piyasa rejimi(10) + Risk/Getiri(5). `/skor_detay SEMBOL`.
15. **Tutarlılık denetleyici (ConsistencyValidator)** — ADX'in yalnızca
    trend GÜCÜNÜ gösterdiği, YÖNÜ göstermediği artık doğru ifade edilir
    ("ADX 39.3: Piyasada güçlü hareket var; yön diğer göstergelerle
    belirlenmiştir."). Destek/direnç/stop/hedef sıralaması, skor-sinyal
    uyumu gibi iç çelişkiler tespit edilip güven seviyesi düşürülür.
16. **Token/secret maskeleme** — Tüm loglarda bot token, API anahtarı,
    webhook secret ve Authorization header'ları otomatik maskelenir.
17. **Alembic migration** — Mevcut V2 veritabanı **kaybolmadan** yeni
    tablolar eklenir.
18. **Veri önbelleği** — yfinance'e kısa sürede tekrar tekrar istek
    atılmaz (TTL'li bellek-içi cache).
19. **Veri kalitesi kontrolü** — NaN, negatif fiyat, High<Low, sıralama
    hataları vb. tespit edilirse analiz **oluşturulmaz**.

---

## V2 → V3 Geçiş Adımları

1. Depoyu güncelleyin / yeni ZIP'i açın (mevcut `.env` ve veritabanı
   dosyanızı silmeyin).
2. Yeni bağımlılıkları kurun: `pip install -e ".[dev]"` (yfinance, alembic,
   matplotlib, apscheduler eklendi).
3. `.env.example`'daki yeni V3 değişkenlerini mevcut `.env` dosyanıza
   ekleyin (özellikle `XU100_SYMBOL`, `CLOSE_SCAN_TIME`, `CACHE_ENABLED`).
4. Migration'ı çalıştırın: `alembic upgrade head` — mevcut verileriniz
   **silinmez**, yalnızca V3 için gereken yeni tablolar eklenir.
5. `MARKET_DATA_PROVIDER=yfinance` kullanıyorsanız hiçbir ek işlem gerekmez;
   V2'deki analiz mantığı V3'te de korunur, üzerine gün içi/kapanış ayrımı,
   XU100 gücü, sektör gücü ve gelişmiş skor eklenmiştir.
6. Mevcut Telegram komutlarınız (`/analiz`, `/portfoy`, `/backtest`, vb.)
   **aynı isimle** çalışmaya devam eder; V3 onları geliştirilmiş biçimde
   çalıştırır. Yeni komutlar (bkz. aşağıdaki tam liste) otomatik eklenmiştir.
7. Docker kullanıyorsanız `docker compose up --build` yeterlidir — container
   başlangıcında migration otomatik uygulanır (`docker-entrypoint.sh`).

---

## Kurulum

### Docker (önerilen)

```bash
cp .env.example .env
# .env dosyasını düzenleyin: TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_USER_IDS

docker compose up --build
```

Container başlarken otomatik olarak `alembic upgrade head` çalıştırılır
(bkz. `docker-entrypoint.sh`), böylece hem yeni hem mevcut veritabanları
güvenle ayağa kalkar.

### Docker olmadan

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -e ".[dev]"

cp .env.example .env
# .env dosyasını düzenleyin

# Migration'ı calistirin (ilk kurulumda veya guncellemede)
alembic upgrade head

# API servisini başlatın
uvicorn app.main:app --reload --port 8000

# Ayrı bir terminalde Telegram botunu başlatın (otomatik akşam taraması dahil)
python run_bot.py
```

### BotFather üzerinden bot oluşturma

1. Telegram'da `@BotFather` ile konuşma başlatın.
2. `/newbot` komutunu gönderin, bot adı/kullanıcı adı belirleyin.
3. Verilen tokeni `.env` dosyasındaki `TELEGRAM_BOT_TOKEN` alanına yapıştırın.
4. Kendi Telegram kullanıcı ID'nizi `@userinfobot` ile öğrenip
   `ADMIN_TELEGRAM_USER_IDS` alanına ekleyin (boş bırakılırsa whitelist
   devre dışı kalır — geliştirme için uygun, **production'da doldurun**).

---

## .env Ayarları (V3)

| Değişken | Açıklama | Varsayılan |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather'dan alınan token | (zorunlu) |
| `ADMIN_TELEGRAM_USER_IDS` | Virgülle ayrılmış whitelist ID'leri | boş = herkese açık |
| `MARKET_DATA_PROVIDER` | `yfinance` (önerilen), `csv`, `mock` (yalnızca test) | `yfinance` |
| `XU100_SYMBOL` | XU100 endeksi için yfinance sembolü | `XU100.IS` |
| `INTRADAY_PREVIEW_ENABLED` | Gün içi ön analiz açık/kapalı | `true` |
| `CONFIRMED_CLOSE_REQUIRED` | Kesin sinyal için kapanış onayı şart mı | `true` |
| `CLOSE_SCAN_TIME` | Kapanış sonrası tarama/kesinleşme saati (TR) | `18:20` |
| `CLOSE_SCAN_ENABLED` | Otomatik akşam taraması açık/kapalı | `true` |
| `TIMEZONE_NAME` | Piyasa saat dilimi | `Europe/Istanbul` |
| `MINIMUM_SIGNAL_SCORE` | Varsayılan minimum sinyal skoru | `65` |
| `MINIMUM_RISK_REWARD` | Varsayılan minimum risk/getiri | `2.0` |
| `RISK_PER_TRADE_PERCENT` | İşlem başına risk yüzdesi | `0.75` |
| `MAXIMUM_POSITION_PERCENT` | Tek pozisyon maksimum ağırlığı | `20` |
| `MAXIMUM_SECTOR_EXPOSURE_PERCENT` | Maksimum sektör ağırlığı | `30` |
| `CACHE_ENABLED` | yfinance TTL cache açık/kapalı | `true` |
| `DAILY_CACHE_TTL_MINUTES` | Günlük veri cache süresi | `30` |
| `YFINANCE_MAX_RETRIES` | yfinance tekrar deneme sayısı | `3` |
| `PROVIDER_RETRY_MAX_ATTEMPTS` | Ana/fallback provider deneme sayısı | `2` |
| `PROVIDER_RETRY_BASE_SECONDS` | Exponential backoff başlangıç süresi | `1.0` |
| `PROVIDER_CIRCUIT_FAILURE_THRESHOLD` | Circuit breaker açılma hata sayısı | `3` |
| `PROVIDER_CIRCUIT_RECOVERY_SECONDS` | HALF_OPEN testine kadar bekleme | `120` |
| `YAHOO_CHART_FALLBACK_ENABLED` | Ayrı Yahoo Chart adapter fallback'i | `true` |
| `DATA_CACHE_DIR` | Son başarılı OHLCV disk cache dizini | `./data/cache/ohlcv` |
| `DATA_CACHE_MAX_AGE_DAILY_MINUTES` | Günlük cache azami yaşı | `720` |
| `DATA_CACHE_MAX_AGE_INTRADAY_MINUTES` | Gün içi cache azami yaşı | `30` |
| `TECHNICAL_PRICE_MODE` | `unadjusted` veya `adjusted` OHLC modu | `unadjusted` |
| `ENHANCED_ALARM_SCAN_ENABLED` | Gelişmiş alarm scheduler işi | `true` |
| `ENHANCED_ALARM_SCAN_MINUTES` | Gelişmiş alarm tarama aralığı | `15` |
| `ENHANCED_ALARM_DEFAULT_COOLDOWN_MINUTES` | Yeni alarm varsayılan cooldown | `120` |
| `DAILY_BRIEF_ENABLED` | Hafta içi otomatik dünkü piyasa özeti | `true` |
| `DAILY_BRIEF_TIME` | Günlük brifing gönderim saati (TR) | `09:10` |
| `TCMB_POLICY_RATE_PERCENT` | Brifingde gösterilecek doğrulanmış politika faizi; bağlı değilse boş | boş |
| `CHART_DPI` | Grafik çıktı çözünürlüğü | `120` |
| `CHART_WIDTH` / `CHART_HEIGHT` | Grafik boyutu (inç) | `13` / `11` |
| `CHART_THEME` | `light` veya `dark` | `light` |
| `CHART_CACHE_TTL_MINUTES` | Render edilmiş grafik cache süresi | `30` |
| `CHART_CACHE_DIR` | Grafik cache dizini | `./data/cache/charts` |
| `PUBLIC_DISCLOSURE_PROVIDER` | `disabled` (varsayılan) veya `rss` | `disabled` |
| `SECTOR_MAP_PATH` | Sektör eşleştirme dosyası | `app/config/sector_map.yaml` |
| `BIST_SYMBOLS_CSV_PATH` | Yerel sembol evreni CSV'si | `data/symbols/bist_symbols.csv` |
| `PERFORMANCE_MINIMUM_SAMPLE_SIZE` | Performans raporu için min. örnek | `20` |
| `SIGNAL_EXPIRY_TRADING_DAYS` | Kaç işlem gününde sinyal EXPIRED olur | `10` |
| `CONSERVATIVE_EXECUTION` | Aynı mumda stop+hedef çakışmasında muhafazakâr seçim | `true` |

Tüm V3 özellikleri bu değişkenler üzerinden **açılıp kapatılabilir**.

---

## yfinance ve fallback sınırlamaları

- Ücretsiz, gecikmeli/gecikmesiz garanti verilmeyen bir kaynaktır; resmi
  BIST/Takasbank verisi yerine geçmez.
- Sağlayıcının izin verdiği pencere içinde günlük, haftalık ve 5m/15m/1h
  gün içi zaman dilimleri kullanılabilir; ücretsiz kaynaklarda gecikme veya
  dönemsel kısıt olabilir.
- Semboller otomatik `.IS` sonekiyle sorgulanır (`SVGYO` → `SVGYO.IS`);
  zaten `.IS` ile bitiyorsa tekrar eklenmez.
- Gerçek veri alınamazsa (sembol yok, ağ hatası, boş sonuç) sistem
  **kesinlikle mock veriye geçmez**; kullanıcıya "Bu sembol için güncel
  veri alınamadı." mesajı gösterilir.
- Geçici hata/rate limit için exponential backoff, arka arkaya hatalar için
  circuit breaker vardır. Ana kaynak çalışmazsa ayrı Yahoo Chart adapter
  denenir; o da çalışmazsa yalnızca yaş sınırını aşmamış disk cache kullanılır.
- Fallback/cache kaynağı veri kalite çıktısı ve Telegram mesajında açıkça yazılır.

---

## Gün İçi vs Kesinleşmiş Kapanış Analizi

`app/analysis/market_state.py` şunları BİRLİKTE değerlendirir (sabit bir
saate kör kör bağlı değildir):

- Europe/Istanbul yerel zamanı
- Hafta sonu
- Son mumun tarihi (bugüne mi, geçmişe mi ait)
- `CLOSE_SCAN_TIME` kapanış tarama saatinin geçilip geçilmediği

**Sonuç:**
- Son mum geçmişe aitse (dünkü/önceki iş günü) → **KESİNLEŞMİŞ KAPANIŞ**.
- Son mum bugüne aitse ve kapanış tarama saati henüz geçmediyse →
  **GÜN İÇİ ÖN ANALİZ**: bugünkü (tamamlanmamış) mum analiz dışı bırakılır,
  yalnızca bilgi amaçlı gösterilir; güven seviyesi bir kademe düşürülür ve
  "aksiyona geçilebilir" (güçlü alım) bayrağı kapatılır.
- Son mum bugüne aitse ve kapanış tarama saati geçtiyse →
  **KESİNLEŞMİŞ KAPANIŞ** (bugünkü mum artık kesinleşmiş sayılır).

Kesinleşmiş kapanış sinyalleri veritabanına **sembol + işlem günü** bazında
tek kez kaydedilir (aynı gün için tekrar kaydedilmez). Gün içi ön analizler
veritabanına kesin sinyal olarak **kaydedilmez**.

---

## XU100 Ayarı

`.env` içinde `XU100_SYMBOL=XU100.IS` varsayılanı kullanılır. Bu sembol
çalışmazsa (yfinance'ten veri gelmezse) sistem **çökmez**; piyasa rejimi
"veri_yetersiz" olarak işaretlenir, ilgili uyarı analiz mesajında gösterilir
ve hisse analizi (teknik + destek/direnç kısmı) normal şekilde devam eder.
Farklı bir endeks kullanmak isterseniz `.env`'den değiştirebilirsiniz.

---

## Sektör Eşleştirme

`app/config/sector_map.yaml` dosyasında sembol → sektör endeksi eşleştirmesi
tutulur:

```yaml
symbols:
  THYAO:
    sector_name: Ulastirma
    sector_index: XULAS.IS
  SVGYO:
    sector_name: Gayrimenkul Yatirim Ortakligi
    sector_index: XGMYO.IS
```

Eşleştirme dosyada yoksa sistem **asla otomatik tahmin üretmez**;
"Sektör eşleştirmesi bulunamadı" mesajı gösterilir. Telegram'dan ekleme:

```
/sektor_ayarla SVGYO XGMYO.IS Gayrimenkul Yatirim Ortakligi
/sektor SVGYO
/sektor_listesi
```

Sektör endeksi yfinance'ten alınamazsa analiz çökmez; sektör gücü
"veri yok" olarak nötr puanlanır (diğer kategorilere puan aktarılmaz).

---

## Akşam Taraması

`CLOSE_SCAN_ENABLED=true` ve `CLOSE_SCAN_TIME=18:20` iken, `run_bot.py`
çalışırken APScheduler her gün belirtilen saatte otomatik tarama yapar.
Tarama yalnızca **izleme listesindeki** sembolleri kapsar; ayrıca yerel
`data/symbols/bist_symbols.csv` sembol evreni piyasa genişliği (`/piyasa`)
için kullanılır (bot internetten kontrolsüz şekilde tüm BIST'i taramaz).

Manuel tetikleme: `/tara`. Diğer komutlar: `/tara_liste`, `/tarama_durumu`,
`/aksam_raporu`, `/tarama_ayarlari`. Tek sembolün veri hatası **tüm
taramayı durdurmaz**; "veri alınamayan" listesinde ayrıca bildirilir.

Kill switch (`/acil_durdur`) aktifken tarama **başlatılamaz** (hem
Telegram komutu hem otomatik zamanlanmış görev bu kontrolden geçer).

---

## Portföy Komutları

```
/pozisyon_ekle SVGYO 1000 15.20
/pozisyon_sil SVGYO
/pozisyon_guncelle SVGYO 1200 14.90
/portfoy
/portfoy_risk
/pozisyon_boyutu SVGYO 100000 0.75
/maliyet SVGYO
/nakit_ayarla 50000
/sermaye_ayarla 150000
```

`/portfoy_risk`: toplam değer, sektör dağılımı, en yoğun sektör, tüm
stoplar gerçekleşirse oluşacak maksimum zarar senaryosu.

---

## Alarm Sistemi

```
/alarm_kur SVGYO ust 14.25       (direnç üstünde kapanış)
/alarm_kur SVGYO alt 12.18       (destek altında kapanış)
/alarm_kur SVGYO hacim 2         (ortalamanın 2 katı hacim)
/alarm_kur SVGYO skor 75         (skor 75 üstüne çıktı)
/alarm_kur SVGYO skor_altinda 35 (skor 35 altına indi)
/alarm_kur SVGYO sinyal alim     (sinyal türü değişti)
/alarm_kur SVGYO gunluk_destek
/alarm_kur SVGYO haftalik_direnc_kirilimi
/alarm_kur SVGYO ortak_destek
/alarm_kur SVGYO hacim_patlamasi
/alarm_kur SVGYO rsi_asiri_satim
/alarm_kur SVGYO haber_etkisi 60
/alarm_kur SVGYO xu100_guc 75
/alarm_kur SVGYO hedef 1
/alarmlar
/alarm_sil ID
/alarm_durdur EID
/alarm_ac EID
/alarm_detay EID
```

Aynı alarm aynı tamamlanmış mum ve durum için tekrar gönderilmez. Kalıcı
tetik kayıtları yeniden başlatma sonrasında dedup/cooldown durumunu korur;
tek sembol hatası diğer sembollerin taramasını durdurmaz.

---

## Grafikler

```
/grafik SVGYO
/grafik SVGYO 1yil
/rs_grafik SVGYO
```

matplotlib patches ile gerçek mum gövdesi/fitili üretir. Günlük görünümde
EMA20/50/100/200, Bollinger, VWAP, seviye zoneları, işlem planı, hacim,
RSI ve MACD; gün içi görünümde VWAP, EMA20/50, seans/gap seviyeleri, RVOL,
hacim ve RSI bulunur. Grafik dosyaları
**geçici** olarak oluşturulur ve Telegram'a gönderildikten hemen sonra
diskten silinir. Yeniden render'ı önleyen asıl cache dosyası TTL süresince
`CHART_CACHE_DIR` altında tutulur.

---

## Sinyal Performansı

```
/sinyaller
/aktif_sinyaller
/sinyal SVGYO
/sinyal_gecmisi SVGYO
/performans
/performans 30
/performans 90
```

Yeterli örnek (`PERFORMANCE_MINIMUM_SAMPLE_SIZE`, varsayılan 20) yoksa
sistem **yanıltıcı bir yüzde göstermez**; "Güvenilir performans
değerlendirmesi için yeterli sinyal bulunmuyor." yazar.

---

## Backtest

```
/backtest SVGYO
/backtest SVGYO 2y
/backtest SVGYO 5y
```

XU100 karşılaştırması ve buy&hold getirisi otomatik eklenir (XU100 verisi
alınamazsa backtest yine de çalışır, yalnızca karşılaştırma alanı boş
kalır). Look-ahead bias yoktur: sinyal, üretildiği mumdan **sonraki** mumun
açılışında gerçekleşir. Aynı günlük mumda hem stop hem hedef görülürse
varsayılan olarak **muhafazakâr** (stop) sonuç seçilir.

---

## Veri ve grafik cache

Kısa süreli bellek cache'i rate-limit yükünü azaltır. Aşama 5d disk cache'i
ise son başarılı OHLCV veri setini sağlayıcı ve oluşturulma zamanı meta
verisiyle saklar. Provider'lar çalışmazsa günlük/gün içi ayrı maksimum yaş
sınırı uygulanır; eski cache kesin analizde kullanılmaz. Grafik cache anahtarı
sembol, zaman dilimi, veri zamanı, grafik türü ve bağlamı içerir.

---

## Sağlık ve veri durumu

- Telegram: `/veri_durumu` veya `/veri_durumu SVGYO`
- API: `/health`, `/health/data`, `/health/providers`, `/health/scheduler`

Provider endpoint'i varsayılan olarak canlı ağ çağrısı yapmaz. Kontrollü
canlı probe için `/health/providers?probe=true` kullanılabilir.

---

## Migration (Alembic)

```bash
# Yeni kurulum veya guncelleme sonrasi:
alembic upgrade head

# Mevcut migration durumunu gormek icin:
alembic current

# Yeni bir migration olusturmak icin (gelistirici):
alembic revision --autogenerate -m "aciklama"
```

`migrations/versions/0001_v3_baseline.py`, önceden `Base.metadata.create_all()`
ile (Alembic izlemesi olmadan) oluşturulmuş bir V2 veritabanını **veri
kaybı olmadan** V3 şemasına taşır: yalnızca eksik olan (V3'te eklenen)
tablolar oluşturulur, mevcut tablolara **dokunulmaz**.

---

## Tüm Telegram Komutları

```
/start              Ana menü (inline butonlarla)
/yardim             Komut listesi
/ekle SEMBOL        Izleme listesine ekle
/sil SEMBOL         Izleme listesinden cikar
/liste              Izleme listesi
/analiz SEMBOL      Kisa ozet analiz (gun ici/kapanis otomatik ayrilir)
/analiz_detay SEMBOL Detayli analiz (skor kirilimi dahil)
/skor_detay SEMBOL  8 kategorili skor detayi
/sektor_ayarla SEMBOL ENDEKS.IS Ad   Sektor eslestirme ekle
/sektor SEMBOL      Sektor bilgisi + goreceli guc
/sektor_listesi     Tum sektor eslestirmeleri
/seviyeler SEMBOL   Cok-zamanli (gunluk/haftalik/aylik) destek-direnc + cakisan bolgeler
/senaryo SEMBOL     Dusus/yukselis senaryo bolgeleri
/kirilsanaryo SEMBOL  "Bu seviye kirilirsa ne olur?" kirilim senaryosu
/guc SEMBOL         Donemsel (1hafta/1ay/3ay/6ay) XU100 ve sektor goreceli guc
/gunici SEMBOL      Gun ici on analiz (kesinlesmis sinyal degildir)
/veri_durumu [SEMBOL] Veri/provider/cache kalite durumu
/tara               Izleme listesini tara
/tara_liste         Tarama listesini goster
/tarama_durumu      Son tarama durumu
/aksam_raporu       Aksam raporunu manuel calistir
/tarama_ayarlari    Tarama ayarlarini goster
/sinyaller          Son sinyaller
/aktif_sinyaller    Acik sinyaller
/sinyal SEMBOL      Son sinyal detayi
/sinyal_gecmisi SEMBOL  Sinyal gecmisi
/performans [gun]   Basari raporu
/portfoy            Portfoy ozeti
/portfoy_risk       Portfoy risk gorunumu
/pozisyon_ekle SEMBOL LOT MALIYET
/pozisyon_sil SEMBOL
/pozisyon_guncelle SEMBOL LOT MALIYET
/pozisyon_boyutu SEMBOL SERMAYE RISK_YUZDESI
/maliyet SEMBOL
/nakit_ayarla TUTAR
/sermaye_ayarla TUTAR
/alarm_kur SEMBOL TUR DEGER
/alarmlar
/alarm_sil ID
/alarm_durdur ID
/alarm_ac ID
/alarm_detay ID
/grafik SEMBOL [6ay|1yil]
/rs_grafik SEMBOL
/piyasa / /genislik  Piyasa genisligi
/backtest SEMBOL [2y|5y]
/ayarlar [ANAHTAR DEGER]
/durum              Sistem/veri sagligi
/acil_durdur        Kill switch (tum analiz/tarama/alarm durur)
/devam_et           Kill switch'i kapat
```

---

## Test Çalıştırma

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

Test paketi mevcut davranışların yanında veri kalite kusurlarını, retry/
circuit-breaker/fallback sırasını, alarm doğrulama-dedup-cooldown akışını,
seviye stabilitesini, gerçek mum/panel/cache grafiklerini, fresh ve Aşama 5c
migration'ını, health uçlarını ve Telegram komut kaydını kapsar.

---

## Güvenlik

- Tüm sırlar yalnızca `.env` dosyasında tutulur, kod içinde sabit değer yoktur.
- **Token/secret maskeleme**: `app/utils/logging_filters.py`, tüm loglarda
  (root logger + httpx/telegram kütüphane logger'ları dahil) Telegram bot
  tokenini, API anahtarlarını, webhook secret'ları ve Authorization
  header'larını otomatik maskeler. Örnek:
  `https://api.telegram.org/bot***MASKED***/getUpdates`
- Telegram whitelist (`ADMIN_TELEGRAM_USER_IDS`) boşsa geliştirme kolaylığı
  için herkese açıktır — **production'da mutlaka doldurun**.
- Kill switch aktifken analiz/tarama/alarm/paper-trading işlemleri
  çalışmaz; yalnızca `/durum`, `/devam_et`, `/yardim` çalışır.
- Docker container root olmayan `mergen` kullanıcısıyla çalışır.
- Canlı emir gönderimi `app/execution/disabled_live_broker.py` içinde
  kasıtlı olarak devre dışıdır ve bu sürümde **hiçbir yerde eklenmemiştir**.

---

## Bilinen Sınırlamalar

- yfinance ücretsiz/gecikmeli olabilir; resmi BIST verisi yerine geçmez.
- Ücretsiz Yahoo kaynaklarının gün içi tarih aralığı ve rate-limit sınırları
  vardır; veri kalite kapısı bu durumda analizi engelleyebilir.
- KAP entegrasyonu yoktur; yalnızca devre dışı bir arayüz (`PublicDisclosureProvider`)
  mevcuttur — sahte bildirim üretilmez.
- Aşama 5g backtest motoru long-only ve tek eşzamanlı pozisyonludur. Rolling/
  expanding walk-forward, ayrı out-of-sample sonuçlar ve parametre hassasiyet
  raporu vardır; çoklu eşzamanlı portföy optimizasyonu yoktur.
- Piyasa genişliği hesaplaması `data/symbols/bist_symbols.csv` içindeki
  sınırlı örnek evrenle çalışır; gerçek kullanımda bu dosyayı
  genişletebilirsiniz.
- Otomatik emir gönderimi **yoktur ve eklenmeyecektir**.
- Aşama 5c: Portföy ağırlığı hesaplanırken, o an fiyatı çekilmeyen diğer
  pozisyonlar için ortalama maliyet fiyatı kullanılır (yaklaşık değerdir).
- BIST resmî tatil takvimi ayrıca bağlı değildir; olası eksik iş günleri
  uyarı olarak raporlanır, tek başına `INVALID` yapılmaz.
- Aşama 5c: `/guc` komutu ve pozisyon detay bloğu, ilgili sembol için
  yeterli ortak işlem günü verisi yoksa ilgili dönemi "veri yetersiz"
  olarak işaretler; sahte skor üretmez.

---

## Sorun Giderme

**"TELEGRAM_BOT_TOKEN ayarlanmamis" hatası**
`.env` dosyasında `TELEGRAM_BOT_TOKEN` değerini BotFather'dan aldığınız
tokenle değiştirin.

**"Bu sembol için güncel veri alınamadı." mesajı**
`yfinance` sağlayıcısı veri çekemedi. Sembolü kontrol edin, birkaç dakika
sonra tekrar deneyin. Sistem hiçbir zaman bu durumda mock veriye geçmez.

**"Veri kalitesi doğrulanamadığı için analiz oluşturulmadı." mesajı**
Kaynaktan gelen veri NaN, negatif fiyat, High<Low gibi bir tutarsızlık
içeriyor. Analiz kasıtlı olarak durdurulur; farklı bir zaman diliminde
tekrar deneyin.

**"Sektör eşleştirmesi bulunamadı" mesajı**
`/sektor_ayarla SEMBOL ENDEKS.IS Sektör Adı` ile ekleyin.

**Migration hatası**
`alembic current` ile mevcut durumu kontrol edin. `alembic upgrade head`
başarısız olursa veritabanınız DEĞİŞTİRİLMEMİŞ olur (migration
`checkfirst=True` ile çalışır, hiçbir DROP işlemi yapmaz).

**Docker build hatası**
`docker compose build --no-cache` ile temiz bir build deneyin.

**Grafik oluşturulamıyor**
matplotlib `Agg` backend ile (GUI'siz) çalışır; sunucu ortamında ek bir
ayar gerekmez. Disk alanı/yazma izni sorunlarını kontrol edin.

## MERGEN QUANT — Aşama 5e

Aşama 5e, mevcut kısa vadeli analizleri kaldırmadan güncel işlem fiyatını son
kesinleşmiş kapanıştan ayırır. Teknik indikatörler tamamlanmış mumlarla; seviye,
hedef, alarm ve portföy uzaklıkları merkezi güncel fiyat çözümleyicisiyle
hesaplanır. Fiyat önceliği intraday snapshot, tamamlanmış 5dk/15dk/1s mum,
provider quote ve son kesinleşmiş günlük kapanış sırasıdır.

Yeni komutlar:

- `/cokluzaman SEMBOL`: haftalık, günlük, 4 saat, 1 saat, 15 dakika ve 5 dakika ağırlıklı analiz
- `/uzunsenaryo SEMBOL`: koşullu uzun vadeli boğa/ayı senaryoları
- `/hedefkontrol SEMBOL FIYAT`: kullanıcı hedefini sinyalden bağımsız inceler
- `/hedefyolu SEMBOL [FIYAT]`: kademeli hedef yol haritası
- `/degerleme SEMBOL`: GYO şirketlerinde NAD ve iskonto/prim analizi
- `/uzungrafik SEMBOL [FIYAT]`: haftalık ve aylık logaritmik grafik
- `/sermaye_islemleri SEMBOL`: sermaye işlemleri ve düzeltme bilgileri
- `/hedefgecmisi SEMBOL`, `/hedefbasari [SEMBOL]`: hedef takip performansı

Yeni ortam ayarları:

```env
PRICE_ADJUSTMENT_MODE=adjusted
TIMEFRAMES=5m,15m,1h,4h,1d,1wk
TIMEFRAME_WEIGHT_WEEKLY=30
TIMEFRAME_WEIGHT_DAILY=25
TIMEFRAME_WEIGHT_4H=20
TIMEFRAME_WEIGHT_1H=15
TIMEFRAME_WEIGHT_15M=7
TIMEFRAME_WEIGHT_5M=3
```

Kurulum ve migration:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m alembic upgrade head
python -m pytest -q
python run_bot.py
```

Migration `0006_stage5e_long_term_targets_valuation` yalnızca yeni tablolar
ekler; mevcut kullanıcı, portföy, sinyal, alarm, haber, seviye ve senaryo
kayıtlarını değiştirmez. Temel veri veya kurumsal işlem verisi sağlayıcıda yoksa
değer üretilmez; çıktı açıkça “Veri bulunamadı / Veri yetersiz” olarak işaretlenir.

## MERGEN QUANT — Aşama 5f

Aşama 5f yeni bir kısa vadeli AL/SAT modeli eklemez. Telegram metinlerini
sadeleştirir, uzun vadeli senaryoları bağımsız teknik kaynaklarla doğrular,
hedef yolundaki mekanik yüzde formüllerini kaldırır ve güvenli release üretir.

### Kanıt gücü ne anlama gelir?

Uzun vadeli hedeflerde gösterilen `Kanıt gücü: 0–100`, hedefin gerçekleşme
olasılığı veya getiri garantisi değildir. Bu alan; teknik yapı, haftalık/aylık
trend, hacim ve likidite, XU100/sektör göreceli gücü, mevcutsa temel değerleme,
veri kalitesi ve spekülasyon riskinin sınırlı katkılarından oluşan açıklanabilir
bir destek puanıdır. Kısa vadeli sinyal güveni ile aynı alan/model değildir.

Temel veri yoksa puana olumlu katkı yapılmaz. Düşük likidite uzak hedefi daha
sert cezalandırır; düşük veri kalitesi yüksek kanıt gücünü sınırlar. Aşırı boğa
senaryosu için yeterli tarih, doğrulanmış logaritmik kanal veya Fibonacci
extension, kabul edilebilir hedef piyasa değeri, likidite ve en az üç bağımsız
teknik kaynak birlikte bulunmalıdır. Aksi halde uzak hedef üretilmez.

### Sade mesajlar ve grafik modları

- `/uzunsenaryo`: varsayılan görünümde en fazla üç boğa ve üç ayı bölgesi.
- `/hedefkontrol`: kullanıcı hedefini bot hedefi/AL-SAT sinyalinden ayrı tutan kısa özet.
- `/cokluzaman`: her zaman diliminde yalnızca trend, güç ve veri durumu.
- `Standart Grafik`: mumlar, EMA20/50, güçlü günlük/haftalık seviyeler, fiyat,
  stop, hedef 1–2, hacim ve RSI.
- `Detaylı Grafik`: EMA100/200, Bollinger, MACD, aylık/confluence bölgeleri,
  haber/anomali ve ek senaryo katmanları.
- `Uzun Grafik`: haftalık ve aylık logaritmik görünüm.

Teknik ayrıntılar Telegram butonlarından açılır. Provider/HTTP/Python hata
metinleri loglarda kalır; kullanıcı yalnızca sade veri durumu mesajı görür.

### Güvenli release

`scripts/build_release.py` fail-closed çalışır. Kaynak klasörde gerçek `.env`,
SQLite DB veya yaygın secret biçimi görürse ZIP üretmez. Arşiv manifesti ve
dosya hashleri oluşturulur, ZIP yeniden açılarak doğrulanır ve ZIP için ayrı
SHA-256 dosyası yazılır.

```powershell
python scripts/build_release.py `
  --source C:\path\to\clean-source `
  --output mergen-quant-stage5f-clean-ux-scenarios.zip
```

Çalışan kurulum klasöründe `.env` veya gerçek DB bulunması normaldir; release
scripti bilinçli olarak bu klasörü doğrudan paketlemeyi reddeder. Script temiz
bir kaynak staging klasöründe çalıştırılmalıdır. Mevcut migration geçmişi Aşama
5f'te değiştirilmemiştir.

## MERGEN QUANT — Aşama 5g

Aşama 5g, mevcut analiz ve sinyal motorlarını kaldırmadan onları point-in-time
geçmiş veri üzerinde çalıştırır. Varsayılan gerçekleşme modeli `next_open`,
intrabar belirsizlik politikası `conservative`'dir. Sinyal kapanışında aynı
fiyattan giriş yapılmaz; tamamlanmamış veya `INVALID` mum kullanılmaz. Raw ve
adjusted seriler karıştırılmaz; raw seride `split_factor` varsa lot, giriş,
stop ve hedefler birlikte düzeltilir.

Masraflar varsayılan olarak sıfır değildir:

```env
BACKTEST_COMMISSION_BPS=15
BACKTEST_SLIPPAGE_BPS=5
BACKTEST_SPREAD_BPS=10
BACKTEST_BSMV_BPS=0
BACKTEST_MINIMUM_COST=0
BACKTEST_INITIAL_CAPITAL=100000
BACKTEST_MAX_POSITION_PCT=20
BACKTEST_INTRABAR_POLICY=conservative
BACKTEST_ENTRY_MODEL=next_open
BACKTEST_MINIMUM_SAMPLE_SIZE=30
```

Walk-forward rolling veya expanding çalışır. Parametre seçici yalnızca eğitim
ve doğrulama dilimlerini görür; test dilimi ayrı `out_of_sample` sonucudur.
Aynı veri/config/seed aynı içerik run ID'sini ve sonucu üretir.

Hesaplanan ana metrikler: net ve yıllıklandırılmış getiri, XU100 ve benchmark
farkı, kazanma/kaybetme oranı, ortalama kazanç/zarar, kazanç-zarar oranı,
profit factor, beklenti, maksimum drawdown ve süresi, Sharpe, Sortino, Calmar,
volatilite, piyasa maruziyeti, pozisyon süresi, MAE/MFE, hedef 1/2/3, stop ve
zaman çıkış oranları ile kazanç/kayıp serileridir. Sonuçlar rejim, volatilite,
likidite, sembol, sektör, sinyal türü ve skor aralığına göre ayrılabilir.

Sinyal doğrulama katmanı her onaylı sinyalin o andaki özelliklerini değişmez
snapshot olarak saklar; 1, 5, 20, 60 işlem günü ve isteğe bağlı kendi ufkunda
getiri, benchmark farkı, MFE/MAE, hedef ve stop sonucunu izler. Geçmiş snapshot
yeni analiz verisiyle güncellenmez.

Kalibrasyon sabit reliability bin'leri, beta shrinkage ve deterministik
isotonic PAVA kullanır. Varsayılan minimum örnek 30'dur; sembol verisi azsa
sektör, o da azsa BIST geneli kullanılır. `calibrated_success_rate` yalnızca
geçmiş benzer sinyallerin oranıdır ve gelecek sonucu garanti etmez.

Sanal işlemler SQLite'ta kullanıcı bazında kalıcıdır. Stop/hedefler yalnızca
tamamlanmış, taze ve geçerli mumlarla kontrol edilir; parçalı çıkış, maliyet,
slippage ve split düzeltmesi desteklenir. Sistem broker API'si içermez ve gerçek
emir gönderemez.

Ana Telegram komutları:

- `/backtest SEMBOL [YYYY-AA-GG YYYY-AA-GG]`
- `/backtest_ozet`
- `/sanal_portfoy`
- `/sanal_performans [SEMBOL]`
- `/sinyalbasari [SEMBOL]`
- `/kalibrasyon [SEMBOL]`
- `/neden SEMBOL`

Analiz ekranındaki `Sanal İşlem Aç` butonu lot/stop/hedef/risk özetini gösterir
ve ikinci onaydan sonra yalnızca sanal kayıt açar. `Kararın Nedenleri` butonu
hesapta gerçekten kullanılan pozitif/negatif katkıları gösterir.

Migration:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

`0007_stage5g_backtest_paper_validation` additive'dir; eski migration'ları
değiştirmez ve kullanıcı, portföy, alarm, sinyal veya hedef kaydı silmez.

Bilinen sınırlamalar: Resmî BIST tatil/delist tarihsel evreni ayrı bir ücretli
veri seti olarak projede yoktur; motor sağlanan gerçek mum takvimini izler ve
survivorship uyarısı üretir. `next_vwap` için provider VWAP vermiyorsa tipik
fiyat kullanılır. `lower_timeframe` sırası yoksa conservative fallback uygulanır.
Ücretsiz provider verisi resmî BIST tick verisi değildir.

Güvenli release:

```powershell
python scripts/build_release.py `
  --source C:\path\to\clean-source `
  --output mergen-quant-stage5g-backtest-paper-validation.zip
```
