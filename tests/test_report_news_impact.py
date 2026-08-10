from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.modules.report_news_impact import build_report_news_impact, format_report_news_impact
from app.services.market_breadth_service import BreadthCandidate, MarketBreadthResult


class _Kap:
    name = "kap_rest"

    def get_latest_disclosures(self, symbol: str):
        when = datetime(2026, 8, 10, 7, 0, tzinfo=timezone.utc)
        if symbol == "SASA":
            return [{
                "title": "Faaliyet durdurma kararı hakkında açıklama",
                "published_at": when,
                "source_url": "https://www.kap.org.tr/tr/Bildirim/1",
            }]
        if symbol == "THYAO":
            return [{
                "title": "Yeni ihale sözleşmesi imzalandı",
                "published_at": when,
                "source_url": "https://www.kap.org.tr/tr/Bildirim/2",
            }]
        return []


def _breadth() -> MarketBreadthResult:
    long = BreadthCandidate("THYAO", "LONG", 90, 2.0, 300.0, 1.2, ("EMA20 üstü",))
    short = BreadthCandidate("SASA", "SHORT/RİSK", 90, -2.0, 4.0, 1.2, ("EMA20 altı",))
    return MarketBreadthResult(
        available=True, note="", universe_size=571, scanned=571,
        long_candidates=(long,), short_candidates=(short,),
        top_gainers=(long,), top_losers=(short,),
    )


def test_report_kap_impact_keeps_only_recent_material_disclosures(tmp_path):
    sector_map = tmp_path / "sectors.yaml"
    sector_map.write_text(
        "symbols:\n  THYAO:\n    sector_name: Ulaştırma\n    sector_index: XULAS.IS\n",
        encoding="utf-8",
    )
    settings = SimpleNamespace(sector_map_path=str(sector_map), report_kap_symbol_limit=10)
    impact = build_report_news_impact(
        settings,
        _breadth(),
        kap_provider=_Kap(),
        now=datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert [item.symbol for item in impact.negative] == ["SASA"]
    assert [item.symbol for item in impact.positive] == ["THYAO"]
    text = "\n".join(format_report_news_impact(impact, timezone_name="Europe/Istanbul"))
    assert "HABER ETKİSİ • DOĞRULANMIŞ KAP" in text
    assert "Faaliyet durdurma" in text
    assert "Ulaştırma izlemesi: THYAO" in text


def test_report_kap_impact_stays_hidden_when_provider_is_disabled():
    settings = SimpleNamespace(sector_map_path="", report_kap_symbol_limit=10)
    disabled = SimpleNamespace(name="disabled")
    impact = build_report_news_impact(settings, _breadth(), kap_provider=disabled)

    assert impact.has_items is False
    assert format_report_news_impact(impact, timezone_name="Europe/Istanbul") == []
