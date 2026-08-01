from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> str:
    return """<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MONTANA FİNANS ROBOTU HİSSE BOT | BIST Kontrol Merkezi</title>
<style>
:root{--bg:#061019;--card:#0c1925;--line:#1e3343;--text:#e6f1f8;--muted:#86a0b2;--cyan:#00c8ff;--green:#00e5a8;--red:#ff3b69;--amber:#ffd43b}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 85% 0,#0b2940 0,transparent 32%),var(--bg);color:var(--text);font:15px Inter,Segoe UI,sans-serif}
.wrap{max-width:1240px;margin:auto;padding:28px}.top{display:flex;justify-content:space-between;gap:20px;align-items:center}.brand{font-size:26px;font-weight:900;letter-spacing:.4px}.brand b{color:var(--cyan)}
.live{color:var(--green);border:1px solid #14644e;background:#09261f;padding:8px 12px;border-radius:999px}.hero{margin:24px 0;padding:26px;border:1px solid var(--line);border-radius:20px;background:linear-gradient(120deg,#0d1d2a,#0a1520)}
.hero h1{font-size:36px;margin:0 0 10px}.hero p{color:var(--muted);max-width:780px;line-height:1.6}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}.card{grid-column:span 4;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px}.wide{grid-column:span 8}.full{grid-column:1/-1}
.kpi{font-size:28px;font-weight:800;margin:8px 0}.cyan{color:var(--cyan)}.green{color:var(--green)}.red{color:var(--red)}.muted{color:var(--muted)}h2{font-size:18px;margin:0 0 14px}h3{font-size:14px;margin:18px 0 8px;color:var(--cyan)}
.flow{display:grid;grid-template-columns:repeat(7,1fr);gap:8px}.step{padding:13px;border:1px solid var(--line);border-radius:12px;text-align:center;background:#091722}.step b{display:block;color:var(--cyan);margin-bottom:5px}.cmd{display:flex;justify-content:space-between;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}code{color:var(--green);background:#07121b;padding:4px 7px;border-radius:6px}.tag{font-size:12px;color:var(--amber)}
.meter{height:9px;background:#07121b;border-radius:20px;overflow:hidden;margin:7px 0 13px}.meter i{display:block;height:100%;background:linear-gradient(90deg,var(--cyan),var(--green));border-radius:20px}.pill{display:inline-block;padding:5px 9px;border-radius:999px;background:#0a2530;color:var(--cyan);margin:3px;font-size:12px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:11px;border-bottom:1px solid var(--line)}th{color:var(--muted)}.note{border-left:3px solid var(--amber);padding:10px 13px;background:#251f0b;color:#f7e7a1;border-radius:6px}
@media(max-width:850px){.card,.wide{grid-column:1/-1}.flow{grid-template-columns:1fr}.hero h1{font-size:28px}.top{align-items:flex-start;flex-direction:column}}
</style></head><body><main class="wrap">
<div class="top"><div class="brand">🏔️ MONTANA FİNANS ROBOTU <b>HİSSE BOT</b> 📈</div><div class="live">● Sistem çevrimiçi</div></div>
<section class="hero"><div class="tag">BIST TEKNİK • TEMEL • ALARM KONTROL MERKEZİ</div><h1>Karışık sinyal değil; net plan, görünür kanıt.</h1><p>Her işlem planı trend, momentum, hacim, piyasa yapısı ve risk/getiri üzerinden puanlanır. Puan olasılık değildir; hangi kanıtın kaç puan verdiği kullanıcıya açıkça gösterilir.</p><span class="pill">FVG</span><span class="pill">Order Block</span><span class="pill">BOS / MSS</span><span class="pill">TP1–TP5</span><span class="pill">KAP / Fintables</span></section>
<section class="grid">
 <article class="card"><div class="muted">Piyasa</div><div class="kpi cyan">Borsa İstanbul</div><span class="muted">Lisanslı canlı veri veya açıkça işaretli gecikmeli analiz</span></article>
 <article class="card"><div class="muted">İşlem kalitesi</div><div class="kpi green">0–100 Kanıt</div><span class="muted">Trend + momentum + yapı + hacim + R/R</span></article>
 <article class="card"><div class="muted">Güvenlik</div><div class="kpi red">Tetik Zorunlu</div><span class="muted">Yetersiz yön farkında BEKLE/PAS GEÇ</span></article>
 <article class="card full"><h2>Analiz nasıl oluşur?</h2><div class="flow">
  <div class="step"><b>1. Veri</b>Tamamlanmış mumlar</div><div class="step"><b>2. Trend</b>EMA20/50/200 + ADX</div><div class="step"><b>3. Yapı</b>S/R + FVG/OB + BOS/MSS</div><div class="step"><b>4. Hacim</b>Göreli hacim + OBV</div><div class="step"><b>5. Giriş</b>Çakışan fiyat bölgesi</div><div class="step"><b>6. Risk</b>ATR stop + gerçek R/R</div><div class="step"><b>7. Karar</b>Tetikle veya bekle</div>
 </div></article>
 <article class="card wide"><h2>Telegram komut rehberi</h2>
  <div class="cmd"><code>/analiz THYAO</code><span>Teknik görünüm ve çoklu zaman özeti</span></div><div class="cmd"><code>/islemplani THYAO</code><span>Kanıt puanlı giriş, stop, TP1–TP5</span></div><div class="cmd"><code>/sirket THYAO</code><span>Bilanço, borç, nakit, kârlılık ve kaynak</span></div><div class="cmd"><code>/aktif_sinyaller</code><span>Okunabilir durum kartları</span></div><div class="cmd"><code>/alarm 9.20 THYAO</code><span>Basit hedef fiyat alarmı</span></div><div class="cmd"><code>/alarm_test radar</code><span>Alarm sesini dene</span></div><div class="cmd"><code>/komutlar</code><span>Tüm özelliklerin öğretici rehberi</span></div>
 </article>
 <article class="card"><h2>Kalite puanı nasıl oluşur?</h2><span class="muted">Trend • 25</span><div class="meter"><i style="width:25%"></i></div><span class="muted">Momentum • 20</span><div class="meter"><i style="width:20%"></i></div><span class="muted">Yapı • 20</span><div class="meter"><i style="width:20%"></i></div><span class="muted">Hacim • 15</span><div class="meter"><i style="width:15%"></i></div><span class="muted">Risk/Getiri • 20</span><div class="meter"><i style="width:20%"></i></div><p class="note">68 altı veya iki yön arasında yeterli fark yoksa sistem işlem zorlamaz.</p></article>
 <article class="card full"><h2>Kavramları öğren</h2><div class="grid"><div class="card"><h3>ATR nedir?</h3><p class="muted">Hissenin ortalama hareket genişliğidir; stop mesafesinin oynaklığa uyarlanmasını sağlar.</p></div><div class="card"><h3>R katsayısı nedir?</h3><p class="muted">Giriş ile stop arasındaki mesafe 1R'dir. 2R hedef, riskin iki katı brüt potansiyeli anlatır.</p></div><div class="card"><h3>Tetik nedir?</h3><p class="muted">Giriş bölgesi görüldükten sonra istenen tamamlanmış kapanış ve hacim şartıdır.</p></div></div></article>
 <article class="card full"><h2>Hangi kod ne işe yarıyor?</h2><table><thead><tr><th>Dosya</th><th>Görev</th><th>Ürettiği çıktı</th></tr></thead><tbody>
  <tr><td><code>analysis/bist_trade_plan.py</code></td><td>Yön, giriş, stop ve hedef matematiği</td><td>Long/short plan nesnesi</td></tr>
  <tr><td><code>analysis/support_resistance_engine.py</code></td><td>Swing ve hacim profiliyle seviyeleri bulur</td><td>Destek/direnç kümeleri</td></tr>
  <tr><td><code>services/chart_service.py</code></td><td>Mumları ve işlem seviyelerini çizer</td><td>Telegram PNG grafiği</td></tr>
  <tr><td><code>telegram/handlers_v3.py</code></td><td>Komutları karşılar ve servisleri çağırır</td><td>Telegram yanıt akışı</td></tr>
  <tr><td><code>data/provider_factory.py</code></td><td>Veri sağlayıcısını seçer ve yedekler</td><td>Standart OHLCV verisi</td></tr>
  <tr><td><code>fundamentals/</code></td><td>Fintables/KAP/Yahoo verisini doğrular ve oranlar</td><td>Kaynaklı temel analiz</td></tr>
  <tr><td><code>alerts/</code></td><td>Fiyat koşulu, tekrar, ses ve teslim kaydı</td><td>Kalıcı alarm bildirimi</td></tr>
 </tbody></table></article>
 <article class="card full"><div class="note">⚠️ Short senaryosu spot BIST’te her payda uygulanamaz. Güncel açığa satış listesi, ödünç pay ve VİOP uygunluğu ayrıca kontrol edilmelidir. Sistem yatırım tavsiyesi üretmez.</div></article>
</section></main></body></html>"""
