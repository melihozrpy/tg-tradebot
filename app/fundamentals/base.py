from __future__ import annotations

from abc import ABC, abstractmethod

from app.fundamentals.models import FundamentalSnapshot


class FundamentalDataError(RuntimeError):
    """Base error safe to expose without leaking credentials or response bodies."""


class ProviderConfigurationError(FundamentalDataError):
    pass


class ProviderUnavailableError(FundamentalDataError):
    pass


class ProviderResponseError(FundamentalDataError):
    pass


class CrossCheckMismatchError(FundamentalDataError):
    pass


class FundamentalDataProvider(ABC):
    name = "abstract"

    @abstractmethod
    def fetch(self, symbol: str) -> FundamentalSnapshot:
        """Return validated data or raise; never return an empty/mock snapshot."""


class DisabledFundamentalDataProvider(FundamentalDataProvider):
    name = "disabled"

    def __init__(self, reason: str = "Temel veri sağlayıcısı yapılandırılmadı.") -> None:
        self.reason = reason

    def fetch(self, symbol: str) -> FundamentalSnapshot:
        raise ProviderUnavailableError(self.reason)
