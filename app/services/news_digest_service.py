from __future__ import annotations

"""Short, source-linked 24-48 hour news digest for ``/haber``."""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.config.settings import Settings
from app.data.base_provider import KapProvider
from app.data.gdelt_provider import GdeltNewsProvider
from app.models.database import NewsDigestCache
from app.services.groq_service import GroqExplainer
from app.services.news_service import get_company_alias_info, get_recent_articles, scan_symbol_news

logger = logging.getLogger("mergen_quant.news_digest")
USER_AGENT = "MontanaFinansRobotu/1.0 (+Telegram news summary)"


def _utc(value) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@dataclass(frozen=True)
class NewsDigestItem:
    title: str
    source: str
    url: str
    published_at: str | None
    kind: str
    sentiment: str = "neutral"


@dataclass(frozen=True)
class NewsDigest:
    symbol: str
    company_name: str
    items: tuple[NewsDigestItem, ...]
    sentiment: str
    sentiment_reason: str
    prepared_at: str
    from_cache: bool = False


def _robots_allows(client: httpx.Client, url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        response = client.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=8)
        if response.status_code >= 400:
            return False
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())
        return parser.can_fetch(USER_AGENT, url)
    except httpx.HTTPError:
        return False


def _scrape_configured_headlines(
    settings: Settings,
    *,
    symbol: str,
    company_name: str,
) -> list[NewsDigestItem]:
    """Scrape only configured, robots-permitted headline pages; never article text."""

    templates = [item.strip() for item in settings.news_scrape_urls.split(",") if item.strip()]
    if not templates:
        return []
    output: list[NewsDigestItem] = []
    with httpx.Client(follow_redirects=True) as client:
        for template in templates:
            try:
                url = template.format(symbol=symbol, company=company_name.replace(" ", "+"))
            except (KeyError, ValueError):
                logger.warning("NEWS_SCRAPE_URLS sablonu gecersiz; atlandi")
                continue
            if not _robots_allows(client, url):
                logger.info("robots.txt headline taramasina izin vermedi host=%s", urlparse(url).netloc)
                continue
            try:
                response = client.get(url, headers={"User-Agent": USER_AGENT}, timeout=12)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("Headline sayfasi alinamadi host=%s error=%s", urlparse(url).netloc, type(exc).__name__)
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            seen: set[str] = set()
            for anchor in soup.select("article a[href], h1 a[href], h2 a[href], h3 a[href]"):
                title = " ".join(anchor.get_text(" ", strip=True).split())
                if len(title) < 12 or title.casefold() in seen:
                    continue
                seen.add(title.casefold())
                output.append(
                    NewsDigestItem(
                        title=title[:240],
                        source=urlparse(url).netloc,
                        url=urljoin(url, str(anchor.get("href"))),
                        published_at=None,
                        kind="web",
                    )
                )
                if len(output) >= 6:
                    break
    return output


def _load_cache(db: Session, symbol: str) -> NewsDigest | None:
    row = (
        db.query(NewsDigestCache)
        .filter(NewsDigestCache.symbol == symbol)
        .filter(NewsDigestCache.expires_at > datetime.now(timezone.utc))
        .first()
    )
    if row is None:
        return None
    try:
        payload = json.loads(row.payload_json)
        payload["items"] = tuple(NewsDigestItem(**item) for item in payload.get("items", []))
        payload["from_cache"] = True
        return NewsDigest(**payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _save_cache(db: Session, digest: NewsDigest, ttl_minutes: int) -> None:
    payload = asdict(digest)
    payload["from_cache"] = False
    row = db.query(NewsDigestCache).filter(NewsDigestCache.symbol == digest.symbol).first()
    if row is None:
        row = NewsDigestCache(symbol=digest.symbol, payload_json="{}", sentiment=digest.sentiment)
        db.add(row)
    row.payload_json = json.dumps(payload, ensure_ascii=False)
    row.sentiment = digest.sentiment
    row.created_at = datetime.now(timezone.utc)
    row.expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    db.commit()


def build_news_digest(
    db: Session,
    *,
    symbol: str,
    settings: Settings,
    gdelt_provider: GdeltNewsProvider,
    kap_provider: KapProvider,
) -> NewsDigest:
    normalized = symbol.upper().removesuffix(".IS")
    cached = _load_cache(db, normalized)
    if cached is not None:
        return cached
    alias = get_company_alias_info(normalized, settings.company_aliases_path)
    company_name = alias["company_name"] if alias else normalized
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.news_digest_lookback_hours)
    raw = []

    # Existing GDELT service remains the primary web-search layer.  Unknown
    # company mappings use the exact exchange symbol, never an invented name.
    if alias:
        scan_symbol_news(
            db,
            normalized,
            gdelt_provider,
            settings,
            lookback_days=max(1, (settings.news_digest_lookback_hours + 23) // 24),
        )
    else:
        try:
            raw = gdelt_provider.fetch_articles(
                normalized,
                normalized,
                [],
                lookback_days=max(1, (settings.news_digest_lookback_hours + 23) // 24),
            )
            # Unknown-symbol results are intentionally not persisted as
            # company-matched records. They are still shown as symbol search.
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sembol bazli GDELT aramasi basarisiz symbol=%s error=%s", normalized, type(exc).__name__)
            raw = []
    items: list[NewsDigestItem] = []
    if alias:
        for article in get_recent_articles(db, normalized, limit=20):
            published = _utc(article.published_at)
            if published is not None and published < cutoff:
                continue
            items.append(
                NewsDigestItem(
                    title=" ".join(article.title.split())[:240],
                    source=article.source or article.provider,
                    url=article.url,
                    published_at=published.isoformat() if published else None,
                    kind="news",
                )
            )
    else:
        for article in raw:
            if article.published_at is not None and article.published_at < cutoff:
                continue
            items.append(
                NewsDigestItem(
                    title=" ".join(article.title.split())[:240],
                    source=article.source or "GDELT",
                    url=article.url,
                    published_at=article.published_at.isoformat() if article.published_at else None,
                    kind="symbol_search",
                )
            )

    try:
        for disclosure in kap_provider.get_latest_disclosures(normalized)[:8]:
            published = _utc(disclosure.get("published_at"))
            if published is not None and published < cutoff:
                continue
            items.append(
                NewsDigestItem(
                    title=str(disclosure.get("title") or "KAP bildirimi")[:240],
                    source="KAP",
                    url=str(disclosure.get("source_url") or "https://www.kap.org.tr/tr/bildirim-sorgu"),
                    published_at=published.isoformat() if published else None,
                    kind="kap",
                )
            )
    except Exception as exc:  # noqa: BLE001 - KAP outage must not break news
        logger.warning("KAP haber ozetine eklenemedi symbol=%s error=%s", normalized, type(exc).__name__)

    items.extend(_scrape_configured_headlines(settings, symbol=normalized, company_name=company_name))
    deduped: list[NewsDigestItem] = []
    seen: set[tuple[str, str]] = set()
    for item in sorted(items, key=lambda value: value.published_at or "", reverse=True):
        key = (item.title.casefold(), item.url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= 10:
            break

    explainer = GroqExplainer(settings)
    try:
        labels, fallback = explainer.classify_news_sentiment(db, [item.title for item in deduped])
    finally:
        explainer.close()
    labelled = tuple(
        NewsDigestItem(**{**asdict(item), "sentiment": labels[index] if index < len(labels) else "neutral"})
        for index, item in enumerate(deduped)
    )
    counts = {label: labels.count(label) for label in ("positive", "neutral", "negative")}
    sentiment = "positive" if counts["positive"] > counts["negative"] else "negative" if counts["negative"] > counts["positive"] else "neutral"
    tr_label = {"positive": "Pozitif", "neutral": "Nötr", "negative": "Negatif"}[sentiment]
    reason = (
        f"{len(labelled)} başlığın {counts['positive']} pozitif, {counts['neutral']} nötr, "
        f"{counts['negative']} negatif sınıflandırılması ({'kural tabanlı fallback' if fallback else 'Groq'})."
    )
    digest = NewsDigest(
        symbol=normalized,
        company_name=company_name,
        items=labelled,
        sentiment=tr_label,
        sentiment_reason=reason,
        prepared_at=datetime.now(timezone.utc).isoformat(),
    )
    _save_cache(db, digest, settings.news_digest_cache_minutes)
    return digest


def format_news_digest(digest: NewsDigest) -> str:
    lines = [f"📰 {digest.symbol} — Son Haberler & Yorumlar", f"🏢 {digest.company_name}", ""]
    if not digest.items:
        lines.append("🔹 Son 24–48 saatte doğrulanmış başlık bulunamadı; haber uydurulmadı.")
    for item in digest.items:
        date_text = item.published_at[:10] if item.published_at else "tarih belirtilmedi"
        icon = "🏛️" if item.kind == "kap" else "🔹"
        lines.append(f"{icon} {item.title} — kaynak: {item.source} (📅 {date_text})")
        lines.append(item.url)
    lines.extend(
        [
            "",
            f"💬 Genel Piyasa Algısı: {digest.sentiment} — {digest.sentiment_reason}",
            "🧭 Yorum/forum başlıkları doğrulanmış şirket açıklaması değildir; varsa yalnız piyasa algısı olarak okunur.",
            f"🗃️ Kaynak: {'15 dk cache' if digest.from_cache else 'yeni tarama'}",
            "⚠️ Bu içerik yatırım tavsiyesi değildir.",
        ]
    )
    return "\n".join(lines)[:4096]
