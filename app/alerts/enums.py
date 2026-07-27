from __future__ import annotations

from enum import Enum


class AlarmCondition(str, Enum):
    PRICE_GTE = "PRICE_GTE"
    PRICE_LTE = "PRICE_LTE"
    CROSS_UP = "CROSS_UP"
    CROSS_DOWN = "CROSS_DOWN"
    PRICE_NEAR = "PRICE_NEAR"
    PERCENT_UP_FROM_BASE = "PERCENT_UP_FROM_BASE"
    PERCENT_DOWN_FROM_BASE = "PERCENT_DOWN_FROM_BASE"


class AlarmStatus(str, Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    TRIGGERED = "TRIGGERED"
    SNOOZED = "SNOOZED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    DISABLED = "DISABLED"
    DELETED = "DELETED"
    ERROR = "ERROR"


class AlarmMode(str, Enum):
    ONE_SHOT = "ONE_SHOT"
    PERSISTENT = "PERSISTENT"
    MANUAL_REARM = "MANUAL_REARM"
    RECURRING_CROSS = "RECURRING_CROSS"


class SoundMode(str, Enum):
    TEXT_ONLY = "TEXT_ONLY"
    FIRST_TRIGGER = "FIRST_TRIGGER"
    PERIODIC = "PERIODIC"


class ImportSource(str, Enum):
    TEXT = "TEXT"
    OCR = "OCR"
    CSV = "CSV"
    XLSX = "XLSX"


class DeliveryStatus(str, Enum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    RETRY = "RETRY"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
