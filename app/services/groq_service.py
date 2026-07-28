from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from pydantic import BaseModel, ValidationError, field_validator
from sqlalchemy.orm import Session

from app.config.settings import Settings
from app.models.database import GroqExplanation

logger = logging.getLogger("mergen_quant.groq")

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

KIND_TECHNICAL = "teknik"
KIND_MULTI_TIMEFRAME = "coklu_zaman"
KIND_NEWS = "haber"
KIND_RISK = "risk"
KIND_MARKET_SENTIMENT = "market_sentiment"

# Groq'un ASLA uretmemesi gereken seyler: fiyat/hedef/stop rakamlari ve
# AL/SAT karari degistirme dili. Bu kaba desen listesi, Groq cevabini kabul
# etmeden once son bir GUVENLIK KAPISI olarak kullanilir; eslesme olursa
# cevap REDDEDILIR ve deterministik sablona dusulur (bolum 3 spesifikasyonu).
_FORBIDDEN_PATTERNS = [
    r"hedef fiyat", r"stop\s*(-|\s)?loss", r"stop\s*seviyesi",
    r"\bal\s*sinyal", r"\bsat\s*sinyal", r"\bal\b tavsiyesi", r"\bsat\b tavsiyesi",
    r"\d+[.,]\d+\s*(tl|try|₺)",
]


class GroqQuotaExceededError(Exception):
    pass


class GroqUnavailableError(Exception):
    pass


class GroqUnsafeResponseError(Exception):
    """Groq cevabi yasakli fiyat/hedef/stop/karar dili icerdiginde firlatilir."""


class GroqExplanationPayload(BaseModel):
    """Groq'tan beklenen YAPILANDIRILMIS cevap semasi. Uygun olmayan cevaplar
    (eksik alan, yanlis tip, yasakli icerik) kabul EDILMEZ."""

    explanation: str

    @field_validator("explanation")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("explanation bos olamaz")
        return v.strip()


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}...{value[-2:]}(masked)"


def compute_cache_key(symbol: str, kind: str, structured_payload: dict) -> str:
    payload_json = json.dumps(structured_payload, sort_keys=True, ensure_ascii=False)
    digest_input = f"{symbol.upper()}|{kind}|{payload_json}"
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:48]


def _contains_forbidden_content(text: str) -> bool:
    lowered = text.lower()
    for pattern in _FORBIDDEN_PATTERNS:
        if re.search(pattern, lowered):
            return True
    return False


def _build_prompt(kind: str, symbol: str, structured_payload: dict) -> str:
    payload_json = json.dumps(structured_payload, ensure_ascii=False, indent=2)
    task_map = {
        KIND_TECHNICAL: "Verilen teknik gostergeleri sade, gunluk konusma Turkcesiyle acikla.",
        KIND_MULTI_TIMEFRAME: "Verilen coklu zaman dilimi (kisa/orta/uzun vade) verisini kisaca ozetle.",
        KIND_NEWS: "Verilen haber ozetini sade Turkce ile aciklа.",
        KIND_RISK: "Verilen veriye dayanarak ana riskleri sade Turkce ile anlat.",
    }
    task = task_map.get(kind, "Verilen veriyi sade Turkce ile acikla.")
    return (
        f"Sen bir finans yazarisin. {task}\n"
        "KURALLAR (KESINLIKLE UY):\n"
        "- Hicbir fiyat, hedef fiyat veya stop seviyesi UYDURMA veya TEKRARLAMA.\n"
        "- AL/SAT/TUT karari VERME veya degistirme; yalnizca mevcut veriyi acikla.\n"
        "- Hicbir teknik gosterge HESAPLAMA; yalnizca sana verilen sayilari yorumla.\n"
        "- Hicbir haber veya veri UYDURMA; yalnizca asagidaki JSON'u kullan.\n"
        "- Sadece 'explanation' alani olan JSON ile cevap ver: {\"explanation\": \"...\"}\n\n"
        f"Sembol: {symbol}\n"
        f"Yapilandirilmis veri:\n{payload_json}\n"
    )


def _deterministic_fallback(kind: str, symbol: str, structured_payload: dict) -> str:
    """Groq kapaliyken / hata/kota/zaman asiminda kullanilan, tamamen
    deterministik (LLM'siz) aciklama sablonu. Groq olmadan da /ai_aciklama
    ve /analiz akislari CALISMAYA devam eder."""
    if kind == KIND_NEWS:
        count = structured_payload.get("count_7d", 0)
        if not count:
            return f"{symbol} icin son 7 günde takip edilen bir haber bulunmuyor."
        impact = structured_payload.get("impact_score")
        impact_txt = "nötr" if impact is None else ("olumlu yönde" if impact > 0 else "olumsuz yönde" if impact < 0 else "nötr")
        return f"{symbol} icin son 7 günde {count} haber tespit edildi; genel eğilim {impact_txt} görünüyor. (Bu haber etkisi tek başına al/sat kararı oluşturmaz.)"
    if kind == KIND_MULTI_TIMEFRAME:
        return f"{symbol} icin coklu zaman dilimi verisi mevcut; detaylar icin /analiz_detay komutunu kullanabilirsin."
    if kind == KIND_RISK:
        return f"{symbol} icin mevcut veriye dayali temel riskler /analiz_detay ciktisinda listelenmistir."
    return f"{symbol} icin teknik gostergeler mevcut veriye dayanarak hesaplanmistir; detaylar /analiz_detay komutunda."


class GroqExplainer:
    """Groq API'sine (tamamen opsiyonel) yapilandirilmis JSON gonderip sade
    Turkce aciklama alan servis.

    Groq KESINLIKLE fiyat/hedef/stop uretemez, AL/SAT karari degistiremez,
    gosterge hesaplayamaz, haber/veri uyduramaz -- bunlar hem prompt
    seviyesinde hem de cevap kabul edilmeden once bir guvenlik kapisinda
    (`_contains_forbidden_content`) zorlanir. Herhangi bir hata/kota/zaman
    asiminda deterministik sablona DUSULUR; /ai_aciklama ve /analiz akislari
    hicbir zaman Groq'a bagimli olarak COKMEZ."""

    def __init__(
        self,
        settings: Settings,
        client: Optional[httpx.Client] = None,
    ):
        self.settings = settings
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.settings.groq_timeout_seconds)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _daily_request_count(self, db: Session) -> int:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        return (
            db.query(GroqExplanation)
            .filter(GroqExplanation.created_at >= since)
            .filter(GroqExplanation.is_fallback.is_(False))
            .count()
        )

    def _call_groq_api(self, prompt: str) -> str:
        if not self.settings.groq_api_key:
            raise GroqUnavailableError("GROQ_API_KEY tanimli degil.")

        headers = {
            "Authorization": f"Bearer {self.settings.groq_api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.settings.groq_model or "llama-3.1-8b-instant",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }

        last_exc: Optional[Exception] = None
        for attempt in range(self.settings.groq_max_retries + 1):
            try:
                client = self._get_client()
                response = client.post(GROQ_API_URL, headers=headers, json=body, timeout=self.settings.groq_timeout_seconds)
                if response.status_code == 429:
                    raise GroqQuotaExceededError("Groq kota/rate-limit hatasi (429).")
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                return content
            except GroqQuotaExceededError:
                raise
            except (httpx.HTTPError, KeyError, ValueError, IndexError) as exc:
                last_exc = exc
                # NOT: hata mesaji API anahtarini ASLA icermez (yalnizca istisna metni loglanir).
                logger.warning("Groq istegi basarisiz (deneme %s/%s): %s", attempt + 1, self.settings.groq_max_retries + 1, exc)
                if attempt < self.settings.groq_max_retries:
                    time.sleep(min(2 ** attempt, 8))

        raise GroqUnavailableError(f"Groq'tan cevap alinamadi: {last_exc}")

    def explain(
        self,
        db: Session,
        symbol: str,
        kind: str,
        structured_payload: dict,
    ) -> tuple[str, bool]:
        """Yapilandirilmis veri icin sade Turkce aciklama uretir.

        Onbellekte varsa (ayni symbol+kind+veri) tekrar istek ATILMAZ.
        Groq kapali, kota dolmus, hata veya gecersiz/yasakli cevap durumunda
        deterministik sablona DUSULUR (asla istisna firlatmaz).

        Doner: (aciklama_metni, is_fallback)"""
        symbol = symbol.upper()
        cache_key = compute_cache_key(symbol, kind, structured_payload)

        cached = db.query(GroqExplanation).filter(GroqExplanation.cache_key == cache_key).first()
        if cached is not None:
            return cached.response_text, cached.is_fallback

        if not self.settings.groq_enabled:
            text = _deterministic_fallback(kind, symbol, structured_payload)
            self._persist(db, symbol, kind, cache_key, text, is_fallback=True, model=None)
            return text, True

        if self._daily_request_count(db) >= self.settings.groq_daily_request_limit:
            logger.info("Groq gunluk istek limiti doldu; deterministik sablona dusuluyor.")
            text = _deterministic_fallback(kind, symbol, structured_payload)
            self._persist(db, symbol, kind, cache_key, text, is_fallback=True, model=None)
            return text, True

        prompt = _build_prompt(kind, symbol, structured_payload)
        try:
            raw_content = self._call_groq_api(prompt)
            parsed_json = json.loads(raw_content)
            payload = GroqExplanationPayload.model_validate(parsed_json)
            if _contains_forbidden_content(payload.explanation):
                raise GroqUnsafeResponseError("Groq cevabi yasakli icerik barindiriyor.")

            self._persist(
                db, symbol, kind, cache_key, payload.explanation,
                is_fallback=False, model=self.settings.groq_model or None,
            )
            return payload.explanation, False
        except (GroqUnavailableError, GroqQuotaExceededError, GroqUnsafeResponseError, ValidationError, json.JSONDecodeError) as exc:
            logger.warning("Groq aciklamasi alinamadi, deterministik sablona donuluyor: %s", exc)
            text = _deterministic_fallback(kind, symbol, structured_payload)
            self._persist(db, symbol, kind, cache_key, text, is_fallback=True, model=None)
            return text, True

    @staticmethod
    def _deterministic_sentiments(texts: list[str]) -> list[str]:
        positive_words = {
            "artış", "artis", "yükseliş", "yukselis", "büyüme", "buyume",
            "güçlü", "guclu", "rekor", "kâr", "kar", "iyileşme", "iyilesme",
            "above", "beat", "growth", "positive",
        }
        negative_words = {
            "düşüş", "dusus", "daralma", "zayıf", "zayif", "zarar", "risk",
            "gerileme", "kriz", "soruşturma", "sorusturma", "below", "miss",
            "negative", "loss",
        }
        output: list[str] = []
        for text in texts:
            words = set(re.findall(r"[a-zA-ZçğıöşüÇĞİÖŞÜ]+", text.casefold()))
            positive = len(words & positive_words)
            negative = len(words & negative_words)
            output.append("positive" if positive > negative else "negative" if negative > positive else "neutral")
        return output

    def classify_news_sentiment(
        self,
        db: Session,
        texts: list[str],
    ) -> tuple[list[str], bool]:
        """Haber başlıklarını toplu ve yapılandırılmış biçimde sınıflandırır.

        Dönüş ``(etiketler, is_fallback)`` biçimindedir. Etiketler yalnızca
        positive/neutral/negative olabilir; sayı veya yön kararı üretilmez.
        """

        clean = [" ".join(str(text).split())[:600] for text in texts if str(text).strip()][:40]
        if not clean:
            return [], True
        payload = {"texts": clean}
        cache_key = compute_cache_key("MARKET", KIND_MARKET_SENTIMENT, payload)
        cached = db.query(GroqExplanation).filter(GroqExplanation.cache_key == cache_key).first()
        if cached is not None:
            try:
                labels = json.loads(cached.response_text).get("sentiments", [])
                if len(labels) == len(clean) and all(
                    value in {"positive", "neutral", "negative"} for value in labels
                ):
                    return list(labels), bool(cached.is_fallback)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

        fallback = self._deterministic_sentiments(clean)
        if (
            not self.settings.groq_enabled
            or not self.settings.groq_api_key
            or self._daily_request_count(db) >= self.settings.groq_daily_request_limit
        ):
            encoded = json.dumps({"sentiments": fallback}, ensure_ascii=False)
            self._persist(
                db, "MARKET", KIND_MARKET_SENTIMENT, cache_key, encoded,
                is_fallback=True, model=None,
            )
            return fallback, True

        prompt = (
            "Aşağıdaki haber başlıklarını sadece duygu yönüne göre sınıflandır. "
            "Her başlık için sırasıyla yalnız positive, neutral veya negative kullan. "
            "Fiyat, hedef, yatırım kararı ya da açıklama üretme. "
            "Sadece şu JSON şemasında cevap ver: "
            '{"sentiments":["positive","neutral","negative"]}\n\n'
            + json.dumps(payload, ensure_ascii=False)
        )
        try:
            parsed = json.loads(self._call_groq_api(prompt))
            labels = parsed.get("sentiments")
            if not isinstance(labels, list) or len(labels) != len(clean):
                raise ValueError("Groq sentiment adetleri girdiyle eşleşmiyor.")
            normalized = [str(value).strip().casefold() for value in labels]
            if any(value not in {"positive", "neutral", "negative"} for value in normalized):
                raise ValueError("Groq sentiment etiketi geçersiz.")
            encoded = json.dumps({"sentiments": normalized}, ensure_ascii=False)
            self._persist(
                db, "MARKET", KIND_MARKET_SENTIMENT, cache_key, encoded,
                is_fallback=False, model=self.settings.groq_model or None,
            )
            return normalized, False
        except Exception as exc:  # noqa: BLE001 - sentiment raporu asla çökertmez
            logger.warning("Groq sentiment sınıflandırması başarısız: %s", type(exc).__name__)
            encoded = json.dumps({"sentiments": fallback}, ensure_ascii=False)
            self._persist(
                db, "MARKET", KIND_MARKET_SENTIMENT, cache_key, encoded,
                is_fallback=True, model=None,
            )
            return fallback, True

    def _persist(
        self, db: Session, symbol: str, kind: str, cache_key: str,
        text: str, is_fallback: bool, model: Optional[str],
    ) -> None:
        row = GroqExplanation(
            symbol=symbol, kind=kind, cache_key=cache_key,
            model=model, response_text=text, is_fallback=is_fallback,
        )
        db.add(row)
        db.commit()
