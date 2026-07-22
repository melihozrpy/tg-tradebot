from __future__ import annotations

from app.execution.base_broker_adapter import BaseBrokerAdapter


class LiveTradingDisabledError(Exception):
    """Canli emir gonderimi FAZ 1'de kesinlikle kapalidir."""


class DisabledLiveBroker(BaseBrokerAdapter):
    """Canli emir gonderimini varsayilan olarak engelleyen placeholder adaptor.

    Bu proje spesifikasyonu geregi (madde 1 ve 21): 'Canli emir gonderme
    kodlari varsayilan olarak kapali olacak ve yalnizca ileride
    BrokerAdapter uzerinden eklenebilecektir.' Bu siniftaki metotlar
    KASITLI olarak hicbir gercek emir gondermez, sadece acik bir hata
    firlatir. Gercek bir broker entegrasyonu FAZ 4'te, cift asamali onay,
    maksimum emir tutari, gunluk islem limiti ve manuel Telegram onayi gibi
    korumalarla birlikte eklenebilir; o zamana kadar bu sinif degistirilmemelidir.
    """

    name = "disabled_live_broker"
    is_live = False

    def market_buy(self, symbol: str, quantity: float, market_price: float, **kwargs):
        raise LiveTradingDisabledError(
            "Canli emir gonderimi bu surumde kapalidir. Sadece paper trading kullanilabilir."
        )

    def market_sell(self, symbol: str, quantity: float, market_price: float, **kwargs):
        raise LiveTradingDisabledError(
            "Canli emir gonderimi bu surumde kapalidir. Sadece paper trading kullanilabilir."
        )
