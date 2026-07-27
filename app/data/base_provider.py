from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd


class DataUnavailableError(Exception):
    """Veri saglayicisi gerekli veriyi saglayamadiginda firlatilir.

    Bu proje hicbir zaman eksik veriyi uydurmaz; boyle durumlarda
    cagiran taraf fail-closed davranmali ve sinyal uretmemelidir.
    """


@dataclass
class DataFreshness:
    symbol: str
    timeframe: str
    last_timestamp: Optional[datetime]
    is_fresh: bool
    max_allowed_lag_minutes: int
    provider: str


class BaseMarketDataProvider(ABC):
    """Tum piyasa veri saglayicilarinin uygulamasi gereken soyut arayuz."""

    name: str = "base"
    # Only a contracted adapter that validates exchange transaction metadata
    # may opt in. Free/delayed OHLC providers remain analysis-only even when
    # their timestamps happen to be recent.
    supports_verified_live_transactions: bool = False

    @abstractmethod
    def get_quote(self, symbol: str) -> dict:
        ...

    @abstractmethod
    def get_ohlcv(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        ...

    @abstractmethod
    def get_index_data(self, index_symbol: str, timeframe: str) -> pd.DataFrame:
        ...

    def get_sector_data(self, sector_symbol: str, timeframe: str) -> pd.DataFrame:
        raise DataUnavailableError(
            f"Sektor verisi '{self.name}' saglayicisinda mevcut degil (FAZ 1)."
        )

    def get_market_breadth(self) -> dict:
        raise DataUnavailableError(
            f"Piyasa genisligi verisi '{self.name}' saglayicisinda mevcut degil (FAZ 1)."
        )

    def get_corporate_actions(self, symbol: str) -> list:
        raise DataUnavailableError(
            f"Kurumsal aksiyon verisi '{self.name}' saglayicisinda mevcut degil (FAZ 1)."
        )

    @abstractmethod
    def is_market_open(self) -> bool:
        ...

    @abstractmethod
    def get_data_freshness(self, symbol: str, timeframe: str) -> DataFreshness:
        ...

    @abstractmethod
    def health_check(self) -> dict:
        ...


class KapProvider(ABC):
    @abstractmethod
    def get_latest_disclosures(self, symbol: str) -> list:
        ...

    @abstractmethod
    def get_disclosure_detail(self, disclosure_id: str) -> dict:
        ...

    @abstractmethod
    def classify_disclosure(self, disclosure: dict) -> str:
        ...

    @abstractmethod
    def get_upcoming_financial_dates(self, symbol: str) -> list:
        ...


class DisabledKapProvider(KapProvider):
    """KAP_PROVIDER=disabled oldugunda kullanilir. Hicbir veri uydurmaz."""

    name = "disabled"

    def get_latest_disclosures(self, symbol: str) -> list:
        return []

    def get_disclosure_detail(self, disclosure_id: str) -> dict:
        raise DataUnavailableError("KAP saglayicisi devre disi (FAZ 1).")

    def classify_disclosure(self, disclosure: dict) -> str:
        return "unavailable"

    def get_upcoming_financial_dates(self, symbol: str) -> list:
        return []


class BrokerFlowProvider(ABC):
    @abstractmethod
    def get_broker_distribution(self, symbol: str, date_range: tuple) -> dict:
        ...

    @abstractmethod
    def get_net_buyer_seller(self, symbol: str) -> dict:
        ...

    @abstractmethod
    def calculate_broker_cost_estimate(self, symbol: str, broker: str) -> Optional[float]:
        ...

    @abstractmethod
    def calculate_accumulation_score(self, symbol: str) -> Optional[float]:
        ...

    @abstractmethod
    def calculate_distribution_score(self, symbol: str) -> Optional[float]:
        ...


class DisabledBrokerFlowProvider(BrokerFlowProvider):
    """BROKER_FLOW_PROVIDER=disabled oldugunda kullanilir.

    Gercek zamanli lisansli veri olmadan kurum dagilimi varmis gibi
    davranmak yasaktir; bu yuzden bu saglayici her zaman 'unavailable'
    doner.
    """

    name = "disabled"

    def get_broker_distribution(self, symbol: str, date_range: tuple) -> dict:
        return {"status": "unavailable", "symbol": symbol}

    def get_net_buyer_seller(self, symbol: str) -> dict:
        return {"status": "unavailable", "symbol": symbol}

    def calculate_broker_cost_estimate(self, symbol: str, broker: str) -> Optional[float]:
        return None

    def calculate_accumulation_score(self, symbol: str) -> Optional[float]:
        return None

    def calculate_distribution_score(self, symbol: str) -> Optional[float]:
        return None


class FundamentalProvider(ABC):
    @abstractmethod
    def get_income_statement(self, symbol: str) -> dict:
        ...

    @abstractmethod
    def get_balance_sheet(self, symbol: str) -> dict:
        ...

    @abstractmethod
    def get_cash_flow(self, symbol: str) -> dict:
        ...

    @abstractmethod
    def get_ratios(self, symbol: str) -> dict:
        ...

    @abstractmethod
    def get_quarterly_growth(self, symbol: str) -> dict:
        ...


class DisabledFundamentalProvider(FundamentalProvider):
    name = "disabled"

    def get_income_statement(self, symbol: str) -> dict:
        return {"status": "unavailable", "symbol": symbol}

    def get_balance_sheet(self, symbol: str) -> dict:
        return {"status": "unavailable", "symbol": symbol}

    def get_cash_flow(self, symbol: str) -> dict:
        return {"status": "unavailable", "symbol": symbol}

    def get_ratios(self, symbol: str) -> dict:
        return {"status": "unavailable", "symbol": symbol}

    def get_quarterly_growth(self, symbol: str) -> dict:
        return {"status": "unavailable", "symbol": symbol}
