from __future__ import annotations

import csv
import io
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from app.alerts.enums import AlarmCondition, AlarmMode, ImportSource
from app.alerts.schemas import AlarmDraft, BulkParseResult, ParseIssue

_HEADER_WORDS = {"hisse", "sembol", "symbol", "ticker", "fiyat", "price", "hedef", "alarm", "koşul", "kosul", "yön", "yon"}
_CONDITION_ALIASES = {
    "ustu": AlarmCondition.PRICE_GTE, "uzeri": AlarmCondition.PRICE_GTE,
    "uzerine cikarsa": AlarmCondition.PRICE_GTE, "yukari": AlarmCondition.PRICE_GTE,
    "buyuk esit": AlarmCondition.PRICE_GTE, ">=": AlarmCondition.PRICE_GTE, "=": AlarmCondition.PRICE_NEAR,
    "alti": AlarmCondition.PRICE_LTE, "altina duserse": AlarmCondition.PRICE_LTE,
    "asagi": AlarmCondition.PRICE_LTE, "kucuk esit": AlarmCondition.PRICE_LTE, "<=": AlarmCondition.PRICE_LTE,
    "yukari keserse": AlarmCondition.CROSS_UP, "yukari kes": AlarmCondition.CROSS_UP,
    "yukari_kes": AlarmCondition.CROSS_UP, "cross_up": AlarmCondition.CROSS_UP,
    "asagi keserse": AlarmCondition.CROSS_DOWN, "asagi kes": AlarmCondition.CROSS_DOWN,
    "asagi_kes": AlarmCondition.CROSS_DOWN, "cross_down": AlarmCondition.CROSS_DOWN,
    "esit": AlarmCondition.PRICE_NEAR, "yaklasik": AlarmCondition.PRICE_NEAR,
    "hedefe gelirse": AlarmCondition.PRICE_NEAR, "fiyat": AlarmCondition.PRICE_NEAR,
}


def normalize_turkish(value: str) -> str:
    text = value.strip().casefold().replace("ı", "i")
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def normalize_symbol(value: str) -> str:
    symbol = value.strip().upper().removesuffix(".IS")
    if not re.fullmatch(r"[A-Z0-9]{3,12}", symbol):
        raise ValueError("geçersiz BIST sembolü")
    return symbol


def parse_decimal(value: str) -> Decimal:
    cleaned = value.strip().replace("₺", "").replace("TL", "").replace("tl", "")
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        number = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("geçersiz fiyat") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError("fiyat sıfırdan büyük olmalı")
    return number.quantize(Decimal("0.0001"))


def parse_condition(value: str) -> AlarmCondition:
    normalized = re.sub(r"\s+", " ", normalize_turkish(value).replace("-", " ")).strip()
    if normalized in _CONDITION_ALIASES:
        return _CONDITION_ALIASES[normalized]
    for alias in sorted(_CONDITION_ALIASES, key=len, reverse=True):
        if normalized.endswith(alias):
            return _CONDITION_ALIASES[alias]
    raise ValueError("alarm koşulu anlaşılamadı")


def _is_header(line: str) -> bool:
    words = {normalize_turkish(x) for x in re.split(r"[\s,;|]+", line) if x.strip()}
    return len(words & _HEADER_WORDS) >= 2


def _split_line(line: str) -> tuple[str, str, str, list[str]]:
    raw = line.strip()
    if not raw:
        raise ValueError("boş satır")
    if ";" in raw or "|" in raw:
        parts = [p.strip() for p in re.split(r"[;|]", raw) if p.strip()]
    elif raw.count(",") >= 2:
        parts = next(csv.reader(io.StringIO(raw), skipinitialspace=True))
    else:
        match = re.match(r"^([A-Za-z0-9.]+)\s+([0-9.,]+)\s+(.+)$", raw)
        if not match:
            raise ValueError("sembol, fiyat veya koşul eksik")
        return match.group(1), match.group(2), match.group(3), []
    if len(parts) < 3:
        raise ValueError("en az sembol, fiyat ve koşul gerekli")
    return parts[0], parts[1], parts[2], parts[3:]


def parse_alarm_line(line: str, *, source: ImportSource = ImportSource.TEXT) -> AlarmDraft:
    percent_match = re.match(
        r"^([A-Za-z0-9.]+)\s+([0-9.,]+)\s+([+-])\s*([0-9.,]+)\s*%\s*(.*)$",
        line.strip(),
    )
    if percent_match:
        symbol = normalize_symbol(percent_match.group(1))
        base = parse_decimal(percent_match.group(2))
        percentage = parse_decimal(percent_match.group(4))
        direction = percent_match.group(3)
        condition = (
            AlarmCondition.PERCENT_UP_FROM_BASE
            if direction == "+"
            else AlarmCondition.PERCENT_DOWN_FROM_BASE
        )
        factor = Decimal("1") + (percentage / Decimal("100")) * (Decimal("1") if direction == "+" else Decimal("-1"))
        target = (base * factor).quantize(Decimal("0.0001"))
        note = percent_match.group(5).strip()[:500] or None
        return AlarmDraft(
            symbol=symbol,
            target_price=target,
            condition=condition,
            note=note,
            source=source,
            base_price=base,
            percentage_value=percentage,
        )
    symbol_raw, price_raw, condition_raw, extras = _split_line(line)
    repeat = 60
    mode = AlarmMode.PERSISTENT
    note = None
    if extras:
        mode_text = normalize_turkish(extras[0])
        if mode_text in {"tek sefer", "tek_sefer", "one shot", "one_shot"}:
            mode = AlarmMode.ONE_SHOT
        elif mode_text in {"manuel", "manual_rearm", "yeniden kur"}:
            mode = AlarmMode.MANUAL_REARM
    if len(extras) > 1 and extras[1].strip():
        try:
            repeat = int(extras[1])
        except ValueError as exc:
            raise ValueError("tekrar aralığı tam sayı olmalı") from exc
    if len(extras) > 2:
        note = " ".join(extras[2:]).strip()[:500] or None
    return AlarmDraft(
        symbol=normalize_symbol(symbol_raw), target_price=parse_decimal(price_raw),
        condition=parse_condition(condition_raw), mode=mode,
        repeat_interval_seconds=repeat, note=note, source=source,
    )


def parse_bulk_text(text: str, *, maximum_rows: int = 250, source: ImportSource = ImportSource.TEXT) -> BulkParseResult:
    lines = [line.strip() for line in text.replace("\r", "").split("\n") if line.strip()]
    if len(lines) > maximum_rows:
        raise ValueError(f"Tek seferde en fazla {maximum_rows} alarm işlenebilir.")
    valid: list[AlarmDraft] = []
    invalid: list[ParseIssue] = []
    duplicate_rows: list[int] = []
    seen: set[tuple] = set()
    for row_number, line in enumerate(lines, 1):
        if _is_header(line):
            continue
        try:
            draft = parse_alarm_line(line, source=source)
            key = (draft.symbol, draft.target_price, draft.condition.value, draft.mode.value)
            if key in seen:
                duplicate_rows.append(row_number)
                continue
            seen.add(key); valid.append(draft)
        except ValueError as exc:
            invalid.append(ParseIssue(row_number, line[:300], str(exc)))
    return BulkParseResult(tuple(valid), tuple(invalid), tuple(duplicate_rows))
