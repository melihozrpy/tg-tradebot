# MONTANA TERMINAL — Grafik Üretim Promptu

Bu şartname tüm mum grafikleri, çoklu-zaman grafikleri, OB/FVG/BOS/MSS
overlay'leri, senaryo görselleri, rapor grafikleri, equity curve ve haftalık
aday kartları için zorunludur. Her çıktı Telegram'a tek fotoğraf olarak
gönderilebilen tek PNG/tek frame olmalıdır.

## Çıktı ve render

- Nihai dosya tam `1600×900` PNG'dir.
- Önce `3200×1800` çiz, antialias/LANCZOS ile `1600×900`e indir.
- Bozuk veya çakışan grafik gönderilmez. Sıra: ana render, sade mum+seviye
  fallback'i, yalnız metin fallback'i.
- NaN/None/inf, tutarsız OHLC ve görünür fiyat aralığı dışında ölçek üreten
  seviyeler kontrol edilir.
- Fiyat/zone/stop/hedef eksikse süs amaçlı çizilmez.

## Renk paleti

| Alan | Renk |
| --- | --- |
| Dış arka plan | `#0A0D13` |
| Panel/kart | `#10141C` |
| İkincil panel | `#161B25` |
| Hairline border | `#232936` |
| Soluk grid | `#1A2029` |
| Ana metin | `#EAEDF3` |
| İkincil metin | `#8B93A3` |
| Eksen metni | `#565F70` |
| Yükseliş/talep | `#2FD8A3` |
| Düşüş/arz | `#FF5C72` |
| OB/FVG vurgu | `#E3B04D` |
| İkincil vurgu | `#8B93FF` |
| Entry | `#5B9DFF` |

Saf siyah, saf beyaz, neon, parlak kırmızı/yeşil, gradient ve glow yasaktır.
Yalnız çok hafif (%2-3) ton farkı kabul edilir; görünüm flat, net ve canlıdır.

## Tipografi

- Sembol başlığı: Inter Bold veya geometrik sans fallback.
- Fiyat, tarih, yüzde, kod ve eksenler: IBM Plex Mono/JetBrains Mono veya
  DejaVu Sans Mono fallback; tabular rakam.
- Gövde metni: Inter/sistem sans.
- Grafik içine emoji çizilmez; gerekiyorsa vektör şekil kullanılır.

## Yerleşim

```text
[ÜST BAR ~80px]  KOD · şirket kısa adı · BIST · TF · tarih
                 sağda CANLI/KAPANIŞ rozeti · büyük fiyat · % değişim · veri zamanı
[ANA ALAN]       sol: mumlar + iki EMA + zona/seviye
                 fiyat ekseni
                 sağ: 282px zone rail
[ALT PLOT]       ince hacim paneli (yaklaşık %14)
[ALT ŞERİT]      piyasa güveni · trend · son MSS · aktif bölge sayısı
[İMZA]           MONTANA FİNANS ROBOTU · SMXM Analiz Sistemi
```

- Günlükte son 120 mum ve son mum sonrası yaklaşık sekiz mum boşluk bırak.
- Ana alan yalnız mumlar, EMA20 (altın), EMA50 (mor), aktif bölgeler ve
  entry/stop/hedefleri gösterir.
- Hacim ana fiyat alanına asla karışmaz. Ayrı alt panelde mum rengiyle %45
  opak barlar, ince Ort20 çizgisi; RVOL ≥1,5 barlarında altın kenar bulunur.
- Güncel fiyat son bara ince kesikli çizgiyle bağlanır ve fiyat ekseninde dolu
  rozet olarak görünür.
- Arka planda en fazla %3-4 opaklıkta filigran kullanılabilir.

## Mumlar, eksenler ve seviyeler

- Yükselen mum/fitil `#2FD8A3`, düşen mum/fitil `#FF5C72`; gövde opak, fitil
  yaklaşık 1,5px, gövde genişliği mum aralığının %70'i, doji en az 1px.
- Grid çok ince/soluk; pazartesiler ince dikey ayraç, ay değişiminde belirgin
  ay etiketi kullanılır.
- Görünen mumların min/max değerine %6 boşluk eklenir. Uzak hedefler ölçeği
  bozmaz; kenarda oklu chip ile gösterilir.
- Hisse/endeks 2, parite 4-5 ondalık gösterilir ve BIST fiyat adımına uyulur.
- Entry mavi düz çizgi, stop kırmızı kesikli, hedefler yeşil kesikli çizgidir.
  Sağdaki değer etiketleri çakışma çözümüyle dikey ayrılır.
- Entry hiçbir zaman güncel kapanıştan kopyalanmaz; yapısal bölgeden hesaplanır.
- Varsayımsal senaryo yolu gerçek mum gibi değil, kesikli projeksiyon olarak
  çizilir.

## Zone rail — çakışma sıfır

1. OB/FVG/arz/talep bandının üstüne metin yazılmaz. Plot içinde yalnız band:
   OB yaklaşık `0.10`, FVG `0.08`, en fazla `0.12` opaklık.
2. Aktif görünüm en çok iki OB ve iki FVG'dir. Mitigate olmuş/geçersiz bölge
   çizilmez.
3. Her bölgenin tüm metni sağdaki zone rail kartındadır: tip, alt-üst fiyat,
   mevcut fiyata uzaklık ve `AKTİF/TEST EDİLDİ/BEKLENİYOR` durum chip'i.
4. Kartlar ait olduğu banda %40-50 opak, ince noktalı leader line ile bağlanır.
5. Kartlar yakınlığa göre değil fiyat eksenindeki Y sırasına göre dizilir.
   Collision-sweep algoritması kartlar arasında en az 10px bırakır; taşarsa
   bütün blok plot sınırı içinde yukarı/aşağı kaydırılır.
6. Aynı fiyatta OB ve FVG varsa ayrı bant ve ayrı kart gösterilir; tek kutuda
   birleştirilmez.

## Yapısal işaretler ve alt şerit

- BOS/MSS/CHoCH mum gövdesi içinde değil, ilgili mumun üst/alt kenarında kısa
  pill olarak çizilir: `▲ BOS`, `▼ MSS`. İki işaret arasında en az 40px vardır.
- Plan grafiklerinde giriş-hedef alanı yeşil %8, giriş-stop alanı kırmızı %8
  dolgu olabilir; ortada R/R bulunur.
- Alt şerit yalnız şu yapıdadır: `Piyasa güveni 62/100 · Trend yükseliş · Son
  MSS bullish · 3 aktif bölge`.
- Güven çubuğu 0-33 kırmızı, 34-66 altın/turuncu, 67-100 teal; skor beyaz ve
  kalın kalır.

## Haftalık aday kartı

- Başlık: `HAFTANIN ADAYLARI · {hafta aralığı}` ve XU100 durum rozeti.
- En fazla iki eşit kart olur. Tek adayda kart ortalanır; aday yoksa görsel
  üretme, yalnız kısa metin gönder.
- Kartta KOD/sektör, uyum skoru, son 40 gün mini mum grafiği, alım bandı, stop,
  H1/H2, R/R, geçerlilik ve olay riski bulunur.
- Alt not: alım bölgesine dönüşte kapanış+hacim teyidi gerekir; güncel fiyat
  otomatik giriş değildir.
