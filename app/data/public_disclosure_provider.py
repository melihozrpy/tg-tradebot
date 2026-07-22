from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class PublicDisclosureItem:
    title: str
    published_at: Optional[datetime]
    source_url: str
    keyword_classification: str  # basit anahtar kelime sinifi: pozitif_olabilir | negatif_olabilir | belirsiz


class PublicDisclosureProvider(ABC):
    """Kamuya acik, ucretsiz duyuru kaynagi (RSS vb.) icin arayuz.

    KAP'in ucretli/anahtar gerektiren resmi API'si BURADA KULLANILMAZ.
    Bu arayuz yalnizca acikca yasal ve halka acik kaynaklar icin
    dusunulmustur; robots.txt ve kullanim sartlarina uyum cagiran
    implementasyonun sorumlulugundadir.
    """

    name: str = "base"

    @abstractmethod
    def get_latest_disclosures(self, symbol: str) -> list[PublicDisclosureItem]:
        ...


class DisabledPublicDisclosureProvider(PublicDisclosureProvider):
    """PUBLIC_DISCLOSURE_PROVIDER=disabled (varsayilan) oldugunda kullanilir.

    Sahte/uydurma bildirim ASLA uretmez; her zaman bos liste doner.
    """

    name = "disabled"

    def get_latest_disclosures(self, symbol: str) -> list[PublicDisclosureItem]:
        return []


def build_public_disclosure_provider(provider_name: str) -> PublicDisclosureProvider:
    """Su an icin yalnizca 'disabled' guvenilir sekilde uygulanmistir.

    'rss' secenegi config'te izin verilse de, guvenilir sekilde
    uygulanabilecek genel-gecer, robots.txt uyumlu, kimlik dogrulama
    gerektirmeyen tek bir kamuya acik RSS kaynagi bu surumde
    dogrulanamadigi icin BILEREK disabled davranisina duser; boylece
    sahte/dogrulanmamis bildirim uretme riski alinmaz.
    """
    return DisabledPublicDisclosureProvider()
