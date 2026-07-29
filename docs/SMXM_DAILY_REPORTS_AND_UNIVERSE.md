# Tüm Hisseler, SMXM Günlük Rapor ve Sanal Portföy

Bu paket; verilen `BIST_Tum_Hisseler_Listesi.pdf` dosyasındaki 571 kodu
Telegram evrenine, 08:00/21:00 raporlarına, sanal portföye ve geçmiş veri
backtestine bağlar. Mevcut `mergen_quant` paket adı ve Coolify/Docker çalışma
biçimi korunmuştur.

> Bu yazılım gerçek emir göndermez ve yatırım tavsiyesi üretmez. Giriş, stop,
> hedef ve puanlar doğrulanmış OHLC verisinden deterministik olarak hesaplanır.
> Groq yalnızca haber duygu sınıflandırması ve sade açıklama için opsiyoneldir.
> Veri kaynağı gecikmiş, eksik veya erişilemezse sistem veri uydurmaz.

## Telegram komutları

| Komut | İşlev |
|---|---|
| `/tum_hisseler [sayfa]` | PDF'ten alınan 571 kodu 50'şer ve ileri/geri düğmeleriyle gösterir. |
| `/eniyi50 [adet]` | Evreni tarar; setup puanı, giriş uzaklığı ve en az 2R hedefe göre en iyi 50 adayı sıralar. Tek sembol hatası taramayı durdurmaz. |
| `/sabah_raporu` | 08:00 SMXM raporunu elle üretir. |
| `/aksam_raporu`, `/smxm_aksam_raporu` | Aynı kapsamlı XU100 + 571 hisse kapanış ve sabah bias karşılaştırmasını üretir. |
| `/piyasa` | 571 hissenin yükselen/düşen, EMA20/50/200, hacim, zirve/dip ve piyasa puanı özetini verir. |
| `/piyasa long`, `/piyasa short`, `/piyasa tum` | Puan eşiğini geçen adayların tamamını Telegram sınırına göre sayfalar. |
| `/sanal_portfoy_olustur Ana 10000` | Gerçek para içermeyen sanal portföy açar. |
| `/sanal_portfoyler` | Sanal portföy bakiyelerini listeler. |
| `/smxm_backtest THYAO 2025-01-01 2025-06-01 10000` | Tarih ve başlangıç bakiyesi parametreli SMXM testi ve equity curve üretir. |

`/eniyi50` sonucu veri kalitesi filtresinden geçer. PDF'te hisse dışında fon
veya sertifika kodu bulunuyorsa ve sağlayıcı OHLC döndürmüyorsa o kod açıkça
başarısız sayılır; sahte fiyatla sıralanmaz. Sonuç bir saat önbelleğe alınır.

## Zamanlanmış işler

- Sabah: her gün `08:00`, `Europe/Istanbul`. Ana varlık yalnızca `XU100.IS`
  endeksidir; veri yoksa THYAO'ya veya başka hisseye sessizce geçmez. Dünün O/H/L/C özeti, ATR ve trend;
  altı maddelik SMXM checklist; VIX/DXY, volatilite, hacim ve haber sentiment'i
  ile 0-100 piyasa güveni; ekonomik takvim ve enstrüman etki eşlemesi üretir.
- Akşam: her gün `21:00`, `Europe/Istanbul`. XU100 kapanışı ve günlük değişim,
  571 hissenin piyasa genişliği, puanlı LONG/SHORT-RİSK sayımı, takvim
  zaman çizelgesi, kesin nedensellik iddia etmeyen olası etki açıklaması ve
  sabah bias'ı ile gerçekleşeni karşılaştırır.
- Her iki job da `try/except` korumalıdır. Bir rapor veya sembol hatası bot
  prosesini durdurmaz; yöneticiye kısa hata bildirimi gönderilir.
- Yeni 08:00 raporu açıkken eski `DAILY_BRIEF` planlanmaz; çift mesaj oluşmaz.

Sabah checklist maddeleri: günlük bias, HTF/LTF OB-FVG zone, sweep ve CHoCH/BOS,
A+ plan uyumu, haber riskinin temizliği, minimum 1:2 risk/getiri. Her madde
ayrı `✅/❌` olarak raporlanır.

Ekonomik takvim parser'ı Investing.com TR tablosundaki saat, ülke, önem,
gerçekleşen, beklenti ve önceki alanlarını okur. Sayfa engellenir veya yapısı
değişirse boş sonuçla güvenli biçimde devam eder; uydurma etkinlik eklemez.

## Görsel motoru

`app/modules/chart_engine.py`, Telegram için doğrudan PNG üretir. Tasarım:

- koyu `#0d1117` arka plan, parlak yeşil/kırmızı gerçek mumlar;
- OB ve FVG alanları, liquidity sweep, BOS/CHoCH işaretleri;
- BUY/SELL zone, sayısal ENTRY/SL/TP etiketleri ve büyük RR;
- büyük güncel fiyat, yön banner'ı, 6 maddelik checklist ve güven çubuğu;
- akşam haber zaman çizelgesi, equity curve/drawdown ve SMXM watermark.

Matplotlib'in `Agg` backend'i kullanılır. Böylece Coolify container'ında tarayıcı
veya Chrome gerektiren Kaleido bağımlılığı olmadan kararlı PNG render edilir.
Dosyalar `REPORT_CHART_OUTPUT_DIR` altında geçici oluşturulur ve Telegram'a
gönderildikten sonra silinir.

Örnekler:

- `docs/examples/smxm_morning_report_example.png`
- `docs/examples/smxm_evening_report_example.png`
- `docs/examples/smxm_equity_curve_example.png`

## Sanal portföy ve backtest kuralları

- Standart risk `%1`; son kapanan işlem zarardaysa sıradaki işlem `%0.5`.
- Minimum `2R` ve minimum `5/6` checklist zorunludur.
- Pazartesi (`0`) ve Cuma (`4`) yeni işlem açılması varsayılan olarak kapalıdır.
- Kullanıcı başına en fazla 3 sanal portföy ve portföy başına en fazla 2
  strateji çalışır.
- Aynı OHLC mumunda SL ve TP birlikte görülürse muhafazakâr olarak SL önce kabul
  edilir.
- Tüm işlemler `virtual_trades` tablosuna otomatik loglanır.
- Sonuç: trade sayısı, win rate, ortalama plan RR, maksimum drawdown, toplam
  getiri ve equity curve.

## Yeni veritabanı tabloları

Migration: `0009_smxm_reports_virtual_portfolios`.

- `virtual_portfolios`: ad, başlangıç/güncel bakiye ve oluşturulma zamanı.
- `virtual_trades`: enstrüman, yön, giriş, SL, TP, boyut, risk yüzdesi,
  açılış/kapanış, PnL, durum, checklist puanı, strateji ve notlar.
- `market_daily_report_logs`: sabah tahmini, kapanış, gerçek yön, tutarlılık,
  OHLC, günlük değişim ve haber JSON'u.

Migration eklemelidir; mevcut tablo ve kullanıcı verilerini silmez.
Migration ayrıca mevcut alarm servisinin kullandığı ancak eski modelde eksik
kalan `user_price_alerts.deleted_at` alanını veri silmeden onarır.

## Coolify ortam değişkenleri

Coolify uygulamasının **Environment Variables** bölümüne aşağıdaki değerleri
ekleyin. JSON değeri tek satır olmalıdır.

```dotenv
TIMEZONE_NAME=Europe/Istanbul
INSTRUMENTS=["THYAO","ASELS","KCHOL","GARAN","AKBNK","SASA","BIMAS","EREGL","TCELL","FROTO","EURUSD","XAUUSD","XAGUSD","US100"]
BIST_UNIVERSE_JSON_PATH=app/config/bist_instruments.json

MORNING_REPORT_ENABLED=true
MORNING_REPORT_TIME=08:00
EVENING_MARKET_REPORT_ENABLED=true
EVENING_MARKET_REPORT_TIME=21:00
DAILY_BRIEF_ENABLED=false
REPORT_CHART_OUTPUT_DIR=/tmp/mergen_quant_reports
REPORT_MAX_NEWS_EVENTS=8

ECONOMIC_CALENDAR_ENABLED=true
ECONOMIC_CALENDAR_URL=https://tr.investing.com/economic-calendar/
ECONOMIC_CALENDAR_TIMEOUT_SECONDS=15
VIX_SYMBOL=^VIX
DXY_SYMBOL=DX-Y.NYB

UNIVERSE_SCAN_TOP_N=50
UNIVERSE_SCAN_MAX_SYMBOLS_PER_RUN=1000
UNIVERSE_SCAN_WORKERS=3
UNIVERSE_SCAN_CACHE_MINUTES=60
UNIVERSE_SCAN_MINIMUM_SCORE=68

VIRTUAL_PORTFOLIO_MAX_PER_USER=3
VIRTUAL_PORTFOLIO_MAX_STRATEGIES=2
VIRTUAL_TRADE_RISK_PERCENT=1.0
VIRTUAL_TRADE_AFTER_LOSS_RISK_PERCENT=0.5
VIRTUAL_TRADE_MINIMUM_RR=2.0
VIRTUAL_TRADE_MINIMUM_CHECKLIST=5
VIRTUAL_TRADE_BLOCKED_WEEKDAYS=0,4

OPENROUTER_DAILY_REQUEST_LIMIT=0
OPENROUTER_LOCAL_RATE_LIMIT_ENABLED=false
OPENROUTER_MODEL_FALLBACKS=inclusionai/ling-3.0-flash:free,nvidia/nemotron-3-super-120b-a12b:free,openai/gpt-oss-20b:free
OPENROUTER_VISION_MODEL_FALLBACKS=google/gemma-4-26b-a4b-it:free,nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free,nvidia/nemotron-nano-12b-v2-vl:free
FUNDAMENTAL_PROVIDER=auto
FUNDAMENTAL_ALLOW_YAHOO_FALLBACK=true
```

`GROQ_ENABLED=true` kullanılacaksa `GROQ_API_KEY` de Coolify secret olarak
tanımlanmalıdır. Kod veya repoya anahtar yazmayın. Üretim alarmı için lisanslı
BIST sağlayıcısı kullanın; `yfinance` gecikmeli fallback'tir.

## Kurulum ve doğrulama

Coolify, yeni commit'i seçtikten sonra **Redeploy** yapmalıdır. Elle Docker ile:

```bash
docker compose build --no-cache
docker compose run --rm bot alembic upgrade head
docker compose up -d
docker compose logs -f bot
```

Container başlangıç betiği migration'ı zaten çalıştırır; ayrı migration komutu
ilk dağıtımda sonucu açıkça görmek içindir.

Yerel test:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
python scripts/import_bist_pdf.py "/path/BIST_Tum_Hisseler_Listesi.pdf" app/config/bist_instruments.json --expected-count 571
python scripts/generate_smxm_examples.py
pytest -q
```

Telegram smoke testi:

1. `/tum_hisseler` yazın; ilk sayfa ve ileri düğmesi gelmelidir.
2. `/eniyi50 10` yazın; tarama botu kilitlemeden sonuçlanmalıdır.
3. `/piyasa`, `/piyasa long` ve `/piyasa short` ile 571 kodluk kapsamı kontrol edin.
4. `/sabah_raporu` ve `/smxm_aksam_raporu` ile iki PNG'nin XU100 olduğunu kontrol edin.
5. `/sanal_portfoy_olustur Ana 10000` ve `/sanal_portfoyler` çalıştırın.
6. En az bir aylık aralıkla `/smxm_backtest THYAO 2025-01-01 2025-06-01 10000`
   çalıştırın; özet ve equity curve gelmelidir.
7. Coolify loglarında `smxm_morning_report` ve `smxm_evening_report` job'larının
   sırasıyla 08:00 ve 21:00'a kurulduğunu doğrulayın.

## Dosya yerleşimi

- `app/config/bist_instruments.json`: PDF'ten doğrulanmış 571 kod.
- `app/config/instruments.py`: env/JSON enstrüman yapılandırması.
- `app/modules/morning_report.py`: Modül 1.
- `app/modules/evening_report.py`: Modül 2.
- `app/modules/backtest_engine.py`: Modül 3.
- `app/modules/chart_engine.py`: Modül 4.
- `app/services/instrument_universe_service.py`: en iyi 50 taraması/cache.
- `app/telegram/smxm_report_handlers.py`: komutlar ve görsel teslimi.
- `migrations/versions/0009_smxm_reports_virtual_portfolios.py`: DB migration.
- `scripts/import_bist_pdf.py`: PDF evrenini tekrar üretme aracı.
- `scripts/generate_smxm_examples.py`: üç örnek görseli tekrar üretir.
- `tests/test_smxm_report_modules.py`: modül testleri.
- `tests/test_smxm_scheduler.py`: scheduler testleri.
