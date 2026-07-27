from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.alerts.enums import AlarmCondition, AlarmMode, ImportSource, SoundMode


@dataclass(frozen=True)
class AlarmDraft:
    symbol: str
    target_price: Decimal
    condition: AlarmCondition
    mode: AlarmMode = AlarmMode.PERSISTENT
    repeat_interval_seconds: int = 60
    note: str | None = None
    sound_mode: SoundMode = SoundMode.FIRST_TRIGGER
    source: ImportSource = ImportSource.TEXT
    base_price: Decimal | None = None
    percentage_value: Decimal | None = None
    near_tolerance: Decimal | None = None
    confidence: float = 1.0
    sound_name: str | None = None


@dataclass(frozen=True)
class ParseIssue:
    row_number: int
    raw_text: str
    error: str


@dataclass(frozen=True)
class BulkParseResult:
    valid: tuple[AlarmDraft, ...]
    invalid: tuple[ParseIssue, ...]
    duplicate_rows: tuple[int, ...] = ()


@dataclass(frozen=True)
class PriceObservation:
    symbol: str
    price: Decimal
    provider: str
    data_timestamp: datetime
    retrieved_at: datetime
    freshness_seconds: int
    is_live: bool
    fallback_used: bool
    quality_state: str = "VALID"
    bar_complete: bool = True
    cached: bool = False


@dataclass(frozen=True)
class EvaluationDecision:
    triggered: bool
    state_key: str
    should_rearm: bool = False
    rejection_reason: str | None = None
