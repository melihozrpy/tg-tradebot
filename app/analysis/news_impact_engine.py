from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# V3.2 (Asama 4, bolum 2): Kurallı ve açıklanabilir haber etkisi motoru.
#
# Hicbir ML/kara kutu YOKTUR. Her haber sabit anahtar kelime kurallariyla bir
# kategoriye atanir, ardindan kategoriye ozgu temel etki puani; kaynak guveni,
# sirket eslesme guveni ve haber yasina gore ayarlanir. Haber bulunmamasi
# ASLA negatif puan olusturmaz (bu motor haber YOKSA hic cagrilmaz / None doner).
# ---------------------------------------------------------------------------

CATEGORY_FINANCIAL_RESULT = "finansal_sonuc"
CATEGORY_NEW_BUSINESS = "yeni_is_ihale"
CATEGORY_INVESTMENT = "yatirim_kapasite"
CATEGORY_PARTNERSHIP_MA = "ortaklik_satinalma"
CATEGORY_DEBT_FINANCING = "borc_finansman"
CATEGORY_DIVIDEND = "temettu"
CATEGORY_CAPITAL_INCREASE = "sermaye_artirimi"
CATEGORY_SHAREHOLDER_SALE = "ortak_satisi"
CATEGORY_LAWSUIT_PENALTY = "dava_ceza"
CATEGORY_OPERATIONS_HALT = "faaliyet_durmasi"
CATEGORY_MANAGEMENT_CHANGE = "yonetim_degisikligi"
CATEGORY_SECTOR_NEWS = "sektor_haberi"
CATEGORY_UNCERTAIN = "belirsiz"

CATEGORY_LABELS_TR = {
    CATEGORY_FINANCIAL_RESULT: "Finansal sonuç",
    CATEGORY_NEW_BUSINESS: "Yeni iş / ihale",
    CATEGORY_INVESTMENT: "Yatırım / kapasite artışı",
    CATEGORY_PARTNERSHIP_MA: "Ortaklık / satın alma",
    CATEGORY_DEBT_FINANCING: "Borç / finansman",
    CATEGORY_DIVIDEND: "Temettü",
    CATEGORY_CAPITAL_INCREASE: "Sermaye artırımı",
    CATEGORY_SHAREHOLDER_SALE: "Ortak satışı",
    CATEGORY_LAWSUIT_PENALTY: "Dava / ceza",
    CATEGORY_OPERATIONS_HALT: "Faaliyet durması",
    CATEGORY_MANAGEMENT_CHANGE: "Yönetim değişikliği",
    CATEGORY_SECTOR_NEWS: "Sektör haberi",
    CATEGORY_UNCERTAIN: "Belirsiz",
}

# Kategori -> (anahtar kelimeler, temel etki puani -100..+100).
# Kelime listeleri Turkce/Ingilizce, kucuk harfe cevrilerek eslestirilir.
_CATEGORY_RULES: list[tuple[str, list[str], float]] = [
    (CATEGORY_LAWSUIT_PENALTY, ["dava", "ceza", "kovusturma", "lawsuit", "fine", "sorusturma", "soruşturma"], -40.0),
    (CATEGORY_OPERATIONS_HALT, ["faaliyet durdu", "üretim durdu", "uretim durdu", "kapatildi", "kapatıldı", "halt", "suspend", "grev", "yangin", "yangın"], -55.0),
    (CATEGORY_SHAREHOLDER_SALE, ["hissesini sattı", "hissesini satti", "pay satisi", "pay satışı", "ortak satışı", "insider sale"], -20.0),
    (CATEGORY_CAPITAL_INCREASE, ["sermaye artirimi", "sermaye artırımı", "bedelli", "rights issue", "capital increase"], -10.0),
    (CATEGORY_DEBT_FINANCING, ["kredi", "tahvil", "borclanma", "borçlanma", "bond", "loan", "financing", "refinans"], 5.0),
    (CATEGORY_DIVIDEND, ["temettu", "temettü", "kar payi", "kâr payı", "dividend"], 25.0),
    (CATEGORY_PARTNERSHIP_MA, ["satin alma", "satın alma", "birlesme", "birleşme", "ortaklik", "ortaklık", "acquisition", "merger", "partnership", "joint venture"], 30.0),
    (CATEGORY_INVESTMENT, ["yatirim", "yatırım", "kapasite artisi", "kapasite artışı", "yeni fabrika", "investment", "expansion"], 25.0),
    (CATEGORY_NEW_BUSINESS, ["ihale", "yeni siparis", "yeni sipariş", "yeni sozlesme", "yeni sözleşme", "tender", "new contract", "new order"], 30.0),
    (CATEGORY_MANAGEMENT_CHANGE, ["genel mudur", "genel müdür", "yonetim kurulu", "yönetim kurulu", "istifa", "ceo", "atandi", "atandı", "resign"], 0.0),
    (CATEGORY_FINANCIAL_RESULT, ["net kar", "net kâr", "net zarar", "bilanco", "bilanço", "finansal sonuc", "finansal sonuç", "earnings", "quarterly results", "revenue"], 15.0),
    (CATEGORY_SECTOR_NEWS, ["sektor", "sektör", "regulasyon", "regülasyon", "industry", "regulation"], 0.0),
]

SOURCE_TRUST_HIGH = 80.0
SOURCE_TRUST_MEDIUM = 55.0
SOURCE_TRUST_LOW = 30.0

# Bilinen guvenilir finans/haber kaynaklari (basit, aciklanabilir bir liste;
# burada olmayan kaynaklar orta guven varsayilir).
_HIGH_TRUST_SOURCES = {
    "reuters.com", "bloomberg.com", "kap.org.tr", "borsagundem.com",
    "dunya.com", "aa.com.tr", "ntv.com.tr", "hurriyet.com.tr",
}

MIN_COMPANY_MATCH_CONFIDENCE = 40.0  # bu esigin altindaki haberler skora ETKI ETMEZ
NEWS_MAX_SCORE_CONTRIBUTION = 3.0  # /analiz toplam skoruna en fazla +-3 puan (bolum 4)
NEWS_AGE_HALF_LIFE_HOURS = 48.0  # haber yasi arttikca etki agirligi yarilanma suresi


def classify_category(title: str) -> str:
    """Baslikta gecen anahtar kelimelere gore SABIT kural tabanli kategori atar.
    Hicbir kelime eslesmezse 'belirsiz' doner (asla tahmin uydurulmaz)."""
    text = (title or "").lower()
    for category, keywords, _base_score in _CATEGORY_RULES:
        for kw in keywords:
            if kw in text:
                return category
    return CATEGORY_UNCERTAIN


def _base_score_for_category(category: str) -> float:
    for cat, _kw, score in _CATEGORY_RULES:
        if cat == category:
            return score
    return 0.0


def _source_trust(source: Optional[str]) -> float:
    if not source:
        return SOURCE_TRUST_LOW
    domain = source.lower().strip()
    for trusted in _HIGH_TRUST_SOURCES:
        if trusted in domain:
            return SOURCE_TRUST_HIGH
    return SOURCE_TRUST_MEDIUM


@dataclass
class NewsImpactAssessment:
    category: str
    impact_score: float  # -100..+100
    confidence_score: float  # 0..100
    source_confidence: float
    company_match_confidence: float
    news_age_hours: Optional[float]
    rationale: str
    counts_toward_score: bool

    @property
    def category_label_tr(self) -> str:
        return CATEGORY_LABELS_TR.get(self.category, self.category)


def assess_article(
    title: str,
    source: Optional[str],
    company_match_confidence: float,
    published_at: Optional[datetime],
    now: Optional[datetime] = None,
) -> NewsImpactAssessment:
    """Tek bir haber icin kural tabanli, aciklanabilir etki degerlendirmesi uretir.

    - Etki skoru: kategoriye gore sabit temel puan, haber yasina gore hafifce
      sonumlendirilir (eski haber daha az agirlik tasir).
    - Guven skoru: kaynak guveni + sirket eslesme guveninin ortalamasidir.
    - Dusuk sirket eslesme guveni (< MIN_COMPANY_MATCH_CONFIDENCE) olan haberler
      `counts_toward_score=False` isaretlenir; bunlar toplam skora ETKI ETMEZ,
      yalnizca BILGI amacli gosterilebilir.
    """
    now = now or datetime.now(timezone.utc)
    category = classify_category(title)
    base_score = _base_score_for_category(category)

    age_hours: Optional[float] = None
    age_factor = 1.0
    if published_at is not None:
        pub = published_at if published_at.tzinfo else published_at.replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (now - pub).total_seconds() / 3600.0)
        # Basit us-benzeri sonumlenme: yasi arttikca agirlik yarilanma suresine
        # gore azalir, ama asla sifirin altina inmez (0.1 taban).
        age_factor = max(0.1, 0.5 ** (age_hours / NEWS_AGE_HALF_LIFE_HOURS))

    source_conf = _source_trust(source)
    confidence_score = round((source_conf + company_match_confidence) / 2, 1)

    impact_score = round(base_score * age_factor, 1)
    impact_score = max(-100.0, min(100.0, impact_score))

    counts = company_match_confidence >= MIN_COMPANY_MATCH_CONFIDENCE

    rationale_parts = [f"Kategori: {CATEGORY_LABELS_TR.get(category, category)}"]
    if age_hours is not None:
        rationale_parts.append(f"haber yaşı ~{age_hours:.0f} saat")
    rationale_parts.append(f"kaynak güveni {source_conf:.0f}/100")
    rationale_parts.append(f"şirket eşleşme güveni {company_match_confidence:.0f}/100")
    if not counts:
        rationale_parts.append("düşük eşleşme güveni nedeniyle skora dahil edilmedi")
    rationale = "; ".join(rationale_parts)

    return NewsImpactAssessment(
        category=category,
        impact_score=impact_score,
        confidence_score=confidence_score,
        source_confidence=source_conf,
        company_match_confidence=company_match_confidence,
        news_age_hours=round(age_hours, 1) if age_hours is not None else None,
        rationale=rationale,
        counts_toward_score=counts,
    )


@dataclass
class NewsImpactSummary:
    """Bir sembol icin birden fazla haberin toplu, agirlikli ozetidir.

    Haber yoksa (assessments bos ise) `available=False` ve `impact_score=None`
    doner; bu ASLA negatif/pozitif bir puan olarak yorumlanmamalidir."""

    available: bool
    article_count: int
    impact_score: Optional[float]  # -100..+100 agirlikli ortalama (haber varsa)
    confidence_score: Optional[float]
    score_contribution: float  # /analiz toplam skoruna eklenecek NIHAI katki (-3..+3)
    top_assessments: list[NewsImpactAssessment] = field(default_factory=list)
    note: str = ""


def summarize_impact(assessments: list[NewsImpactAssessment]) -> NewsImpactSummary:
    """Birden fazla haberin etkisini tek bir ozet skoruna indirger.

    - Yalnizca `counts_toward_score=True` olan (yeterli sirket eslesme guvenine
      sahip) haberler agirliga katilir.
    - Haber tek basina AL/SAT sinyali OLUSTURMAZ: nihai katki daima
      [-NEWS_MAX_SCORE_CONTRIBUTION, +NEWS_MAX_SCORE_CONTRIBUTION] araligina
      sikistirilir (bolum 4 spesifikasyonu).
    - Hicbir gecerli haber yoksa (liste bos veya hepsi dusuk guvenli) available=False
      doner ve score_contribution 0.0'dir (haber YOKLUGU ceza/odul degildir).
    """
    if not assessments:
        return NewsImpactSummary(
            available=False, article_count=0, impact_score=None, confidence_score=None,
            score_contribution=0.0, top_assessments=[], note="Haber bulunamadı.",
        )

    countable = [a for a in assessments if a.counts_toward_score]
    if not countable:
        return NewsImpactSummary(
            available=True, article_count=len(assessments), impact_score=None, confidence_score=None,
            score_contribution=0.0, top_assessments=sorted(assessments, key=lambda a: abs(a.impact_score), reverse=True)[:3],
            note="Bulunan haberlerin şirket eşleşme güveni düşük; skora dahil edilmedi.",
        )

    weights = [max(a.confidence_score, 1.0) for a in countable]
    weighted_sum = sum(a.impact_score * w for a, w in zip(countable, weights))
    total_weight = sum(weights)
    avg_impact = round(weighted_sum / total_weight, 1) if total_weight > 0 else 0.0
    avg_confidence = round(sum(a.confidence_score for a in countable) / len(countable), 1)

    # -100..+100 -> -NEWS_MAX_SCORE_CONTRIBUTION..+NEWS_MAX_SCORE_CONTRIBUTION olceklendirme.
    raw_contribution = (avg_impact / 100.0) * NEWS_MAX_SCORE_CONTRIBUTION
    score_contribution = round(max(-NEWS_MAX_SCORE_CONTRIBUTION, min(NEWS_MAX_SCORE_CONTRIBUTION, raw_contribution)), 2)

    top = sorted(countable, key=lambda a: abs(a.impact_score), reverse=True)[:3]

    return NewsImpactSummary(
        available=True,
        article_count=len(assessments),
        impact_score=avg_impact,
        confidence_score=avg_confidence,
        score_contribution=score_contribution,
        top_assessments=top,
        note="",
    )
