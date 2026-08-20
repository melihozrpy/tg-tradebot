from __future__ import annotations

"""Robots-aware Midas KAP headline monitor with durable de-duplication."""

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analysis.news_impact_engine import assess_article
from app.models.database import KapMonitorEvent

logger = logging.getLogger("mergen_quant.kap_monitor")
USER_AGENT = "MontanaFinansRobotu/1.0 (+Telegram KAP monitor)"


@dataclass(frozen=True)
class KapHeadline:
    title: str
    source: str
    source_url: str
    relative_time: str | None
    impact_score: float
    category: str


def _robots_allow(client: httpx.Client, url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        response = client.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=10)
        if response.status_code >= 400:
            return False
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())
        return parser.can_fetch(USER_AGENT, url)
    except httpx.HTTPError:
        return False


def fetch_midas_kap_headlines(url: str) -> tuple[KapHeadline, ...]:
    """Fetch only public card headings, never article bodies or a protected API."""

    if urlparse(url).netloc.casefold() != "www.getmidas.com":
        raise ValueError("KAP monitörü yalnız yapılandırılmış getmidas.com başlık sayfasını kabul eder.")
    with httpx.Client(follow_redirects=True) as client:
        if not _robots_allow(client, url):
            raise PermissionError("Midas robots.txt bu başlık taramasına izin vermiyor.")
        response = client.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        response.raise_for_status()
    # The page currently declares a legacy default to HTTP clients although
    # its body is UTF-8.  Decode bytes explicitly so Turkish company names do
    # not turn into corrupted alert text in Telegram.
    soup = BeautifulSoup(response.content.decode("utf-8", errors="replace"), "html.parser")
    items: list[KapHeadline] = []
    for card in soup.select(".kap-card__inner")[:80]:
        body = card.select_one(".kap-card__content")
        if body is None:
            continue
        title = " ".join(body.get_text(" ", strip=True).split())
        if len(title) < 20:
            continue
        time_node = card.select_one(".kap-card__time")
        assessment = assess_article(
            title=title,
            source="getmidas.com",
            company_match_confidence=100.0,
            published_at=datetime.now(timezone.utc),
        )
        items.append(
            KapHeadline(
                title=title[:700],
                source="Midas KAP başlık akışı",
                source_url=url,
                relative_time=(" ".join(time_node.get_text(" ", strip=True).split()) if time_node else None),
                impact_score=assessment.impact_score,
                category=assessment.category_label_tr,
            )
        )
    return tuple(items)


def claim_new_impacting_headlines(
    db: Session,
    *,
    url: str,
    minimum_impact_score: float,
) -> tuple[KapHeadline, ...]:
    """Save unseen high-impact headlines and return only newly claimed items."""

    output: list[KapHeadline] = []
    for item in fetch_midas_kap_headlines(url):
        if abs(item.impact_score) < float(minimum_impact_score):
            continue
        content_hash = hashlib.sha256(f"{item.source_url}|{item.title}".encode("utf-8")).hexdigest()
        event = KapMonitorEvent(
            content_hash=content_hash,
            source=item.source,
            source_url=item.source_url,
            title=item.title,
            impact_score=item.impact_score,
            category=item.category,
        )
        try:
            with db.begin_nested():
                db.add(event)
                db.flush()
            output.append(item)
        except IntegrityError:
            continue
    db.commit()
    return tuple(output)


def format_kap_alert(item: KapHeadline) -> str:
    direction = "POZİTİF" if item.impact_score > 0 else "NEGATİF"
    sign = "+" if item.impact_score > 0 else ""
    return (
        f"🔔 KAP BAŞLIK RADARI — {direction}\n"
        f"📰 {item.title}\n"
        f"🏷️ Sınıf: {item.category}  •  Etki puanı: {sign}{item.impact_score:.0f}/100\n"
        f"⏰ Kaynakta görünen zaman: {item.relative_time or 'belirtilmedi'}\n"
        f"📌 Kaynak: {item.source}\n{item.source_url}\n\n"
        "Bu başlık otomatik anahtar-kelime sınıflamasıdır; işlemden önce resmî KAP açıklamasını doğrula."
    )[:4096]
