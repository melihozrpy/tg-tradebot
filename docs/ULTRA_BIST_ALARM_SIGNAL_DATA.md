# 🏔️ Montana Melih Hisse Bot — Ultra BIST Alarm, Sinyal ve Veri Rehberi

Bu belge, **BIST Fiyat Alarm Sistemi Uygulama Talimatı** ile `borsaist.txt`
gereksinimlerinin mevcut projeye nasıl uygulandığını, hangi işlevlerin lisanslı
veri veya kullanıcı yetkisi istediğini ve Coolify üzerinde güvenli işletim
adımlarını tek yerde toplar.

Kullanıcıya görünen marka **Montana Melih Hisse Bot**'tur. Geriye uyumluluğu
bozmamak için `mergen-quant` Python paket adı, `mergen_quant.db`, Docker
kullanıcısı ve logger adları gibi iç teknik tanımlayıcılar değiştirilmemiştir.

> Bu sistem yatırım tavsiyesi vermez ve broker'a emir göndermez. AL/TUT/STOP,
> TP ve temel analiz çıktıları açıklanabilir senaryolardır. BIST spot sürümü
> varsayılan olarak `LONG_ONLY=true` çalışır; SAT, pozisyonu olmayan kullanıcıya
> çıplak short açma talimatı değildir.

## Durum işaretleri

| İşaret | Anlamı |
|---|---|
| ✅ | Uygulama/domain/persistence katmanında uygulanmış işlev |
| 🔐 | Adaptör hazırdır; sözleşme, lisans, OAuth veya API anahtarı olmadan çalışmaz |
| ⚠️ | Geliştirme/fallback işlevi; kesin canlı veri olarak kullanılamaz |
| 🚫 | Bilinçli olarak kapsam dışı veya güvenlik nedeniyle kapalı |

## Gereksinim–uygulama matrisi

### Kalıcı kullanıcı fiyat alarmları

| PDF gereksinimi | Uygulama karşılığı | Durum / işletim notu |
|---|---|---|
| Tekli, yönlendirmeli alarm | `/alarm_kur`, `/tekli_alarm`; sembol → fiyat → koşul → onay akışı | ✅ Kayıttan önce özet ve kullanıcı onayı |
| Tek satır hızlı komut | `/alarm_kur ASELS 72.50 üstü` | ✅ Türkçe ondalık virgül/nokta destekli |
| Çoklu metin | `/toplu_alarm` veya `/alarm_listesi` | ✅ Önizleme, hatalı satır raporu, toplu onay |
| CSV/XLSX içe aktarma | `/alarm_dosya` | ✅ Boyut/satır sınırı ve kayıt öncesi doğrulama |
| Görsel/OCR | `/alarm_foto` veya `/fotodan_alarm` | ✅ Pillow + yerel Tesseract `tur+eng`; düşük güven fail-closed |
| 100+ alarm | Sembol bazında gruplanmış tarama ve kullanıcı başına yapılandırılabilir limit | ✅ Varsayılan etkin limit 500, tek import 250 |
| Üstü/altı | `PRICE_GTE`, `PRICE_LTE` | ✅ `Decimal` karşılaştırması |
| Yukarı/aşağı kesişim | `CROSS_UP`, `CROSS_DOWN` | ✅ Önceki ve yeni gözlem birlikte değerlendirilir |
| Yaklaşma ve yüzde koşulları | `PRICE_NEAR`, tabana göre yüzde artış/azalış | ✅ Koşul enum'ları; doğrulanmış taban fiyatı gerekir |
| Tek sefer/sürekli/manuel yeniden kurma | `ONE_SHOT`, `PERSISTENT`, `MANUAL_REARM`, `RECURRING_CROSS` | ✅ Histerezis ve yeniden kurma durumu saklanır |
| Alarmı sustur/ertele/devam/sil | `/alarm_durdur`, `/alarm_ertele`, `/alarm_devam`, `/alarm_sil` | ✅ Sahiplik kontrolü ve tahmin edilemez `ALR-…` referansı |
| Liste/detay/sayfalama | `/alarmlar`, `/aktif_alarmlar`, `/tetiklenen_alarmlar`, `/alarm_detay` | ✅ Kullanıcı izolasyonu ve sayfalama |
| Sesli bildirim | Kullanıcı ses modu: metin, ilk tetik, periyodik | ✅ Telegram/telefon sessiz modunu bot aşamaz |
| Tekrarlı bildirim | Alarm başına tekrar aralığı, susturma ve ACK | ✅ Varsayılan 60 sn; alt sınır 30 sn |
| Kesin bir kez olay kaydı | Trigger dedup anahtarı + veritabanı teslimat kuyruğu | ✅ Yeniden başlatmada aynı olayı çoğaltmaz |
| Eşzamanlı teslimat claim'i | Due outbox satırında atomik status compare-and-set (`PENDING/RETRY` → `SENDING`) | ✅ İki worker satırı görse bile yalnız bir claim başarılı olur; 5 dk lease ile stuck recovery |
| Gönderim hatasında tekrar | Teslimat outbox'ı ve kontrollü retry | ✅ Kullanıcı/global dakika limiti ile |
| Her sembolü bir kez çekme | Aktif alarmları sembole göre gruplama | ✅ Aynı turda aynı sembol için tek çözümleme |
| Bayat/gecikmeli veriyi engelleme | Kaynak, veri timestamp'i, retrieval timestamp'i, kalite ve market durumu kapısı | ✅ Üretimde fail-closed |
| Scheduler bağımsızlığı | Fiyat alarmı işleri akşam kapanış taramasından ayrı çalışır | ✅ `CLOSE_SCAN_ENABLED=false`, alarm scheduler'ını kapatmamalıdır |
| Geçici dosya temizliği | Import/OCR işi TTL ve boyut sınırı | ✅ Kalıcı kaynak görsel yerine normalize satırlar saklanır |

### BIST sinyal yaşam döngüsü ve gerçekleşme modeli

| `borsaist.txt` gereksinimi | Uygulama karşılığı | Durum / işletim notu |
|---|---|---|
| Spot ve long-only | `LONG_ONLY=true` | ✅ Kripto, kaldıraç, fonlama ve likidasyon mantığı yok |
| Açık durum makinesi | `PENDING_ENTRY`, `ACTIVE`, `TP1_HIT`, `TP2_HIT`, `TP3_HIT`, `STOPPED`, `EXPIRED`, `INVALIDATED`, `CANCELLED`, `CLOSED_MANUALLY`, `EXIT_PENDING`, `UNFILLED`, `SUSPENDED` | ✅ Enum tabanlı; sermaye düzeltmesi state değiştirmeyen immutable event'tir |
| Geçersiz geçişin reddi | Saf yaşam döngüsü servisi, transition hata kaydı ve immutable event | ✅ Durum bozulmadan reddedilir |
| Giriş gerçekten oluşmadan ACTIVE olmama | OHLC/tick gözlemi ve emir türüne özel gerçekleşme | ✅ Plan fiyatı gerçekleşmiş fiyat sayılmaz |
| Emir türleri | `LIMIT_BUY`, `BREAKOUT_BUY`, `NEXT_OPEN`, `ENTRY_ZONE`, `MANUAL_ENTRY` | ✅ Tür bazlı kurallar |
| Breakout onayı | touch, intraday cross, completed close, close+volume, retest, next-open | ✅ Tarihsel onayda tamamlanmamış mum kullanılmaz |
| BIST fiyat adımı | Merkezi, sürümlü market-rule ve tick rounding servisi | ✅ Ham ve yuvarlanmış fiyat ayrılır |
| Tavan/taban | Enstrüman/pazar bazlı açık limit yüzdesi, limit-lock ve ilk işlem yapılabilir fiyat modeli | ✅ Genel `%10` varsayımı yok; yüzde/metadata yoksa fail-closed, likidite yoksa sahte fill yok |
| Devre kesici/suspension/tek fiyat | Trading-state enum ve sinyal bazlı immutable olaylar | ✅ Geçerli işlem yokken emir pending/suspended kalır |
| Global seans audit akışı | Dedup'lı `MarketSessionEvent` persistence modeli | ⚠️ Tablo/constraint hazır; otomatik normalize session-feed ingestor'u henüz yok |
| Üç ayrı hedef | TP1/TP2/TP3 ayrı hedef durumu, zaman, fiyat, miktar, PnL | ✅ Bir hedef diğerlerini otomatik gerçekleşmiş saymaz |
| Kısmi kâr dağılımı | Varsayılan `%40 / %35 / %25`; toplam `%100` doğrulaması | ✅ Lot bazında kalan miktar korunur |
| Stop yönetimi | Fixed; TP1 sonrası breakeven; TP2 sonrası TP1 stopu; `/stop_girise` | ✅ Long stop yalnız yukarı taşınabilir |
| ATR/structure/manual trailing | `ATR_TRAILING`, `STRUCTURE_TRAILING`, `MANUAL_TRAILING` `StopPolicy` değerleri | ⚠️ Bu sürümde yalnız enum; ATR/yapıdan stop hesaplayan otomatik runtime worker yok |
| Pozisyon boyutlama | Risk bütçesi, nakit, portföy yüzdesi, hacim katılımı, komisyon rezervi | ✅ Finansal hesaplarda `Decimal`, bütün lot |
| Kısmi gerçekleşme | full, volume-limited, conservative-volume-limited | ✅ İstenen/dolan/kalan lot ayrı |
| İşlem maliyetleri | Komisyon, komisyon vergisi, slippage ve asgari ücret modeli | ✅ Koşu config'iyle birlikte saklanmalı |
| Aynı mumda TP+stop | lower-timeframe, yoksa conservative | ✅ Gelecek mumla geriye karar verilmez |
| Kurumsal işlem runtime çekirdeği | Split/bedelsiz/reverse split için atomik fiyat-lot düzeltmesi ve state-preserving `CORPORATE_ACTION_APPLIED` olayı | ✅ Sahiplik/dedup/tam lot/katsayı doğrulaması; kaynak metadata'sı yoksa reddedilir |
| Kurumsal işlem otomasyonu | Lisanslı normalize feed → `apply_corporate_action_adjustment` çağrısı | ⚠️ Feed hook/worker/scheduler henüz bağlı değildir; otomatik uygulanmış gibi gösterilmez |
| Point-in-time backtest | Kronolojik replay, next-session fill, eksik mum engeli, no-lookahead | ✅ Sağlayıcının tarihsel evren/işlem durumu kapsamı ayrıca raporlanır |
| Watchlist/sektör/endeks evreni | Arka plan koşusu, kullanıcı/kill-switch/concurrency sınırı | ✅ Boş veya doğrulanamayan evren uydurulmadan reddedilir |
| BIST30/50/100 üyeliği | `bist_index_membership.csv` güncel snapshot doğrulaması | ✅ Tam 30/50/100 ve güncellik kanıtı yoksa fail-closed; tarihsel üyelik yoksa survivorship uyarısı |
| 45,50 kabul senaryosu | 1.000 lotun 400/350/250 hedef dağılımını sınayan deterministik domain testi | ✅ Durum/event/miktar/maliyet idempotency kontrolü |
| Kullanıcı sinyal takibi | `/takip`, bırak/iptal/stop-giriş/manuel-kapat/aktif pozisyon komutları | ✅ Sahiplik kontrollü, DB-backed, gerçek emir göndermeyen sanal takip |
| Plan süresi | Timeframe bazlı `expires_at`, quote gerektirmeyen idempotent `SIGNAL_EXPIRED` event/outbox | ✅ Monitor scheduler açıksa piyasa kapalıyken ve quote takibi bırakılmışken de due plan expire edilir |
| Canlı izleme | Non-final sinyaller, stale/delayed/valid-trade/trading-state kalite kapısı, dedup event | 🔐 Düşük gecikmeli güvenilir sonuç için lisanslı BIST quote'u ve seans metadata'sı zorunlu |
| Canlı broker emri | Yok | 🚫 Analiz, alarm, backtest ve paper-trading ile sınırlı |

### Teknik, temel ve kamuyu aydınlatma verisi

| Gereksinim | Uygulama karşılığı | Durum / işletim notu |
|---|---|---|
| Tamamlanmış mum | Teknik sinyal ve geçmiş doğrulamada incomplete bar çıkarılır | ✅ Açık mumdan “kesinleşti” sonucu yok |
| Çoklu zaman dilimi | 5dk, 15dk, 1s, 4s, günlük, haftalık | ✅ Her dilim kendi veri/kalite sonucunu taşır |
| Look-ahead engeli | Centered/future swing yerine nedensel teyit zamanı | ✅ Sinyal tarihi pivotun sonradan bilinen tarihi değildir |
| Güncel fiyatın kaynağı | `source`, `data_timestamp`, `retrieved_at`, gecikme ve kalite | ✅ Her tetiklenebilir gözlem izlenebilir olmalı |
| Kesin canlı BIST fiyatı | Yerleşik `licensed_rest` quote/OHLCV/market-state adaptörü | 🔐 Kod hazır; sağlayıcı sözleşmesi, gerçek endpoint şeması ve API anahtarı zorunlu |
| Yahoo/yfinance | OHLCV/quote fallback | ⚠️ Gecikme/tamlık/SLA garantisi yok; resmî veya bağımsız ikinci kaynak değildir |
| Fintables temel analiz | MCP üzerinden kullanıcının OAuth yetkisi | 🔐 Sayfa kazıma ve hesabı çoklu kullanıcıya ortak kullandırma yok |
| KAP temel veri | Sözleşmeli REST/gateway adaptörü | 🔐 API anahtarı, izinli IP ve kullanım/yeniden dağıtım hakkı gerekir |
| Temel veri çapraz kontrolü | Primary + bağımsız secondary, tolerans ve kaynak provenance | ✅ Çelişki strict modda fail-closed |
| Yahoo temel veri | Açıkça düşük güvenli fallback | ⚠️ Lisanslı mali tablo kaynağının eşdeğeri değildir |
| KAP bildirimleri | `KAP_PROVIDER=kap_rest` ve `/kap SEMBOL` talep-anı disclosure sorgusu | 🔐 Adaptör hazır; sözleşme/feed ve yeniden dağıtım hakkı gerekir. Otomatik push aktif değildir |
| LLM anlatımı | Doğrulanmış metriklerden sade açıklama | ✅ LLM rakam, fiyat, bilanço kalemi veya AL/SAT kararı uydurmamalı |

## Alarm komutları

| Komut | İşlev |
|---|---|
| `/alarm_kur` | Yönlendirmeli tekli alarm başlatır |
| `/alarm_kur ASELS 72.50 üstü` | Tek satırda alarm önizlemesi oluşturur |
| `/tekli_alarm` | `/alarm_kur` kısa akışının diğer adı |
| `/toplu_alarm` | Çok satırlı metin içe aktarmayı başlatır |
| `/alarm_listesi` | `/toplu_alarm` diğer adı |
| `/alarm_foto` veya `/fotodan_alarm` | Görselden OCR içe aktarma başlatır |
| `/alarm_dosya` | CSV/XLSX içe aktarma başlatır |
| `/alarmlar` | Tüm alarmları sayfalı listeler |
| `/aktif_alarmlar` | Etkin alarmları listeler |
| `/tetiklenen_alarmlar` | Tetiklenmiş alarmları listeler |
| `/alarm_detay ALR-XXXXXX` | Kullanıcıya ait alarmı gösterir |
| `/alarm_durdur ALR-XXXXXX` | Alarmı onaylayıp tekrarını durdurur |
| `/alarm_ertele ALR-XXXXXX 5` | Alarmı belirtilen dakika erteler |
| `/alarm_devam ALR-XXXXXX` | Alarmı yeniden etkinleştirir |
| `/alarm_sil ALR-XXXXXX` | Alarmı soft-delete eder |
| `/alarm_ayar` | Tekrar, ses ve seans tercihlerini gösterir/değiştirir |
| `/alarm_yardim` | Kısa kullanım kılavuzunu gösterir |

Telegram sesi cihazın bildirim/sessiz moduna tabidir. Bot, işletim sisteminin
sessiz modunu aşamaz; “alarm çalıyor” mesajı gönderim başarısının cihazda ses
çıkacağı garantisi değildir.

### Alarm outbox claim ve retry

Delivery worker önce due satır ID'lerini okur, sonra her satırı tek bir koşullu
DB güncellemesiyle yalnız `PENDING`/`RETRY` ve zamanı gelmişse `SENDING` yapar;
aynı atomik işlem `attempted_at` ve `attempt_count` alanlarını da günceller.
Eşzamanlı iki worker aynı ID'yi görse bile etkilenen satır sayısı yalnız birinde
`1` olur; diğeri Telegram gönderimine geçmez. Worker gönderim ortasında kapanırsa
5 dakikayı aşan `SENDING` lease'i `RETRY` durumuna geri alınır. Bu compare-and-set
teslimat çoğaltmasını sınırlar; Telegram polling ve diğer scheduler işlerine
genel bir dağıtık lock sağlamaz. Telegram mesajı kabul edildikten sonra, fakat
`SENT` DB commit'inden önce process çökerse retry dış sistemde yinelenen mesaj
üretebilir; Telegram API uçtan uca exactly-once/idempotency anahtarı sunmadığı
için bu crash penceresi operasyonel metrik ve loglarla izlenmelidir.

### İlgili sinyal, backtest ve portföy komutları

| Komut | İşlev |
|---|---|
| `/sinyaller` | Kullanıcının sinyal geçmişini listeler |
| `/aktif_sinyaller` | Final duruma gelmemiş sinyalleri gösterir |
| `/sinyal ID veya SEMBOL` | Görülebilir kaydı ID ile ya da sembolün son sinyal planını mevcut durum ve olay geçmişiyle gösterir |
| `/sinyal_gecmisi SEMBOL` | Sembolün sinyal geçmişini listeler |
| `/performans [gün]` | Yeterli örnek varsa sinyal performansını hesaplar |
| `/backtest SEMBOL 1g 5y` | Timeframe + dönemle (en fazla 10 yıl) point-in-time backtest başlatır |
| `/backtest SEMBOL YYYY-AA-GG YYYY-AA-GG` | Geriye uyumlu ISO başlangıç/bitiş sözdizimi |
| `/backtest_ozet` | Son backtest koşularını özetler |
| `/sanal_portfoy` | Paper-trading pozisyonlarını gösterir |
| `/sanal_performans [SEMBOL]` | Sanal işlem performansını gösterir |
| `/sinyalbasari [SEMBOL]` | Saklanan sinyallerin gerçekleşme istatistiğini verir |
| `/kalibrasyon [SEMBOL]` | Yeterli örnek varsa güven kalibrasyonunu gösterir |
| `/neden SEMBOL` | Sinyale gerçekten katkı veren açıklanabilir nedenleri gösterir |
| `/takip ID` | Analiz planını kullanıcıya ait `PENDING_ENTRY` sanal takibe alır |
| `/takip_birak ID` | İzlemeyi kapatır; kayıt ve olay geçmişini silmez |
| `/sinyal_iptal ID` | Yalnız `PENDING_ENTRY` planını iptal eder |
| `/stop_girise ID` | Açık sanal pozisyonun stopunu gerçekleşmiş girişe taşır |
| `/pozisyon_kapat ID` | Doğrulanmış güncel işlem fiyatıyla sanal pozisyonu kapatır |
| `/aktif_pozisyonlar` | Açık sanal takip pozisyonlarını listeler |
| `/portfoy` ve `/portfoy_risk` | Portföy ve risk özetini gösterir |
| `/pozisyon_boyutu SEMBOL` | Risk/nakit/lot sınırlarıyla önerilen miktarı hesaplar |
| `/nakit_ayarla TUTAR` | Kullanıcı nakit ayarını günceller |
| `/kap SEMBOL` | Lisanslı modda son KAP bildirimlerini; kapalı/hata modunda resmî KAP arama linkini gösterir |
| `/backtest_signal ID` | Görülebilir kayıtlı sinyal planını üretim kaydını değiştirmeden tarihsel mumlarla replay eder |
| `/backtest_gecmisi` | `/backtest_ozet` ile aynı kullanıcıya ait koşu özetini gösterir |
| `/backtest_stats` | Kullanıcının tüm kayıtlı koşu/işlem maliyet ve K/Z istatistiğini toplar |
| `/backtest_watchlist 1g 3y` | Kullanıcının doğrulanmış izleme listesinde evren backtesti |
| `/backtest_sector XBANK 1g 5y` | Yerel aktif sektör üyelerinde evren backtesti |
| `/backtest_bist30 1g 3y` | Doğrulanmış tam 30 üyeli güncel snapshot ile backtest |
| `/backtest_bist50 1g 3y` | Doğrulanmış tam 50 üyeli güncel snapshot ile backtest |
| `/backtest_bist100 1g 3y` | Doğrulanmış tam 100 üyeli güncel snapshot ile backtest |

Tek-sembol `/backtest` komutu `5d/5m`, `15d/15m`, `1s/1h`, `4s/4h`, `1g/1d`
ve `1hf/1w/1wk` timeframe aliaslarını kabul eder. Dönem `g/d/w/hf/a/y`
birimleriyle 1 gün–10 yıl arasında olmalıdır. Yalnız sembol verilirse stratejinin
varsayılan timeframe'i ve iki yıllık pencere kullanılır. ISO tarih sözdiziminde
timeframe ayrıca verilmez; varsayılan timeframe kullanılır.

Evren komutlarında desteklenen timeframe girdileri `5d`/`5m`, `15d`/`15m`,
`1s`/`1h`, `1g`/`1d` ve `1hf`/`1w`/`1wk`; dönem girdileri gün/hafta/ay/yıl
birimleriyle en fazla 10 yıldır.

## Ultra BIST sinyal izleme davranışı

### Takibe alma ve sahiplik

`/takip ID`, mevcut analiz planını çalışmış emir gibi işaretlemez. Planın side'ı
BUY olmalı; giriş seviyesi/bölgesi, stop ve TP1–TP3 bulunmalıdır. Risk bütçesi,
nakit, maksimum pozisyon ve hacim katılım sınırı en az bir bütün lot üretiyorsa
kullanıcıya ait yeni `PENDING_ENTRY` kayıt açılır. Başka kullanıcıya ait kayıt
takibe alınamaz. Aynı kullanıcıya ait non-final kayıt için takip bayrağı yeniden
açılır.

Komut durum kuralları:

- `/takip_birak ID`: `monitoring_enabled=false`; sinyal ve immutable event geçmişi
  korunur.
- `/sinyal_iptal ID`: yalnız `PENDING_ENTRY` → `CANCELLED`; event/outbox kaydı
  oluşturur.
- `/stop_girise ID`: yalnız `ACTIVE`, `TP1_HIT` veya `TP2_HIT`; gerçekleşmiş
  ortalama giriş yoksa ya da yeni stop eskisinden yukarı değilse reddedilir.
- `/pozisyon_kapat ID`: yalnız kullanıcıya ait açık sanal pozisyon; kalan lotu
  doğrulanmış güncel işlem fiyatıyla `CLOSED_MANUALLY` yapar. Broker emri yoktur.
- `/aktif_pozisyonlar`: en fazla 30 açık sanal kaydı durum, giriş, stop ve kalan
  lotla listeler.
- Nihai durum (`TP3_HIT`, `STOPPED`, `EXPIRED`, `INVALIDATED`, `CANCELLED`,
  `CLOSED_MANUALLY`, `UNFILLED`) tekrar izlemeye açılamaz.

### Live-data fail-closed kapısı

`SIGNAL_MONITOR_ENABLED=true` olduğunda scheduler varsayılan 5 saniyede bir açık
ve izlenen BUY sinyallerini sembole göre gruplar. Kullanıcı kill-switch'i açıksa
sinyal atlanır; provider piyasa kapalı diyorsa quote çekilmez. Aynı sembol turda
bir kez çekilir. Bu kural fiyat-temelli geçişler içindir; aşağıdaki wall-clock
expiration, plan ömrünü sonsuza uzatmamak için quote takibi/kill-switch'ten
bağımsız değerlendirilir.

Bir quote'un durum değiştirebilmesi için tamamı gerekir:

- saat dilimli timestamp; gelecekte en fazla 5 saniye ve azami yaş sınırı içinde;
- açıkça `is_live=true` ve `is_fresh=true`;
- `valid_transaction=true`;
- desteklenen `trading_state`;
- pozitif fiyat ve işlem miktarı/volume;
- sembol ile kayıt eşleşmesi.

`MAX_MARKET_DATA_STALENESS_SECONDS` boşsa runtime 15 saniye kullanır.
`ALLOW_DELAYED_DATA_FOR_LIVE_TRIGGER=false` güvenli varsayılandır. Eksik, bayat,
gelecek tarihli, gecikmeli veya geçerli işleme dayanmayan veri giriş, TP, stop ya
da manuel kapanış üretmez. Anlık quote `bar_complete=true` demedikçe tamamlanmış
mum sayılmaz; completed-close breakout planı doğrulanmış tamamlanmış bar bekler.
Suspension, circuit breaker, order collection, closed ve no-valid-trade durumları
sahte fill üretmez; sinyali uygun pending/suspended durumda tutar.

Her state değişimi immutable `SignalEvent` ve tekil dedup key ile kaydedilir.
Telegram teslimatı ayrı DB outbox'ından retry/rate-limit kurallarıyla yapılır;
yeniden başlatma aynı olayı ikinci kez oluşturmamalıdır.

### Expiration

`/takip` sırasında saklanan wall-clock deadline'lar:

| Timeframe | `expires_at` yaklaşık süresi |
|---|---:|
| 5dk (`5m`/`5d`) | 3 saat |
| 15dk (`15m`/`15d`) | 6 saat |
| 1 saat (`1h`/`1s`) | 3 gün |
| 4 saat (`4h`/`4s`) | 7 gün |
| Günlük (`1d`/`1g`) | 8 gün |
| Haftalık (`1wk`/`1w`/`1hafta`) | 28 gün |
| Bilinmeyen | 8 gün |

Bu süreler resmî BIST işlem/tatil takvimi veya “tamamlanmış N mum” sayacı değil,
temkinli wall-clock yaklaşıklardır. `SIGNAL_MONITOR_ENABLED=true` iken expiry
adayları quote izlemesinden önce değerlendirilir: BIST kapalı olsa, provider quote
veremese veya kullanıcı `/takip_birak` ile quote takibini kapatsa bile due
`PENDING_ENTRY`, deadline timestamp'li `system_clock` observation ile tekil
`SIGNAL_EXPIRED` event'ine geçer. Monitoring kapanır, kapanış/data zamanı
saklanır ve Telegram outbox kaydı oluşturulur. DB dedup/reconciliation restart
sonrası aynı expiry'yi çoğaltmamalıdır. `SIGNAL_MONITOR_ENABLED=false` scheduler
işini tümden kapattığı için otomatik expiration da çalışmaz.

## BIST fiyat limiti, seans ve sermaye işlemleri

### Enstrüman bazlı günlük limit

Genel market-rule nesnesi günlük tavan/taban için evrensel `%10` varsaymaz.
`daily_price_limits(base_price, limit_percent=...)` çağrısı, lisanslı quote veya
reference-data kaynağından gelen enstrüman/pazar/işlem-yöntemi yüzdesini açıkça
almalıdır. Yüzde yoksa `MarketRuleError` ile fail-closed durur. Değer verildiğinde
ham bant önce hesaplanır; çalıştırılabilir alt/üst fiyatlar BIST tick'ine bandın
içinde kalacak yönde yuvarlanır. Limit-lock, order-book miktarı veya ilk işlem
yapılabilir fiyat metadata'sı yoksa fill varmış gibi üretilmez.

### Seans olayı audit modeli

`MarketSessionEvent`, provider kaynaklı suspension/devre-kesici/açılış benzeri
olaylar için `symbol`, `event_type`, `started_at`, isteğe bağlı `ended_at`,
`source`, metadata ve `unique_dedup_key` saklar. Tekil constraint restart sonrası
aynı provider olayının iki kez yazılmasını engelleyebilir. Bu yalnız persistence
modelidir: lisanslı normalize session feed'ini okuyup bu tabloya yazan ingest
worker/scheduler henüz bağlı değildir. Quote içindeki `trading_state` sinyal
geçişlerinde kullanılabilir; bunun global seans audit tablosunun otomatik dolduğu
anlamına gelmediği operasyon ekranında açık tutulmalıdır.

### Runtime sermaye işlemi düzeltmesi

`BistSignalRuntimeService.apply_corporate_action_adjustment` kullanıcıya ait
nihai olmayan sinyalde şu üç grubu destekler: stock split, bedelsiz/bonus ve
reverse split. `adjustment_factor`, *işlem sonrası pay / işlem öncesi pay*
anlamındadır; fiyatlar faktöre bölünür, miktarlar faktörle çarpılır. Emir fiyatı
alanları amaçlarına uygun BIST tick yönünde yuvarlanır; gerçekleşmiş ekonomik
değerler ile önceden kaydedilmiş PnL/maliyet tutarları korunur.

İşlem state'i geçici bir `CORPORATE_ACTION_ADJUSTED` durumuna taşımaz; mevcut
durumu koruyan immutable `CORPORATE_ACTION_APPLIED` event'i, before/after audit
snapshot'ı ve artırılmış row version ile aynı transaction'da yazılır. Kalıcı
`corporate_action_key` dedup anahtarı retry/restart sırasında faktörün ikinci kez
uygulanmasını engeller. Provider/source/key eksikliği, nihai sinyal, uyumsuz
katsayı ve tam BIST lotuna dönüşmeyen reverse split fail-closed reddedilir.

Bu runtime çekirdeğini KAP veya başka lisanslı normalize sermaye-işlemi feed'ine
bağlayan otomatik hook/worker/scheduler henüz yoktur. Metodun bulunması canlı
kurumsal aksiyonların kendiliğinden uygulandığı anlamına gelmez.

### Trailing-stop kapsamı

`ATR_TRAILING`, `STRUCTURE_TRAILING` ve `MANUAL_TRAILING` yalnız `StopPolicy`
enum değerleridir; ATR veya piyasa yapısından yeni stop hesaplayıp otomatik
uygulayan runtime worker bu sürümde yoktur. Uygulanan davranışlar TP1 sonrası
breakeven, TP2 sonrası TP1 stopuna yükseltme ve sahiplik kontrollü
`/stop_girise ID` komutudur. Long stopun aşağı taşınmasına izin verilmez.

## Ultra evren backtestleri

Watchlist, sektör ve BIST endeks komutları işi arka planda başlatır ve kullanıcıya
bir `Run ID` döndürür. Kullanıcı başına aktif ağır iş sınırı, global sembol üst
sınırı ve timeout uygulanır; kill-switch açıkken yeni iş başlamaz. `mock` provider
reddedilir. Bir sembol başarısız olursa diğerleri devam edebilir ve eksikler
raporda açıkça sayılır; sonuç eşit ağırlıklı bağımsız sembol koşularının
toplulaştırmasıdır, tek birleşik portföy optimizasyonu değildir.

`/backtest_gecmisi`, `/backtest_ozet` alias'ıdır.

### Sinyal-ID tarihsel replay

`/backtest_signal ID`, ortak analiz sinyaline veya yalnız komutu veren
kullanıcıya ait sinyale erişir; başka kullanıcıya ait kaydın varlığını açığa
çıkarmaz. Kayıtlı plan özgün giriş, `stop_price` ve TP değerleriyle üretim
DB'sinden salt-okunur ve detached olarak alınır; daha sonra taşınmış
`current_stop_price` başlangıç stopu yapılmaz.
Runtime geçişleri geçici bellek içi DB'de yürütüldüğü için kaynak `Signal`, event
geçmişi ve paper/live durumları değiştirilmez; gerçek broker emri yoktur.

Bilgi sınırı `max(created_at, data_timestamp, valid_from)` değeridir. Providerdan
bu andan sonraki sinyal timeframe'ine ait OHLCV alınır ve yalnız kronolojik,
tamamlanmış mumlar işlenir. `mock` provider; boş frame; OHLCV
alanı/timestamp eksikliği; uyuşmayan fiyat düzeltme modu veya şüpheli ilk-mum
boşluğu replay'i fail-closed durdurur. Provider serisi var ama filtre sonrası
uygun tamamlanmış mum yoksa fiyat geçişi uydurulmaz: kayıtlı deadline geçmişse
yalnız wall-clock `EXPIRED`, aksi halde dönem-sonu unfilled bilgisi raporlanır.
Güvensiz/geçersiz mum tetik üretmez. Aynı mum içi sıra bilinmediğinde muhafazakâr
stop-önce politikası uygulanır. Tavan/taban, kullanılabilir alış-satış miktarı,
devre kesici ve seans durumu ancak provider bu mikro-yapı alanlarını verdiyse
hesaba katılır.

Plan `PENDING_ENTRY` olarak tekrar kurulur; giriş/kısmi fill, TP1–TP3, stop,
askıya alma/devam ve varsa kayıtlı `expires_at` geçişleri canlı
`BistSignalRuntimeService` kurallarıyla ilerler. Dönem bittiğinde giriş dolmadıysa
rapor bunu açıkça söyler; kaynak sinyal `UNFILLED` yapılmaz. Çıktı kullanılan
providerı (metadata varsa fallback/cache provenance'ıyla), fiyat modunu, olay
zaman çizelgesini (18'den fazlaysa ilk 9 + son 9), son durumu, brüt/net
gerçekleşen K/Z'yi, maliyeti, açık lot varsa son kapanışa göre gerçekleşmemiş
brüt K/Z'yi ve MFE/MAE'yi içerir. Kayıtlı
lotu olmayan eski analiz planı 1 lota normalize edildiğini; komisyon/vergi ayarı
yoksa sıfır kullanıldığını açıkça belirtir.

### BIST30/50/100 üyelik dosyası

Varsayılan yol:

```text
data/symbols/bist_index_membership.csv
```

Dosya release/deployment içinde yoksa ya da güvenli okunamıyorsa BIST30/50/100
komutu fail-closed durur; `bist_symbols.csv` içinden tahmin veya ilk N sembol
seçilmez.

En küçük current-snapshot şeması:

```csv
symbol,index_code,active
AKBNK,XU030,true
AKBNK,XU050,true
AKBNK,XU100,true
```

Tarih kanıtlı alternatif:

```csv
symbol,index_code,as_of,effective_from,effective_to
AKBNK,XU030,2026-07-25,2026-07-01,2026-09-30
```

Doğrulama kuralları:

1. `symbol` zorunludur. Index alanı canonical `index_code`; geriye uyumlu olarak
   `index`, `index_name` veya `universe` da okunabilir.
2. Güncellik kanıtı için en az `active`, `as_of`, `as_of_date`, `effective_from`
   veya `effective_to` sütunlarından biri gereklidir.
3. `active` true değerleri `1`, `true`, `yes`, `evet`, `aktif`; diğer dolu
   değerler pasif sayılır.
4. Tarih biçimi ISO `YYYY-AA-GG` olmalıdır. Effective aralığı bugünü kapsamayan,
   gelecekteki `as_of` veya 120 günden eski snapshot satırı seçilmez.
5. Semboller `.IS` olmadan/ile verilebilir; normalize edilir, 3–8 alfanümerik
   karakter dışında değer reddedilir.
6. Index kodları XU030/BIST30/BIST030, XU050/BIST50/BIST050 ve XU100/BIST100
   aliaslarını kabul eder.
7. Filtre ve dedup sonrası XU030 tam 30, XU050 tam 50, XU100 tam 100 sembol
   olmalıdır. Eksik veya fazla sayıda komut başlamaz; sessiz kırpma/doldurma yoktur.

Aynı şirket her index için ayrı satır taşıyabilir. Current snapshot kullanımı,
geçmiş bir backtest tarihindeki gerçek endeks üyeliğini kanıtlamaz; koşu config'i
`membership_mode=current_snapshot` saklar ve rapor survivorship-bias uyarısı
verir. Gerçek point-in-time üyelik için tarihsel effective aralıklarını kullanan
ayrı bir resolver/veri seti gerekir; mevcut komut bunu varmış gibi göstermez.

Sektör komutu ayrı `data/symbols/bist_symbols.csv` kaynağında `symbol` ve
`sector_index` sütunlarını, varsa `active` filtresini kullanır. İzleme-listesi
komutu yalnız kullanıcının kayıtlı sembollerini kullanır; boş listeye sembol
uydurmaz.

## Alarm giriş biçimleri

Metin:

```text
ASELS 72.50 üstü
THYAO 285 altı
EREGL;31,20;yukarı_kes
KCHOL 196 aşağı_kes
```

CSV/XLSX önerilen sütunları:

| hisse | fiyat | koşul | mod | tekrar_saniye | not |
|---|---:|---|---|---:|---|
| ASELS | 72,50 | üstü | sürekli | 60 | Direnç alarmı |
| THYAO | 285,00 | altı | tek | 60 | Risk seviyesi |

İçe aktarma iki aşamalıdır: parse/doğrulama önizlemesi ve kullanıcı onayı.
Hatalı, yinelenen veya OCR güveni düşük satırlar otomatik kaydedilmez.

## Lisanslı piyasa REST adaptörü

Üretim seçimi `MARKET_DATA_PROVIDER=licensed_rest` değeridir. Adaptör yerleşiktir;
ayrıca provider kodu yazmak gerekmez. Buna karşılık yazılı veri dağıtım hakkı,
sözleşmeli/gateway taban adresi, gerçek path şablonları ve API anahtarı operatör
tarafından sağlanmalıdır.

Adaptörün fail-closed sözleşmesi:

- `LICENSED_MARKET_DATA_BASE_URL` geçerli HTTPS adresi olmalıdır. Düz HTTP yalnız
  localhost/127.0.0.1 test gateway'i için kabul edilir.
- `LICENSED_MARKET_DATA_API_KEY` boşsa provider kurulmaz.
- `LICENSED_MARKET_DATA_QUOTE_PATH` ve `LICENSED_MARKET_DATA_OHLCV_PATH`
  `{symbol}` alanını içermelidir.
- Quote yanıtı `symbol`, pozitif `price`, saat dilimli `timestamp` ve boolean
  `is_live` vermelidir. `market_open` ve `trade_id` desteklenen ek alanlardır.
- OHLCV yanıtı `timestamp`, `open`, `high`, `low`, `close`, `volume` ve
  `is_complete` alanlarını taşıyan `bars`/`result` listesi olmalıdır.
- Market-state yanıtı saat dilimli `timestamp` ve boolean `is_open` vermelidir;
  eski market-state “açık” kabul edilmez.
- Sembol, timestamp, fiyat veya şema doğrulanamazsa veri kullanıma açılmaz.
- `licensed_rest` hatası Yahoo, cache veya mock kaynağa sessizce düşürülmez.

`/quote/{symbol}`, `/ohlcv/{symbol}` ve `/market-state` yalnız varsayılan gateway
sözleşme örnekleridir. Borsa İstanbul'un herkese açık endpoint'leri oldukları
anlamına gelmez. Path'ler ve dönen JSON, sözleşmeli sağlayıcının veya kurum içi
normalize gateway'in gerçek sözleşmesine göre ayarlanmalıdır. Query parametreleri
OHLCV için `timeframe`, UTC ISO-8601 `start` ve `end` olarak gönderilir.

`LICENSED_MARKET_DATA_PROVIDER_NAME`, rapor ve provenance içinde görünen kaynak
etiketidir; lisanslı şirketin gerçek adını kullanın, fakat bunu güven/kalite
kanıtı saymayın. Kalite yine timestamp, freshness ve içerik kapılarından geçer.

## Veri doğruluğu sözleşmesi

“Tam doğru ve güncel” bir yazılım ayarı değildir. Aşağıdaki zincirin tamamı
sağlanmadan kesin canlı tetik iddiası kurulamaz:

1. Borsa İstanbul verisini dağıtma hakkı bulunan sözleşmeli sağlayıcı.
2. Sembol, gerçek işlem zamanı, alım zamanı, interval, bar-complete, delayed ve
   seans/işlem durumu metadata'sını veren adaptör.
3. UTC iç kayıt ve `Europe/Istanbul` kullanıcı gösterimi.
4. Sağlayıcı timestamp'ine göre freshness sınırı; sunucunun “şimdi” zamanı fiyatın
   zamanı gibi kullanılamaz.
5. Negatif/NaN fiyat, `high < low`, sıra bozukluğu, eksik hacim ve aykırı veri
   için kalite kapısı.
6. Ham, split-adjusted ve total-return serilerin aynı hesapta karıştırılmaması.
7. Tamamlanmamış mumun confirmed breakout, MSS/BOS veya backtest sinyali
   yapılmaması.
8. İki sağlayıcı kullanılacaksa gerçekten bağımsız upstream olması. yfinance ve
   Yahoo Chart aynı Yahoo ailesidir; bağımsız doğrulama sayılmaz.
9. Tavan/taban, suspension, circuit breaker ve geçerli işlem bilgisi yoksa fill
   sonucunun “bilinmiyor/pending” kalması.
10. Her raporda kaynak, fiyat zamanı, çekilme zamanı, kalite ve fallback etiketi.

`ALLOW_DELAYED_DATA_FOR_LIVE_TRIGGER=false` üretim için güvenli varsayılandır.
Lisanslı REST erişimi yapılandırılmadan Yahoo ile sistem gecikmeli analiz sunabilir;
ancak bu gözlem kesin canlı alarm veya gerçekleşme kanıtı olarak kullanılmamalıdır.

Resmî veri dağıtımı ve erişim çerçeveleri:

- [Borsa İstanbul — Data Dissemination](https://www.borsaistanbul.com/en/data/data-dissemination)
- [Fintables — MCP bağlantısı ve OAuth](https://fintables.com/evo/mcp)
- [KAP — REST API entegrasyon bilgisi](https://www.kap.org.tr/tr/api/about/content-file/8a019492945fbe080194b26d8bed4873)

## Temel analiz sağlayıcı politikası

### Fintables MCP

`FUNDAMENTAL_PROVIDER=fintables_mcp`, yalnız yetkili Fintables OAuth oturumu veya
erişim belirteciyle kullanılmalıdır. Adaptör MCP JSON-RPC yanıtını normalize eder;
HTML kazımaz. Fintables üyeliği, veriyi Telegram botunun diğer kullanıcılarına
yeniden dağıtma hakkını kendiliğinden vermez. Çok kullanıcılı/ücretli kullanım
öncesinde yazılı izin ve lisans kapsamı doğrulanmalıdır.

### Lisanslı KAP REST/gateway

`FUNDAMENTAL_PROVIDER=kap_rest` için sözleşmeli taban adresi, API anahtarı,
gerekirse izinli çıkış IP'si ve sağlayıcının gerçek endpoint şeması gerekir.
Uygulamadaki path template, doğrudan KAP sözleşmesindeki uç noktaya veya sizin
normalize eden lisanslı gateway'inize göre ayarlanmalıdır; dokümanda endpoint
uydurulmaz.

### KAP disclosure adaptörü ve `/kap`

KAP temel veri provider'ı ile disclosure provider'ı aynı sözleşmeli taban adresi
ve API anahtarını kullanabilir, ancak yetki ve yeniden dağıtım kapsamları aynı
varsayılmaz. Bot içi disclosure okuma ayrıca `KAP_PROVIDER=kap_rest` ile açılır.
Varsayılan `KAP_PROVIDER=disabled` fail-closed durumudur.
Eski `PUBLIC_DISCLOSURE_PROVIDER` RSS ayarı bu lisanslı KAP adapterından ayrıdır.

Canonical disclosure ayarları:

- `KAP_REST_DISCLOSURES_PATH`: sembole göre bildirim listesi path'i;
- `KAP_REST_DISCLOSURE_DETAIL_PATH`: `{id}` içeren detay path şablonu;
- `KAP_REST_SYMBOL_QUERY_PARAM`: liste isteğinde sembolün gönderileceği query adı.

Bu varsayılan path/query değerleri resmî KAP endpoint garantisi değildir.
Sözleşme veya normalize gateway farklı ad kullanıyorsa aynen ona göre ayarlanır.
Adres HTTPS olmalı, API anahtarı boş olmamalı ve detay path'i `{id}` içermelidir.

`/kap THYAO` davranışı:

1. Provider kapalıysa bildirim uydurmaz; resmî KAP arama bağlantısını gösterir.
2. Provider açıksa doğrulanabilen en yeni 10 kaydı; zaman, başlık, temkinli
   anahtar-kelime sınıfı ve kaynak URL ile listeler.
3. Bağlantı/HTTP/JSON/şema hatasında fail-closed kalır ve resmî aramaya yöneltir.
4. Sonuç boşsa “lisanslı akışta bildirim bulunamadı” der; boşluğu olumlu/olumsuz
   şirket sinyali olarak yorumlamaz.

Bu sürümde otomatik KAP push bildirimi, periyodik disclosure scheduler'ı veya
izleme-listesine kendiliğinden bildirim gönderimi aktif değildir. `/kap`, yalnız
kullanıcı komutuyla çalışan pull sorgusudur. Anahtar-kelime classification alanı
özet kolaylığıdır; doğrulanmış duygu analizi veya yatırım tavsiyesi değildir.

### Kaynak izi ve çapraz kontrol

Her mali kalem mümkün olduğunda şu bilgileri taşır:

- sağlayıcı ve kaynak URL/kimliği;
- dönem ve dönem türü;
- yayımlanma, revizyon ve çekilme zamanı;
- konsolide/solo işareti;
- para birimi, ölçek ve enflasyon düzeltme durumu;
- ham değer ve deterministik hesaplanan oran;
- primary/secondary uyuşmazlık raporu.

F/K, PD/DD, net borç/FAVÖK, marj, büyüme ve benzeri oranlar normalize mali
kalemlerden deterministik hesaplanmalıdır. LLM yalnızca bu doğrulanmış değerleri
Türkçe özetleyebilir; kayıp kalemi tahmin edemez. Strict çapraz kontrolde tolerans
dışı fark, kesin yorum üretimini durdurmalıdır.

## Coolify ortam değişkenleri

Aşağıdaki örnek gerçek bir token veya anahtar içermez. Gizli değerleri Coolify
**Secret** alanında oluşturun; Git'e, build loguna veya ekran görüntüsüne koymayın.
Uygulama varsayılanı geliştirme için `MARKET_DATA_PROVIDER=mock`, KAP için
`KAP_PROVIDER=disabled` değeridir. Production `mock` ayarını reddeder. Aşağıdaki
örnek iki lisanslı REST akışını bilinçli olarak açan production şablonudur; gerçek
sözleşme yoksa ilgili provider'ı açmayın.

```env
APP_ENV=production
LOG_LEVEL=INFO
DATABASE_URL=sqlite:////app/data/mergen_quant.db

TELEGRAM_BOT_TOKEN=<COOLIFY_SECRET>
ADMIN_TELEGRAM_USER_IDS=<TELEGRAM_ID_LISTESI>
TELEGRAM_MODE=polling

# Üretim: sözleşmeli gateway. Boş/yanlış adres veya anahtarda fail-closed.
MARKET_DATA_PROVIDER=licensed_rest
LICENSED_MARKET_DATA_BASE_URL=<LISANSLI_HTTPS_BASE_URL>
LICENSED_MARKET_DATA_API_KEY=<COOLIFY_SECRET>
LICENSED_MARKET_DATA_API_KEY_HEADER=X-API-Key
LICENSED_MARKET_DATA_QUOTE_PATH=/quote/{symbol}
LICENSED_MARKET_DATA_OHLCV_PATH=/ohlcv/{symbol}
LICENSED_MARKET_DATA_MARKET_STATE_PATH=/market-state
LICENSED_MARKET_DATA_PROVIDER_NAME=licensed_rest
LICENSED_MARKET_DATA_TIMEOUT_SECONDS=10
ALLOW_DELAYED_DATA_FOR_LIVE_TRIGGER=false
MAX_MARKET_DATA_STALENESS_SECONDS=30
TIMEZONE_NAME=Europe/Istanbul

USER_PRICE_ALERTS_ENABLED=true
USER_PRICE_ALERT_POLL_SECONDS=30
USER_PRICE_ALERT_DELIVERY_POLL_SECONDS=5
USER_PRICE_ALERT_DEFAULT_REPEAT_SECONDS=60
USER_PRICE_ALERT_MIN_REPEAT_SECONDS=30
USER_PRICE_ALERT_STALE_AFTER_SECONDS=30
USER_PRICE_ALERT_MAX_ACTIVE_PER_USER=500
USER_PRICE_ALERT_MAX_BULK_IMPORT=250
USER_PRICE_ALERT_MAX_DELIVERIES_PER_MINUTE_PER_USER=10
USER_PRICE_ALERT_MAX_GLOBAL_DELIVERIES_PER_MINUTE=500
USER_PRICE_ALERT_AUDIO_ENABLED=true
USER_PRICE_ALERT_OCR_ENABLED=true
USER_PRICE_ALERT_OCR_LANGUAGE=tur+eng
USER_PRICE_ALERT_TEMP_FILE_TTL_MINUTES=30
USER_PRICE_ALERT_MAX_IMAGE_BYTES=10485760
USER_PRICE_ALERT_MAX_FILE_BYTES=10485760

LONG_ONLY=true
SIGNAL_MONITOR_ENABLED=true
SIGNAL_MONITOR_INTERVAL_SECONDS=5
DEFAULT_RISK_PERCENT=1.0
MAX_POSITION_PERCENT=20
MAX_DAILY_VOLUME_PARTICIPATION_PERCENT=1
DEFAULT_TP1_ALLOCATION=40
DEFAULT_TP2_ALLOCATION=35
DEFAULT_TP3_ALLOCATION=25
MOVE_STOP_TO_BREAKEVEN_AFTER_TP1=true
MOVE_STOP_TO_TP1_AFTER_TP2=true
BACKTEST_ENTRY_MODE=next_session_level_touch
BACKTEST_INTRABAR_MODE=lower_timeframe_then_conservative
BACKTEST_FILL_MODEL=conservative_volume_limited
BACKTEST_LIMIT_LOCK_MODE=conservative
BACKTEST_PRICE_MODE=split_adjusted
BACKTEST_COMMISSION_RATE=
BACKTEST_COMMISSION_MINIMUM=
BACKTEST_COMMISSION_TAX_RATE=
BACKTEST_COMMISSION_BPS=0
BACKTEST_SLIPPAGE_BPS=0
BACKTEST_SPREAD_BPS=0
BACKTEST_INCLUDE_DIVIDENDS=true

# Birincil örnek: yetkili Fintables MCP. Secret değeri burada yazılmaz.
FUNDAMENTAL_PROVIDER=fintables_mcp
FUNDAMENTAL_CROSS_CHECK_ENABLED=false
FUNDAMENTAL_CROSS_CHECK_STRICT=true
FUNDAMENTAL_ALLOW_YAHOO_FALLBACK=false
FINTABLES_MCP_URL=https://evo.fintables.com/mcp
FINTABLES_MCP_BEARER_TOKEN=<COOLIFY_SECRET>
FINTABLES_MCP_TOOL_NAME=<YETKILI_TOOL_ADI>
FINTABLES_MCP_SYMBOL_ARGUMENT=symbol

# KAP temel veri/disclosure kullanılıyorsa sözleşmedeki gerçek değerleri girin.
KAP_PROVIDER=kap_rest
KAP_REST_BASE_URL=<LISANSLI_BASE_URL>
KAP_REST_API_KEY=<COOLIFY_SECRET>
KAP_REST_API_KEY_HEADER=X-API-Key
KAP_REST_ENDPOINT_PATH=<SOZLESMELI_PATH_TEMPLATE>
KAP_REST_DISCLOSURES_PATH=/disclosures
KAP_REST_DISCLOSURE_DETAIL_PATH=/disclosureDetail/{id}
KAP_REST_SYMBOL_QUERY_PARAM=symbol
```

Maliyet alanları boş/sıfır bırakılırsa sistem tahminî komisyon uydurmaz; koşu
snapshot'ında sıfır olarak açıkça saklar. Örneğin binde bir gerçek komisyon için
`BACKTEST_COMMISSION_RATE=0.001`, komisyon üzerinden yüzde beş vergi için
`BACKTEST_COMMISSION_TAX_RATE=0.05` girilir. Tamamlanmış kapanışla teyit edilen
kırılım aynı kapanıştan doldurulmaz; teyit olayı saklanır ve ilk sonraki geçerli
gözlemin açılışı kullanılır. Üretim backtestinde `is_complete` alanı zorunludur;
string `false` dâhil belirsiz değerler fail-closed dışarıda bırakılır.

`licensed_rest` adaptörü provider factory'ye kayıtlıdır. Örnekteki quote/OHLCV/
market-state ve KAP path'leri yalnız varsayılan normalize gateway sözleşmesidir;
sözleşmeli kaynağın endpoint/path/query şemasına göre değiştirilmelidir. Base URL
veya anahtar yoksa kurulum fail-closed hata verir; Yahoo/mock'a sessiz geçmez.

KAP sözleşmesi yoksa `KAP_PROVIDER=disabled` kullanın ve KAP secret/path
değerlerini boş bırakın. Bu durumda `/kap SEMBOL` yalnız resmî KAP arama
bağlantısını verir. `KAP_PROVIDER=kap_rest` otomatik push'u açmaz; sadece kullanıcı
komutuyla talep-anı sorguyu etkinleştirir.

Yahoo ile bilinçli gecikmeli geliştirme/analiz gerekiyorsa
`MARKET_DATA_PROVIDER=yfinance` ayrıca seçilebilir. Bu, lisanslı provider için
fallback değildir ve kesin canlı alarm/gerçekleşme kanıtı olarak gösterilemez.

Coolify için ek kurallar:

- SQLite kullanılıyorsa `/app/data` kalıcı volume olmalıdır.
- Polling modunda aynı bot tokenıyla yalnız **bir bot replica** çalıştırın.
- Scheduler işlerindeki duplicate riskini önlemek için tek bot replica kullanın;
  yatay ölçekleme gerekiyorsa dağıtık lock ekleyin.
- API ve bot aynı kalıcı veritabanını görmelidir.
- Lisanslı sağlayıcı IP whitelist istiyorsa Coolify sunucusunun sabit çıkış IP'sini
  sözleşme hesabına tanımlayın.
- Bot tokenı, OAuth tokenı, API anahtarı ve Authorization header loglanmamalıdır.
- `CLOSE_SCAN_ENABLED=false` yalnız kapanış taramasını kapatmalı; fiyat alarmı
  monitor ve delivery işlerinin çalıştığını scheduler health/loglarından doğrulayın.

## OCR ve dosya güvenliği

Docker imajı `tesseract-ocr`, `tesseract-ocr-tur` ve `tesseract-ocr-eng`
paketlerini; Python ortamı Pillow, openpyxl ve pytesseract'ı içerir.

Deployment sonrası kontrol:

```bash
tesseract --version
tesseract --list-langs
python -c "from PIL import Image; import openpyxl, pytesseract; print('OCR/XLSX OK')"
```

`tesseract --list-langs` çıktısında `tur` ve `eng` görünmelidir. OCR akışı:

1. dosya boyutu ve resim formatını doğrular;
2. EXIF yönünü uygular;
3. yerel ön işleme/OCR yapar;
4. güven skorunu kontrol eder;
5. yalnız önizleme üretir;
6. kullanıcı onayından sonra normalize alarm satırlarını kaydeder.

OCR bir doğruluk garantisi değildir. Düşük güven, bozuk sembol, belirsiz ondalık
veya geçersiz koşul varsa fail-closed çalışır. Kullanıcı orijinal metni görüp
düzeltmeden alarm oluşturulmaz.

## Migration ve deployment runbook

### 1. Ön kontrol ve yedek

```bash
python -m alembic current
python -m alembic heads
python -m pytest -q
```

Deployment'tan önce SQLite dosyasının veya haricî veritabanının tutarlı snapshot
yedeğini alın. Çalışan SQLite dosyasını gelişigüzel kopyalamak yerine servisi
durdurun veya veritabanının online backup mekanizmasını kullanın. `.env`, token,
API anahtarı ve gerçek DB dosyasını Git'e eklemeyin.

### 2. İmajı oluştur

```bash
docker build --pull -t montana-melih-hisse-bot:ultra .
docker run --rm --entrypoint sh montana-melih-hisse-bot:ultra -c \
  "tesseract --list-langs && python -c 'from PIL import Image; import openpyxl, pytesseract'"
```

### 3. Migration uygula

```bash
python -m alembic upgrade head
python -m alembic current
```

Alarm/sinyal migration'ları additive olmalıdır: eski kullanıcı, analiz, portföy,
alarm, sinyal ve backtest geçmişini silmez. Migration iki yerde sınanmalıdır:

- boş bir veritabanında `upgrade head`;
- production yedeğinin ayrı bir kopyasında mevcut revision'dan `upgrade head`.

### 4. Servisleri başlat

Docker Compose kullanılıyorsa:

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail=200 api bot
```

Coolify'da önce migration/build başarılı olmalı, sonra API health ve bot polling
logları kontrol edilmelidir. Aynı tokenla eski bot instance'ı çalışıyorsa Telegram
polling çakışır; eski instance'ı kapatmadan yenisini çoğaltmayın.

### 5. Smoke test

1. `/alarm_yardim` ve `/alarm_kur TEST 9.20 üstü` önizlemesini açın.
2. Alarmı onaylayın; listede `ALR-…` referansını görün.
3. Gecikmiş/mock veriyle üretim alarmının tetiklenmediğini doğrulayın.
4. Kontrollü test provider'ıyla fiyatı hedefin altından üstüne geçirip tek trigger
   ve tek teslimat oluştuğunu doğrulayın.
5. `/alarm_ertele`, `/alarm_devam`, `/alarm_durdur`, `/alarm_sil` sahiplik
   davranışlarını iki farklı kullanıcıyla sınayın.
6. 100+ alarmı içe aktarın; aynı sembolün tur başına tek kez çözümlendiğini
   metrik/log üzerinden doğrulayın.
7. CSV, XLSX ve temiz bir ekran görüntüsünde önizleme-onay akışını sınayın.
8. Botu yeniden başlatın; aynı trigger'ın tekrar gönderilmediğini doğrulayın.
9. `CLOSE_SCAN_ENABLED=false` ile alarm monitor/delivery job'larının hâlâ aktif
   olduğunu doğrulayın.
10. Sinyal kabul testinde 45,50 girişin 44,80'de ACTIVE olmadığını ve hedef lotların
    sırasıyla 400/350/250 olduğunu doğrulayın.
11. `licensed_rest` fixture/gateway'iyle quote timestamp, `is_live`, market-state
    ve tamamlanmış/tamamlanmamış OHLCV ayrımını doğrulayın.
12. Lisanslı API anahtarını geçici olarak kaldırıp başlangıcın fail-closed olduğunu;
    Yahoo/mock veriye sessiz geçilmediğini doğrulayın.
13. `KAP_PROVIDER=disabled` iken `/kap THYAO` komutunun yalnız resmî arama linki
    verdiğini; sahte bildirim oluşturmadığını doğrulayın.
14. Sözleşmeli test gateway'iyle `KAP_PROVIDER=kap_rest` açıp sembol query adını,
    yayın zamanını, kaynak URL'yi ve hatalı JSON'da fail-closed mesajını sınayın.
    Bu test otomatik push beklememelidir; böyle bir scheduler aktif değildir.
15. `/takip ID` sonrası yeni kullanıcı kaydının `PENDING_ENTRY` olduğunu ve giriş
    seviyesine ulaşmadan `ACTIVE` olmadığını doğrulayın; `/takip_birak` sonrası
    kayıt/event geçmişinin korunduğunu kontrol edin.
16. Timestamp, `is_live`, `is_fresh`, `valid_transaction`, `trading_state` ve
    işlem miktarını sırayla kaldırarak her quote'un fail-closed reddedildiğini;
    hiçbir giriş/TP/stop/manuel kapanış event'i oluşmadığını doğrulayın.
17. PENDING_ENTRY planın timeframe deadline'ı sonrası tam bir `SIGNAL_EXPIRED`
    event/outbox kaydı üretip final duruma geçtiğini ve restart sonrası bildirimi
    çoğaltmadığını doğrulayın. Testi piyasa kapalı ve `/takip_birak` sonrası da
    tekrarlayın; `SIGNAL_MONITOR_ENABLED=false` iken scheduler çalışmamalıdır.
18. `bist_index_membership.csv` yok, güncellik sütunsuz, eski `as_of`, yanlış tarih
    ve 29/49/99 üyeli varyantlarda BIST evren komutlarının başlamadığını sınayın.
19. Tam 30/50/100 aktif ve güncel satırla ilgili komutların arka plan Run ID
    ürettiğini; current-snapshot survivorship uyarısının config/raporda kaldığını
    doğrulayın.
20. İki ayrı DB session/worker ile aynı due alarm delivery ID'sini claim etmeyi
    deneyin; yalnız birinin `SENDING` yapabildiğini ve `attempt_count` değerini
    bir artırdığını doğrulayın. 5 dakikadan eski stuck claim'in retry'a döndüğünü
    ayrıca sınayın.
21. Genel `daily_price_limits` çağrısının açık `limit_percent` olmadan
    `MarketRuleError` verdiğini; enstrümana özel yüzde verildiğinde tick'e
    yuvarlanan alt/üst limitlerin ham bandın dışına taşmadığını doğrulayın.
22. Aynı `MarketSessionEvent.unique_dedup_key` ile ikinci insert'in reddedildiğini
    doğrulayın. Bu test otomatik seans-feed ingest'i beklememelidir; worker bağlı
    değildir.
23. Split/bedelsiz ve reverse-split fixture'larında fiyat/lot ekonomik değerinin,
    mevcut signal state'inin ve gerçekleşmiş PnL'nin korunduğunu; retry sonrası
    yalnız bir `CORPORATE_ACTION_APPLIED` oluştuğunu sınayın. Tam lot üretmeyen
    reverse split reddedilmeli; test otomatik lisanslı feed hook'u varsaymamalıdır.
24. `ATR_TRAILING` ve `STRUCTURE_TRAILING` enum'larının tek başına stop
    hareketi üretmediğini; yalnız TP1→breakeven, TP2→TP1 stop ve `/stop_girise`
    yollarının uygulandığını doğrulayın.
25. `/backtest THYAO 1g 5y` ile legacy
    `/backtest THYAO 2024-01-01 2026-01-01` biçimlerinin ikisinin de parse
    edildiğini; ilkinde `1d`, ikincisinde varsayılan timeframe'in koşuya
    yazıldığını kontrol edin.
26. `/backtest_signal ID` için sahiplik sınırını, özgün `stop_price` başlangıcını,
    üretim `Signal`/event satırlarının değişmediğini, stop-first sonucu ve gerçek
    provider/fallback etiketini doğrulayın. Filtre sonrası uygun tamamlanmış mum
    yokken geçmiş deadline'ın `EXPIRED`, deadline yoksa dönem-sonu unfilled
    raporladığını sınayın.

### 6. Sağlık ve operasyon takibi

Takip edilmesi gereken minimum göstergeler:

- provider son başarı zamanı, hata oranı ve circuit-breaker durumu;
- fiyat veri yaşı ve gecikmeli/fallback oranı;
- lisanslı quote/OHLCV/market-state endpoint 401/403, şema ve timestamp hataları;
- kullanıcı isteğiyle yapılan `/kap` sorgularının başarı/hata oranı; otomatik KAP
  push metriği beklenmez çünkü bu sürümde push scheduler'ı yoktur;
- alarm monitor tur süresi, sembol sayısı, değerlendirilen alarm sayısı;
- pending/retry/failed teslimat kuyruğu;
- kullanıcı/global rate-limit nedeniyle ertelenen teslimat;
- OCR hata ve düşük güven oranı;
- scheduler job `next_run_time`, overlap/misfire sayısı;
- DB boyutu, kilitlenme ve migration revision;
- lisans/OAuth süresi dolma ve 401/403 cevapları.

## Rollback

Şema ve uygulama sürümünü birlikte geri alın:

1. Yeni deployment'ı durdurun; aynı DB'ye eski ve yeni kodun eşzamanlı yazmasını
   engelleyin.
2. Uygulamayı önceki doğrulanmış imaja alın.
3. En güvenli yöntem, deployment öncesi DB snapshot'ını geri yüklemektir.
4. `alembic downgrade -1` yalnız ilgili downgrade production kopyasında ayrıca
   sınandıysa kullanılmalıdır. Tablo/kolon silen downgrade veri kaybettirebilir.
5. Eski uygulama yeni additive şemayla uyumluysa kolonları silmek yerine yerinde
   bırakmak tercih edilir.
6. `/app/data` volume'unu silmeyin ve `docker compose down -v` çalıştırmayın.
7. Geri dönüşten sonra `alembic current`, API health, bot polling, alarm listesi
   ve outbox tekrarlarını kontrol edin.

Rollback sırasında gönderilmemiş outbox kayıtlarını körlemesine silmeyin.
Önce olay dedup anahtarlarını koruyup, eski sürümün bu kayıtları anlayıp
anlamadığını doğrulayın.

## Doğruluk iddiasının sınırları

- Kod, gelen verinin Borsa'dan doğru üretildiğini matematiksel olarak kanıtlayamaz;
  kaynak sözleşmesi/SLA, timestamp ve çapraz kontrol gerekir.
- `licensed_rest` adaptörünün çalışması tek başına veri lisansı veya yeniden
  dağıtım hakkını kanıtlamaz; bu hak operatör–sağlayıcı sözleşmesinden gelir.
- Yahoo/yfinance verisi resmî BIST tick verisi değildir ve “canlı” etiketiyle
  sunulmamalıdır.
- Günlük OHLC, aynı mum içinde stop ile hedefin kronolojik sırasını tek başına
  göstermez. Alt zaman dilimi yoksa muhafazakâr sonuç kullanılır.
- Tarihsel BIST30/50/100 üyeliği sağlanmazsa evren backtest'i survivorship bias
  içerir ve raporda açıkça yazılmalıdır.
- Tarihsel suspension, tavan/taban emir defteri ve kurumsal işlem metadata'sı
  yoksa gerçekleşme kesinliği düşer; motor kârlı fill uydurmamalıdır.
- Günlük tavan/taban yüzdesi enstrüman, pazar ve işlem yöntemine göre açıkça
  sağlanmalıdır; generic servis evrensel `%10` varsaymaz.
- `MarketSessionEvent` tablosu ve `apply_corporate_action_adjustment` runtime
  metodu lisanslı normalize feed ingestor'u değildir. Otomatik seans/sermaye
  işlemi worker'ı bağlı olmadığı için bu özellikler canlıda kendiliğinden
  çalışıyormuş gibi gösterilmemelidir.
- `ATR_TRAILING`, `STRUCTURE_TRAILING` ve `MANUAL_TRAILING` bu sürümde enum
  seviyesindedir; ATR/yapı tabanlı otomatik stop hesabı uygulanmamıştır.
- Fintables veya KAP erişimi, içeriğin tüm bot kullanıcılarına yeniden dağıtım
  izni anlamına gelmez.
- `/kap` talep-anı okumadır. Otomatik KAP push bildirimi/scheduler'ı bu sürümün
  özelliği değildir ve doküman/arayüzde varmış gibi gösterilmemelidir.
- Temel veride konsolide/solo, dönem, para birimi, ölçek veya revizyon belirsizse
  kıyas/yorum kesin sonuç vermemelidir.
- Sesli Telegram bildirimi, cihazın sessiz modunu aşamaz.
- Alarm ve analiz sonuçları yatırım tavsiyesi veya getiri garantisi değildir.

Production kabulü ancak testler, migration kopyası, lisanslı veri kaynağı,
credential rotasyonu, tekil scheduler/polling topolojisi ve yukarıdaki smoke
senaryoları birlikte doğrulandıktan sonra verilmelidir.
