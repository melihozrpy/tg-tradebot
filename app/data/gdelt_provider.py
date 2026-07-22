from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("mergen_quant.data.gdelt")

GDELT_DOC_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_RECORDS = 50
DEFAULT_LOOKBACK_DAYS = 7


class GdeltUnavailableError(Exception):
    """GDELT'ten veri alinamadiginda firlatilir (zaman asimi, HTTP hatasi,
    beklenmeyen yanit formati). Bu hata teknik analizi ASLA durdurmaz; cagiran
    servis katmani bu hatayi yakalayip haberSIZ devam eder."""


@dataclass
class RawNewsArticle:
    title: str
    source: Optional[str]
    url: str
    published_at: Optional[datetime]
    language: Optional[str]
    matched_alias: str


_TURKISH_FOLD_MAP = str.maketrans({
    "ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u",
})


def _normalize_title_for_dedup(title: str) -> str:
    """Baslik icindeki noktalama/bosluk/Turkce ozel karakter farkliliklarini
    yok sayarak, farkli kaynaklardaki AYNI haberin (orn. 'uçak' / 'ucak')
    birlestirilebilmesi icin normallestirilmis bir anahtar uretir."""
    text = (title or "").lower().strip()
    text = text.translate(_TURKISH_FOLD_MAP)
    text = re.sub(r"[^a-z0-9 ]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def compute_dedup_key(title: str, published_at: Optional[datetime]) -> str:
    day = published_at.date().isoformat() if published_at else "bilinmeyen-tarih"
    normalized = _normalize_title_for_dedup(title)
    digest_input = f"{normalized}|{day}"
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:32]


def build_gdelt_provider(settings) -> "GdeltNewsProvider":
    """`app.data.provider_factory.build_market_data_provider` ile ayni desen:
    her cagrida hafif, taze bir saglayici ornegi olusturur."""
    return GdeltNewsProvider(
        timeout_seconds=settings.gdelt_timeout_seconds,
        max_retries=settings.gdelt_max_retries,
        cache_ttl_minutes=settings.news_cache_ttl_minutes,
    )


class GdeltNewsProvider:
    """GDELT DOC 2.0 API'sinden, bir sirketin adi ve alternatif isimleriyle
    (hisse koduyla DEGIL) haber arayan opsiyonel saglayici.

    - Rate limit / timeout / retry uygular (ayarlanabilir).
    - Kisa sureli (TTL'li) bellek-ici cache kullanir; ayni sorgu tekrar
      tekrar GDELT'e gonderilmez.
    - GDELT calismazsa (zaman asimi, HTTP hatasi) GdeltUnavailableError
      firlatir; cagiran taraf bunu yakalayip teknik analize haberSIZ devam eder.
    - Haber bulunamadiginda BOS liste doner; ASLA sahte haber uretmez.
    """

    name = "gdelt"

    def __init__(
        self,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        cache_ttl_minutes: int = 30,
        client: Optional[httpx.Client] = None,
        min_request_interval_seconds: float = 1.0,
    ):
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.cache_ttl_minutes = cache_ttl_minutes
        self._client = client
        self._owns_client = client is None
        self.min_request_interval_seconds = min_request_interval_seconds
        self._last_request_at: Optional[float] = None
        self._cache: dict[str, tuple[datetime, list[RawNewsArticle]]] = {}

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_seconds)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _respect_rate_limit(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        wait = self.min_request_interval_seconds - elapsed
        if wait > 0:
            time.sleep(wait)

    def _build_query(self, company_name: str, aliases: list[str]) -> str:
        terms = [company_name] + list(aliases)
        quoted = [f'"{t}"' for t in terms if t and t.strip()]
        return "(" + " OR ".join(quoted) + ")"

    def fetch_articles(
        self,
        symbol: str,
        company_name: str,
        aliases: list[str],
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        max_records: int = DEFAULT_MAX_RECORDS,
    ) -> list[RawNewsArticle]:
        """Verilen sirket adi/alternatif isimlerle GDELT'te haber arar.

        `company_name` bos ise (sirket eslestirmesi yoksa) hicbir istek
        ATILMAZ ve bos liste doner (uydurma arama yapilmaz)."""
        if not company_name or not company_name.strip():
            return []

        cache_key = f"{symbol}:{lookback_days}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            fetched_at, articles = cached
            age_minutes = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 60
            if age_minutes <= self.cache_ttl_minutes:
                return articles

        query = self._build_query(company_name, aliases)
        params = {
            "query": query,
            "mode": "artlist",
            "maxrecords": str(max_records),
            "timespan": f"{lookback_days}d",
            "format": "json",
        }

        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                self._respect_rate_limit()
                client = self._get_client()
                response = client.get(GDELT_DOC_API_URL, params=params, timeout=self.timeout_seconds)
                self._last_request_at = time.monotonic()
                response.raise_for_status()
                payload = response.json()
                articles = self._parse_payload(payload, company_name, aliases)
                self._cache[cache_key] = (datetime.now(timezone.utc), articles)
                return articles
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                logger.warning(
                    "GDELT istegi basarisiz (deneme %s/%s) symbol=%s: %s",
                    attempt + 1, self.max_retries + 1, symbol, exc,
                )
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))

        raise GdeltUnavailableError(f"GDELT'ten {symbol} icin veri alinamadi: {last_exc}")

    def _parse_payload(self, payload: dict, company_name: str, aliases: list[str]) -> list[RawNewsArticle]:
        raw_articles = payload.get("articles") if isinstance(payload, dict) else None
        if not raw_articles:
            return []

        all_terms_lower = [company_name.lower()] + [a.lower() for a in aliases]
        results: list[RawNewsArticle] = []
        for item in raw_articles:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            url = item.get("url") or ""
            source = item.get("domain") or item.get("sourcecountry")
            language = item.get("language")
            published_at = _parse_gdelt_date(item.get("seendate"))

            matched_alias = company_name
            title_lower = title.lower()
            for term in all_terms_lower:
                if term and term in title_lower:
                    matched_alias = term
                    break

            results.append(
                RawNewsArticle(
                    title=title, source=source, url=url,
                    published_at=published_at, language=language,
                    matched_alias=matched_alias,
                )
            )
        return results

    def health_check(self) -> dict:
        return {"provider": self.name, "status": "ok", "detail": "GDELT saglayicisi hazir (istek atilmadi)."}


def _parse_gdelt_date(raw: Optional[str]) -> Optional[datetime]:
    """GDELT 'seendate' alani genelde 'YYYYMMDDHHMMSS' formatindadir."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        return datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
