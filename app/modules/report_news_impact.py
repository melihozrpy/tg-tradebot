"""Verified KAP-only news impact panel for the morning/evening reports.

The report deliberately checks a bounded, technically prioritised shortlist.
It never scrapes KAP pages and never invents an impact when the contracted KAP
provider is disabled or has no dated disclosure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from app.config.settings import load_sector_map
from app.services.market_breadth_service import BreadthCandidate, MarketBreadthResult

logger = logging.getLogger("mergen_quant.modules.report_news_impact")

_SERIOUS_NEGATIVE = (
    "iflas", "konkordato", "temerrüt", "temerrut", "faaliyet durdur",
    "üretim durdur", "uretim durdur", "sermaye kayb", "haciz", "tasfiye",
    "lisans iptal", "işlem sırası kapat", "islem sirasi kapat",
)
_POSITIVE = (
    "ihale", "sözleşme", "sozlesme", "sipariş", "siparis", "yatırım",
    "yatirim", "kapasite art", "geri alım", "geri alim", "temettü", "temettu",
)
_STRONG_POSITIVE = ("ihale", "sözleşme", "sozlesme", "sipariş", "siparis", "kapasite art")


@dataclass(frozen=True)
class KapImpactItem:
    symbol: str
    title: str
    published_at: datetime
    source_url: str
    sector_name: str | None
    watch_symbols: tuple[str, ...]
    impact_score: int
    rationale: str


@dataclass(frozen=True)
class ReportNewsImpact:
    negative: tuple[KapImpactItem, ...] = ()
    positive: tuple[KapImpactItem, ...] = ()
    inspected_symbols: int = 0

    @property
    def has_items(self) -> bool:
        return bool(self.negative or self.positive)


def _as_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _classify(disclosure: dict) -> str:
    text = " ".join(str(disclosure.get(key) or "") for key in ("title", "summary", "subject")).casefold()
    if any(keyword in text for keyword in _SERIOUS_NEGATIVE):
        return "negative"
    if any(keyword in text for keyword in _POSITIVE):
        return "positive"
    return "neutral"


def analyze_news_sector_impact(
    disclosure: dict,
    *,
    sector_name: str | None,
    watch_symbols: tuple[str, ...],
) -> tuple[str, int, str]:
    """Score a verified disclosure without inventing a macro-news conclusion.

    This is intentionally deterministic and fast: scheduled reports must not
    wait on an LLM.  The score combines the KAP source's reliability, the
    seriousness of the disclosed event and whether a mapped sector has more
    than one technically relevant symbol.  Only scores at the configured
    threshold are shown to the user.
    """

    category = _classify(disclosure)
    text = " ".join(str(disclosure.get(key) or "") for key in ("title", "summary", "subject")).casefold()
    sector_bonus = 5 if sector_name and len(watch_symbols) > 1 else 0
    if category == "negative":
        score = min(100, 85 + sector_bonus)
        rationale = "Resmî KAP bildirimindeki olumsuz başlık, ilgili hisse ve sektör algısını baskılayabilir."
    elif category == "positive":
        score = min(100, (80 if any(word in text for word in _STRONG_POSITIVE) else 70) + sector_bonus)
        rationale = "Resmî KAP bildirimi olumlu bir katalizör olabilir; teknik teyit olmadan fiyat etkisi varsayılmaz."
    else:
        score = 0
        rationale = ""
    return category, score, rationale


def _prioritised_symbols(breadth: MarketBreadthResult, maximum: int) -> list[str]:
    ordered: list[BreadthCandidate] = [
        *breadth.short_candidates,
        *breadth.long_candidates,
        *breadth.top_losers,
        *breadth.top_gainers,
    ]
    output: list[str] = []
    for candidate in ordered:
        symbol = candidate.symbol.upper().removesuffix(".IS")
        if symbol and symbol not in output:
            output.append(symbol)
        if len(output) >= maximum:
            break
    return output


def _sector_details(settings, symbol: str, candidates: Iterable[BreadthCandidate]) -> tuple[str | None, tuple[str, ...]]:
    try:
        entry = load_sector_map(getattr(settings, "sector_map_path", "")).get("symbols", {}).get(symbol, {})
    except Exception:  # noqa: BLE001 - an optional map cannot break reports
        entry = {}
    sector_name = str(entry.get("sector_name") or "").strip() or None
    if not sector_name:
        return None, (symbol,)
    try:
        mappings = load_sector_map(getattr(settings, "sector_map_path", "")).get("symbols", {})
    except Exception:  # noqa: BLE001
        mappings = {}
    related = [symbol]
    for candidate in candidates:
        mapped = mappings.get(candidate.symbol, {})
        if mapped.get("sector_name") == sector_name and candidate.symbol not in related:
            related.append(candidate.symbol)
        if len(related) >= 4:
            break
    return sector_name, tuple(related)


def build_report_news_impact(
    settings,
    breadth: MarketBreadthResult | None,
    *,
    kap_provider=None,
    now: datetime | None = None,
) -> ReportNewsImpact:
    """Return only recent, dated and classification-relevant KAP disclosures.

    Scanning all 571 symbols against a per-symbol provider during each report
    would create avoidable rate-limit pressure.  The shortlist contains the
    highest current technical long/weakness candidates, so it is useful and
    bounded.  A provider with a proper bulk endpoint can replace this adapter
    later without changing the report format.
    """

    if breadth is None or not breadth.available:
        return ReportNewsImpact()
    if kap_provider is None:
        try:
            from app.data.provider_factory import build_kap_provider

            kap_provider = build_kap_provider(settings)
        except Exception as exc:  # noqa: BLE001
            logger.info("KAP rapor etkisi sağlayıcısı kurulamadı: %s", type(exc).__name__)
            return ReportNewsImpact()
    if getattr(kap_provider, "name", "disabled") == "disabled":
        return ReportNewsImpact()

    maximum = max(4, min(40, int(getattr(settings, "report_kap_symbol_limit", 20))))
    symbols = _prioritised_symbols(breadth, maximum)
    cutoff = (now or datetime.now(timezone.utc)).astimezone(timezone.utc) - timedelta(hours=48)
    negative: list[KapImpactItem] = []
    positive: list[KapImpactItem] = []
    candidates = (*breadth.long_candidates, *breadth.short_candidates)
    for symbol in symbols:
        try:
            disclosures = kap_provider.get_latest_disclosures(symbol)
        except Exception as exc:  # noqa: BLE001 - one KAP lookup cannot break a report
            logger.info("KAP rapor etkisi alınamadı symbol=%s error=%s", symbol, type(exc).__name__)
            continue
        for disclosure in disclosures[:8]:
            published_at = _as_utc(disclosure.get("published_at"))
            if published_at is None or published_at < cutoff:
                continue
            sector_name, watch_symbols = _sector_details(settings, symbol, candidates)
            category, impact_score, rationale = analyze_news_sector_impact(
                disclosure,
                sector_name=sector_name,
                watch_symbols=watch_symbols,
            )
            minimum_score = max(1, min(100, int(getattr(settings, "report_news_impact_minimum_score", 70))))
            if category == "neutral" or impact_score < minimum_score:
                logger.info(
                    "KAP rapor etkisi sessizce atlandı symbol=%s category=%s score=%s",
                    symbol,
                    category,
                    impact_score,
                )
                continue
            item = KapImpactItem(
                symbol=symbol,
                title=" ".join(str(disclosure.get("title") or "KAP bildirimi").split())[:260],
                published_at=published_at,
                source_url=str(disclosure.get("source_url") or "https://www.kap.org.tr/tr/bildirim-sorgu"),
                sector_name=sector_name,
                watch_symbols=watch_symbols,
                impact_score=impact_score,
                rationale=rationale,
            )
            (negative if category == "negative" else positive).append(item)
            break
    negative.sort(key=lambda item: item.published_at, reverse=True)
    positive.sort(key=lambda item: item.published_at, reverse=True)
    return ReportNewsImpact(tuple(negative[:2]), tuple(positive[:2]), len(symbols))


def format_report_news_impact(impact: ReportNewsImpact | None, *, timezone_name: str) -> list[str]:
    """Render no block at all when there is no material verified KAP item."""

    if impact is None or not impact.has_items:
        return []
    from zoneinfo import ZoneInfo

    lines = ["", "📰 KAP ETKİSİ • yalnızca yüksek önem"]
    if impact.negative:
        item = impact.negative[0]
        local = item.published_at.astimezone(ZoneInfo(timezone_name))
        affected = item.sector_name or item.symbol
        lines.append(f"🔴 {item.symbol}: {item.title} ({local:%d.%m %H:%M})")
        lines.append(f"   Etki: {affected} üzerinde baskı riski; teknik teyit olmadan nedensellik kurulmaz.")
    if impact.positive:
        item = impact.positive[0]
        local = item.published_at.astimezone(ZoneInfo(timezone_name))
        watched = ", ".join(item.watch_symbols)
        lines.append(f"🟢 {item.symbol}: {item.title} ({local:%d.%m %H:%M})")
        lines.append(f"   İzleme: {watched} • teknik teyit gelirse katalizör olabilir.")
    return lines
