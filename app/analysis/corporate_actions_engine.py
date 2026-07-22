from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Optional

import pandas as pd

from app.utils.financial_formatter import finite_float


ACTION_ALIASES = {
    "split": "Hisse bölünmesi", "stock_split": "Hisse bölünmesi",
    "reverse_split": "Ters bölünme", "bonus": "Bedelsiz sermaye artırımı",
    "bedelsiz": "Bedelsiz sermaye artırımı", "rights_issue": "Bedelli sermaye artırımı",
    "bedelli": "Bedelli sermaye artırımı", "dividend": "Temettü", "temettu": "Temettü",
    "merger": "Birleşme", "shares_change": "Pay sayısı değişimi",
    "capital_change": "Çıkarılmış sermaye değişimi", "capital_ceiling_change": "Sermaye tavanı değişimi",
}


@dataclass
class CorporateActionEvent:
    symbol: str
    corporate_action_type: str
    effective_date: Optional[date]
    raw_price: Optional[float] = None
    adjusted_price: Optional[float] = None
    adjustment_factor: Optional[float] = None
    cash_amount: Optional[float] = None
    share_ratio: Optional[float] = None
    old_share_count: Optional[float] = None
    new_share_count: Optional[float] = None
    source: str = "provider"
    notes: list[str] = field(default_factory=list)


def _date(value: Any) -> Optional[date]:
    if value is None:
        return None
    try:
        return pd.Timestamp(value).date()
    except Exception:  # noqa: BLE001
        return None


def _ratio(value: Any) -> Optional[float]:
    if isinstance(value, str) and ":" in value:
        left, right = value.split(":", 1)
        a, b = finite_float(left), finite_float(right)
        return a / b if a is not None and b not in (None, 0) else None
    return finite_float(value)


def normalize_corporate_actions(symbol: str, actions: Optional[Iterable[dict]]) -> list[CorporateActionEvent]:
    result: list[CorporateActionEvent] = []
    for raw in actions or []:
        raw_type = str(raw.get("type") or raw.get("action_type") or "unknown").strip().lower()
        action_type = ACTION_ALIASES.get(raw_type, str(raw.get("label") or raw_type or "Bilinmeyen işlem"))
        ratio = _ratio(raw.get("ratio") or raw.get("split_ratio") or raw.get("share_ratio"))
        factor = finite_float(raw.get("adjustment_factor"))
        if factor is None and ratio and ratio > 0 and action_type in {
            "Hisse bölünmesi", "Ters bölünme", "Bedelsiz sermaye artırımı",
        }:
            factor = 1.0 / ratio
        raw_price = finite_float(raw.get("raw_price") or raw.get("price_before"))
        adjusted_price = finite_float(raw.get("adjusted_price"))
        if adjusted_price is None and raw_price is not None and factor is not None:
            adjusted_price = raw_price * factor
        result.append(
            CorporateActionEvent(
                symbol=symbol.upper(), corporate_action_type=action_type,
                effective_date=_date(raw.get("effective_date") or raw.get("date") or raw.get("timestamp")),
                raw_price=raw_price, adjusted_price=adjusted_price, adjustment_factor=factor,
                cash_amount=finite_float(raw.get("amount") or raw.get("cash_amount")),
                share_ratio=ratio, old_share_count=finite_float(raw.get("old_share_count")),
                new_share_count=finite_float(raw.get("new_share_count")),
                source=str(raw.get("source") or "provider"),
                notes=list(raw.get("notes") or []),
            )
        )
    return sorted(result, key=lambda item: item.effective_date or date.min)


def apply_price_adjustments(
    df: pd.DataFrame,
    events: Iterable[CorporateActionEvent],
    *,
    mode: str = "adjusted",
) -> pd.DataFrame:
    if mode not in {"adjusted", "raw"}:
        raise ValueError("Fiyat düzeltme modu 'adjusted' veya 'raw' olmalı.")
    out = df.copy().sort_values("timestamp").reset_index(drop=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    for col in ("open", "high", "low", "close"):
        out[f"raw_{col}"] = pd.to_numeric(out[col], errors="coerce")
        out[f"adjusted_{col}"] = out[f"raw_{col}"]
    out["adjustment_factor"] = 1.0
    out["corporate_action_type"] = None
    out["effective_date"] = pd.Series(pd.NaT, index=out.index, dtype="datetime64[ns, UTC]")
    for event in events:
        if event.effective_date is None:
            continue
        date_ts = pd.Timestamp(event.effective_date, tz="UTC")
        if event.adjustment_factor is not None and event.adjustment_factor > 0:
            mask = out["timestamp"] < date_ts
            out.loc[mask, "adjustment_factor"] *= event.adjustment_factor
            for col in ("open", "high", "low", "close"):
                out.loc[mask, f"adjusted_{col}"] *= event.adjustment_factor
        same_day = out["timestamp"].dt.date == event.effective_date
        out.loc[same_day, "corporate_action_type"] = event.corporate_action_type
        out.loc[same_day, "effective_date"] = date_ts
    for col in ("open", "high", "low", "close"):
        out[col] = out[f"adjusted_{col}"] if mode == "adjusted" else out[f"raw_{col}"]
    out["raw_price"] = out["raw_close"]
    out["adjusted_price"] = out["adjusted_close"]
    return out


def corporate_action_for_date(events: Iterable[CorporateActionEvent], event_date: date) -> Optional[CorporateActionEvent]:
    return next((event for event in events if event.effective_date == event_date), None)


def classify_price_gap(events: Iterable[CorporateActionEvent], event_date: date) -> dict:
    event = corporate_action_for_date(events, event_date)
    if event is None:
        return {"excluded_from_gap_alarm": False, "kind": "normal_gap", "note": "Kurumsal işlem eşleşmesi yok"}
    if event.corporate_action_type == "Temettü":
        return {"excluded_from_gap_alarm": True, "kind": "dividend_gap", "note": "Temettü kaynaklı fiyat boşluğu"}
    if event.corporate_action_type in {"Hisse bölünmesi", "Ters bölünme", "Bedelsiz sermaye artırımı"}:
        return {"excluded_from_gap_alarm": True, "kind": "capital_adjustment", "note": f"{event.corporate_action_type} kaynaklı fiyat düzeltmesi"}
    return {"excluded_from_gap_alarm": False, "kind": "corporate_action", "note": event.corporate_action_type}
