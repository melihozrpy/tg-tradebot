from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> str:
    return """<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MERGEN QUANT | BIST Kontrol Merkezi</title>
<style>
:root{--bg:#061019;--card:#0c1925;--line:#1e3343;--text:#e6f1f8;--muted:#86a0b2;--cyan:#00c8ff;--green:#00e5a8;--red:#ff3b69;--amber:#ffd43b}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 85% 0,#0b2940 0,transparent 32%),var(--bg);color:var(--text);font:15px Inter,Segoe UI,sans-serif}
.wrap{max-width:1240px;margin:auto;padding:28px}.top{display:flex;justify-content:space-between;gap:20px;align-items:center}.brand{font-size:26px;font-weight:800;letter-spacing:.4px}.brand b{color:var(--cyan)}
.live{color:var(--green);border:1px solid #14644e;background:#09261f;padding:8px 12px;border-radius:999px}.hero{margin:24px 0;padding:26px;border:1px solid var(--line);border-radius:20px;background:linear-gradient(120deg,#0d1d2a,#0a1520)}
.hero h1{font-size:36px;margin:0 0 10px}.hero p{color:var(--muted);max-width:780px;line-height:1.6}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}.card{grid-column:span 4;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px}.wide{grid-column:span 8}.full{grid-column:1/-1}
.kpi{font-size:28px;font-weight:800;margin:8px 0}.cyan{color:var(--cyan)}.green{color:var(--green)}.red{color:var(--red)}.muted{color:var(--muted)}h2{font-size:18px;margin:0 0 14px}h3{font-size:14px;margin:18px 0 8px;color:var(--cyan)}
.flow{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}.step{padding:13px;border:1px solid var(--line);border-radius:12px;text-align:center}.step b{display:block;color:var(--cyan);margin-bottom:5px}.cmd{display:flex;justify-content:space-between;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}code{color:var(--green);background:#07121b;padding:4px 7px;border-radius:6px}.tag{font-size:12px;color:var(--amber)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:11px;border-bottom:1px solid var(--line)}th{color:var(--muted)}.note{border-left:3px solid var(--amber);padding:10px 13px;background:#251f0b;color:#f7e7a1;border-radius:6px}
@media(max-width:850px){.card,.wide{grid-column:1/-1}.flow{grid-template-columns:1fr}.hero h1{font-size:28px}.top{align-items:flex-start;flex-direction:column}}
</style></head><body><main class="wrap">
<div class="top"><div class="brand">🏹 MERGEN <b>QUANT</b></div><div class="live">● Sistem çalışıyor</div></div>
<section class="hero"><div class="tag">BIST ANALİZ VE RİSK PLATFORMU</div><h1>Karışık sinyal değil, okunabilir işlem planı.</h1><p>Destek/direnç, trend, momentum ve volatiliteyi tek akışta birleştirir. Tüm seviyeler kapanmış piyasa verisinden kural tabanlı üretilir.</p></section>
<section class="grid">
 <article class="card"><div class="muted">Veri pazarı</div><div class="kpi cyan">Borsa İstanbul</div><span class="muted">Yahoo / CSV sağlayıcı katmanı</span></article>
 <article class="card"><div class="muted">Plan yapısı</div><div class="kpi green">Long + Short</div><span class="muted">TP1–TP5 ve üç stop seçeneği</span></article>
 <article class="card"><div class="muted">Risk yaklaşımı</div><div class="kpi red">Tetik zorunlu</div><span class="muted">Tetik gelmeden senaryo aktif olmaz</span></article>
 <article class="card full"><h2>Analiz nasıl oluşur?</h2><div class="flow">
  <div class="step"><b>1. Veri</b>OHLC mumları alınır</div><div class="step"><b>2. Yapı</b>Destek ve direnç bulunur</div><div class="step"><b>3. Yön</b>EMA, RSI, MACD puanlanır</div><div class="step"><b>4. Risk</b>ATR ile stop mesafesi kurulur</div><div class="step"><b>5. Plan</b>Giriş ve TP1–TP5 üretilir</div>
 </div></article>
 <article class="card wide"><h2>Telegram komut rehberi</h2>
  <div class="cmd"><code>/analiz THYAO</code><span>Kısa teknik özet</span></div><div class="cmd"><code>/islemplani THYAO</code><span>Giriş, stop, TP1–TP5 ve grafik</span></div><div class="cmd"><code>/seviyeler THYAO</code><span>Çok zamanlı destek/direnç</span></div><div class="cmd"><code>/cokluzaman THYAO</code><span>Zaman dilimi uyumu</span></div><div class="cmd"><code>/tara</code><span>İzleme listesini tarar</span></div><div class="cmd"><code>/portfoy</code><span>Pozisyon ve risk özeti</span></div>
 </article>
 <article class="card"><h2>Kavramları öğren</h2><h3>ATR nedir?</h3><p class="muted">Fiyatın ortalama hareket genişliğidir. ATR yükseldikçe stop mesafesi doğal olarak genişler.</p><h3>R katsayısı nedir?</h3><p class="muted">Giriş ile stop arasındaki risk 1R kabul edilir. 2R hedef, alınan riskin iki katı potansiyel anlamına gelir.</p><h3>Tetik nedir?</h3><p class="muted">Planı aktif eden kapanış ve hacim şartıdır; tek başına seviyeye dokunmak yeterli değildir.</p></article>
 <article class="card full"><h2>Hangi kod ne işe yarıyor?</h2><table><thead><tr><th>Dosya</th><th>Görev</th><th>Ürettiği çıktı</th></tr></thead><tbody>
  <tr><td><code>analysis/bist_trade_plan.py</code></td><td>Yön, giriş, stop ve hedef matematiği</td><td>Long/short plan nesnesi</td></tr>
  <tr><td><code>analysis/support_resistance_engine.py</code></td><td>Swing ve hacim profiliyle seviyeleri bulur</td><td>Destek/direnç kümeleri</td></tr>
  <tr><td><code>services/chart_service.py</code></td><td>Mumları ve işlem seviyelerini çizer</td><td>Telegram PNG grafiği</td></tr>
  <tr><td><code>telegram/handlers_v3.py</code></td><td>Komutları karşılar ve servisleri çağırır</td><td>Telegram yanıt akışı</td></tr>
  <tr><td><code>data/provider_factory.py</code></td><td>Veri sağlayıcısını seçer ve yedekler</td><td>Standart OHLCV verisi</td></tr>
 </tbody></table></article>
 <article class="card full"><div class="note">⚠️ Short senaryosu spot BIST’te her payda uygulanamaz. Güncel açığa satış listesi, ödünç pay ve VİOP uygunluğu ayrıca kontrol edilmelidir. Sistem yatırım tavsiyesi üretmez.</div></article>
</section></main></body></html>"""
