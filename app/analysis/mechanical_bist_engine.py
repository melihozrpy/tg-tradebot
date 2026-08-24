from __future__ import annotations

"""Closed-candle, multi-timeframe BIST structural setup engine.

The module returns a machine-readable payload for the Telegram bot.  It does
not place orders and deliberately returns ``WAIT`` where a mechanical trigger,
liquidity gate, or a usable structural target is absent.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pandas as pd

from app.analysis.liquidity_engine import compute_liquidity
from app.data.base_provider import BaseMarketDataProvider

Bias = Literal["UP", "DOWN", "NEUTRAL"]


_BIAS_LABELS = {"UP": "YÜKSELİŞ", "DOWN": "DÜŞÜŞ", "NEUTRAL": "YATAY / KARARSIZ"}
_LEVEL_LABELS = {
    "daily_open": "Gün açılışı",
    "previous_day_high": "Dünkü tepe",
    "previous_day_low": "Dünkü dip",
    "previous_day_close": "Dünkü kapanış",
    "weekly_open": "Haftalık açılış",
    "monthly_open": "Aylık açılış",
    "twenty_day_high": "20 günlük tepe",
    "five_day_low": "5 günlük dip",
    "previous_week_high": "Geçen hafta tepe",
    "previous_week_low": "Geçen hafta dip",
    "previous_month_high": "Geçen ay tepe",
    "previous_month_low": "Geçen ay dip",
}


def _price_text(value: float | int | None) -> str:
    """Render prices consistently for Telegram without exposing raw JSON values."""

    if value is None:
        return "—"
    return f"{float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _bias_card(label: str, bias: str) -> str:
    icon = "🟢" if bias == "UP" else "🔴" if bias == "DOWN" else "🟡"
    return f"{icon} <b>{label}</b>: {_BIAS_LABELS.get(bias, 'BELİRSİZ')}"


def format_mechanical_bist_report(payload: dict[str, Any], *, timezone_name: str = "Europe/Istanbul") -> str:
    """Turn the coordinate-rich engine result into a compact Telegram trading card.

    The raw JSON remains an internal contract for tests and other modules.  End
    users receive a readable, closed-candle plan instead of developer output.
    """

    symbol = escape(str(payload.get("symbol", "BIST")))
    current = float(payload.get("current_price") or 0.0)
    generated = pd.Timestamp(payload.get("generated_at", datetime.now(timezone.utc)))
    if generated.tzinfo is None:
        generated = generated.tz_localize("UTC")
    local = generated.tz_convert(ZoneInfo(timezone_name))
    hierarchy = payload.get("timeframe_hierarchy") or {}
    daily = hierarchy.get("1D") or {}
    four_hour = hierarchy.get("4H") or {}
    weekly = hierarchy.get("1W") or {}
    monthly = hierarchy.get("1M") or {}
    levels = [row for row in (payload.get("structural_levels") or []) if isinstance(row, dict) and row.get("y") is not None]
    below = sorted((row for row in levels if float(row["y"]) < current), key=lambda row: float(row["y"]), reverse=True)[:2]
    above = sorted((row for row in levels if float(row["y"]) > current), key=lambda row: float(row["y"]))[:2]
    liquidity = payload.get("liquidity") or {}
    gate_passed = liquidity.get("gate") == "PASS"
    status_active = payload.get("status") == "ACTIVE" and isinstance(payload.get("signal"), dict)

    lines = [
        f"┏━━ ⚙️ <b>MEKANİK İŞLEM PLANI • {symbol}</b> ━━┓",
        f"🕒 {local:%d.%m.%Y • %H:%M} TSİ  •  Kapanmış mum analizi",
        "",
        ("🟢 <b>SETUP AKTİF</b>  •  Mekanik teyit tamamlandı" if status_active else "🟡 <b>ŞU AN İŞLEM YOK</b>  •  Teyit bekleniyor"),
        f"💰 <b>GÜNCEL FİYAT:</b> {_price_text(current)} TL",
        "",
        "<b>🧭 ÇOKLU ZAMAN DİLİMİ</b>",
        _bias_card("Aylık", str(monthly.get("bias", "NEUTRAL"))),
        _bias_card("Haftalık", str(weekly.get("bias", "NEUTRAL"))),
        _bias_card("Günlük", str(daily.get("bias", "NEUTRAL"))),
        _bias_card("4 Saat", str(four_hour.get("bias", "NEUTRAL"))),
        f"▫️ 4S aralığı: {_price_text(four_hour.get('swing_low'))} – {_price_text(four_hour.get('swing_high'))} TL",
    ]

    if below or above:
        lines.extend(["", "<b>📍 KRİTİK FİYAT BÖLGELERİ</b>"])
        if below:
            lines.append("🟩 <b>Destek:</b> " + "  •  ".join(
                f"{_price_text(row['y'])} ({_LEVEL_LABELS.get(str(row.get('label')), 'Yapısal seviye')})" for row in below
            ))
        if above:
            lines.append("🟥 <b>Direnç:</b> " + "  •  ".join(
                f"{_price_text(row['y'])} ({_LEVEL_LABELS.get(str(row.get('label')), 'Yapısal seviye')})" for row in above
            ))

    zones = [row for row in (payload.get("zones") or []) if isinstance(row, dict) and row.get("low") is not None and row.get("high") is not None]
    zones.sort(key=lambda row: abs(((float(row["low"]) + float(row["high"])) / 2) - current))
    if zones:
        lines.extend(["", "<b>🔶 YAKIN OB / FVG BÖLGELERİ</b>"])
        for zone in zones[:3]:
            direction = "Talep" if zone.get("direction") == "BULLISH" else "Arz"
            icon = "🟢" if direction == "Talep" else "🔴"
            lines.append(f"{icon} {zone.get('kind', 'Bölge')} • {direction}: {_price_text(zone['low'])} – {_price_text(zone['high'])} TL")

    if status_active:
        signal = payload["signal"]
        direction = "LONG / AL" if signal.get("direction") == "BUY" else "SAT / KORUMA"
        lines.extend([
            "",
            "<b>🎯 TEYİTLİ İŞLEM PLANI</b>",
            f"{('🟢' if signal.get('direction') == 'BUY' else '🔴')} <b>Yön:</b> {direction}  •  {signal.get('entry_type', 'MEKANİK')} teyidi",
            f"📥 <b>Giriş:</b> {_price_text(signal.get('entry_price'))} TL",
            f"🛑 <b>Geçersizlik / Stop:</b> {_price_text(signal.get('stop_loss'))} TL",
            f"🎯 <b>Hedefler:</b> {_price_text(signal.get('target_1'))}  •  {_price_text(signal.get('target_2'))}  •  {_price_text(signal.get('target_3'))} TL",
            f"⚖️ <b>Risk / Getiri:</b> {signal.get('risk_reward', '—')}  •  Risk: %{float(signal.get('position_risk_percent') or 0):.2f}",
            f"✅ <b>Teyit:</b> {escape(str(signal.get('reason', 'Kapanmış mumlarla doğrulandı.')))}",
        ])
    else:
        wait_notes: list[str] = []
        if not gate_passed:
            wait_notes.append("Likidite kalite kapısı geçmedi; yeni plan oluşturulmadı.")
        if daily.get("bias") == "NEUTRAL" or daily.get("bias") != four_hour.get("bias"):
            wait_notes.append("Günlük ve 4 saatlik yön henüz aynı tarafta değil.")
        if not wait_notes:
            wait_notes.append("15dk sweep veya 1 saatlik kırılım-retest kapanışı henüz oluşmadı.")
        lines.extend([
            "",
            "<b>⏳ BEKLENECEK TEYİT</b>",
            *[f"• {escape(note)}" for note in wait_notes],
            "📌 Güncel fiyat giriş emri değildir; yalnız seviye retesti ve 15dk kapanışından sonra plan aktifleşir.",
        ])

    liquidity_value = liquidity.get("score")
    liquidity_text = f"{float(liquidity_value):.0f}/100" if liquidity_value is not None else "doğrulanamadı"
    liquidity_state = "UYGUN" if gate_passed else "BLOKE"
    lines.extend([
        "",
        f"💧 <b>Likidite:</b> {liquidity_text}  •  <b>Kalite kapısı:</b> {liquidity_state}",
        "<i>Bu teknik senaryo yatırım tavsiyesi değildir; emirden önce KAP/haber ve güncel derinlik kontrol edilmelidir.</i>",
    ])
    return "\n".join(lines)[:4096]


@dataclass(frozen=True)
class Point:
    label: str
    x: str
    y: float


@dataclass(frozen=True)
class Zone:
    kind: str
    direction: str
    low: float
    high: float
    start_x: str
    end_x: str


def _utc_text(value: Any) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC").isoformat()


def _prepared(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        raise ValueError("OHLCV verisi eksik")
    data = frame.loc[:, ["timestamp", "open", "high", "low", "close", "volume"]].copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True, errors="coerce")
    data = data.dropna().sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)
    if len(data) < 25:
        raise ValueError("Yeterli kapanmış mum yok")
    return data


def _resample(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    data = frame.set_index("timestamp")
    result = data.resample(rule).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"),
    ).dropna().reset_index()
    return result


def _four_hour(frame: pd.DataFrame) -> pd.DataFrame:
    # Four source 1H candles are required.  Incomplete 4H blocks are ignored.
    data = frame.copy()
    data["session"] = data["timestamp"].dt.tz_convert("Europe/Istanbul").dt.date
    data["bucket"] = data.groupby("session").cumcount() // 4
    count = data.groupby(["session", "bucket"], sort=False).size()
    complete = count[count >= 4].index
    result = data.groupby(["session", "bucket"], sort=False).agg(
        timestamp=("timestamp", "last"), open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
    )
    return result.loc[complete].reset_index(drop=True) if len(complete) else result.iloc[0:0].reset_index()


def _bar_bias(frame: pd.DataFrame, count: int) -> Bias:
    if len(frame) < count:
        return "NEUTRAL"
    sample = frame.tail(count)
    direction: list[str] = []
    for row in sample.itertuples(index=False):
        span = max(float(row.high) - float(row.low), 1e-9)
        body = float(row.close) - float(row.open)
        if abs(body) / span < .20:
            return "NEUTRAL"
        direction.append("UP" if body > 0 else "DOWN")
    return direction[0] if len(set(direction)) == 1 else "NEUTRAL"


def _atr(frame: pd.DataFrame, period: int = 14) -> float:
    prev_close = frame["close"].shift(1)
    tr = pd.concat(
        [(frame["high"] - frame["low"]).abs(), (frame["high"] - prev_close).abs(), (frame["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    value = tr.rolling(period, min_periods=period).mean().iloc[-1]
    return float(value) if pd.notna(value) and value > 0 else 0.0


def _fvg_zones(frame: pd.DataFrame, *, lookback: int = 60) -> list[Zone]:
    data = frame.tail(lookback).reset_index(drop=True)
    zones: list[Zone] = []
    for index in range(2, len(data)):
        left, current = data.iloc[index - 2], data.iloc[index]
        if float(current.low) > float(left.high):
            zones.append(Zone("FVG", "BULLISH", float(left.high), float(current.low), _utc_text(left.timestamp), _utc_text(current.timestamp)))
        elif float(current.high) < float(left.low):
            zones.append(Zone("FVG", "BEARISH", float(current.high), float(left.low), _utc_text(left.timestamp), _utc_text(current.timestamp)))
    return zones[-4:]


def _order_block(frame: pd.DataFrame) -> Zone | None:
    data = frame.tail(5).copy()
    if data.empty:
        return None
    data["body"] = (data["close"] - data["open"]).abs()
    row = data.loc[data["body"].idxmax()]
    return Zone(
        "OB",
        "BULLISH" if float(row.close) >= float(row.open) else "BEARISH",
        min(float(row.open), float(row.close)), max(float(row.open), float(row.close)),
        _utc_text(row.timestamp), _utc_text(frame.iloc[-1].timestamp),
    )


def _level_points(daily: pd.DataFrame, weekly: pd.DataFrame, monthly: pd.DataFrame) -> list[Point]:
    current, previous = daily.iloc[-1], daily.iloc[-2]
    levels = [
        Point("daily_open", _utc_text(current.timestamp), float(current.open)),
        Point("previous_day_high", _utc_text(previous.timestamp), float(previous.high)),
        Point("previous_day_low", _utc_text(previous.timestamp), float(previous.low)),
        Point("previous_day_close", _utc_text(previous.timestamp), float(previous.close)),
        Point("weekly_open", _utc_text(weekly.iloc[-1].timestamp), float(weekly.iloc[-1].open)),
        Point("monthly_open", _utc_text(monthly.iloc[-1].timestamp), float(monthly.iloc[-1].open)),
        Point("twenty_day_high", _utc_text(daily.iloc[-20:].iloc[-1].timestamp), float(daily.iloc[-20:]["high"].max())),
        Point("five_day_low", _utc_text(daily.iloc[-5:].iloc[-1].timestamp), float(daily.iloc[-5:]["low"].min())),
    ]
    if len(weekly) >= 2:
        prior_week = weekly.iloc[-2]
        levels.extend((
            Point("previous_week_high", _utc_text(prior_week.timestamp), float(prior_week.high)),
            Point("previous_week_low", _utc_text(prior_week.timestamp), float(prior_week.low)),
        ))
    if len(monthly) >= 2:
        prior_month = monthly.iloc[-2]
        levels.extend((
            Point("previous_month_high", _utc_text(prior_month.timestamp), float(prior_month.high)),
            Point("previous_month_low", _utc_text(prior_month.timestamp), float(prior_month.low)),
        ))
    return levels


def _targets(points: list[Point], entry: float, direction: str) -> list[float]:
    candidates = sorted({point.y for point in points if (point.y > entry if direction == "BUY" else point.y < entry)})
    return candidates[:3] if direction == "BUY" else list(reversed(candidates))[:3]


def _mechanical_signal(
    *, daily_bias: Bias, four_hour_bias: Bias, hourly: pd.DataFrame, minute_15: pd.DataFrame,
    points: list[Point], liquidity_score: float, minimum_liquidity: float, risk_percent: float,
) -> dict[str, Any] | None:
    if daily_bias == "NEUTRAL" or four_hour_bias != daily_bias or liquidity_score < minimum_liquidity:
        return None
    atr_value = _atr(minute_15)
    if atr_value <= 0 or len(hourly) < 2 or len(minute_15) < 4:
        return None
    last = minute_15.iloc[-1]
    prior_hour = hourly.iloc[-2]
    recent = minute_15.tail(4)
    direction = "BUY" if daily_bias == "UP" else "SELL"
    reference = float(prior_hour.high if direction == "BUY" else prior_hour.low)
    # A completed 15m break must retest the exact previous 1H boundary and
    # close back through it.  It never treats the last price as an entry.
    retest = (
        float(last.low) <= reference + atr_value * .12 and float(last.close) > reference
        if direction == "BUY"
        else float(last.high) >= reference - atr_value * .12 and float(last.close) < reference
    )
    sweep_reference = next((p.y for p in points if p.label == ("previous_day_low" if direction == "BUY" else "previous_day_high")), None)
    swept = (
        sweep_reference is not None
        and ((float(last.low) < sweep_reference and float(last.close) > sweep_reference) if direction == "BUY" else (float(last.high) > sweep_reference and float(last.close) < sweep_reference))
    )
    if not retest and not swept:
        return None
    entry_type = "SWEEP" if swept else "BREAK_RETEST"
    entry = float(sweep_reference if swept else reference)
    stop = (float(recent["low"].min()) - atr_value * .12) if direction == "BUY" else (float(recent["high"].max()) + atr_value * .12)
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    targets = _targets(points, entry, direction)
    if not targets:
        return None
    target_1 = targets[0]
    reward = abs(target_1 - entry)
    min_rr = 1.5 if entry_type == "SWEEP" else 2.0
    if reward / risk < min_rr:
        return None
    while len(targets) < 3:
        targets.append(targets[-1])
    return {
        "timeframe": "15min",
        "direction": direction,
        "entry_price": round(entry, 4),
        "entry_type": entry_type,
        "stop_loss": round(stop, 4),
        "target_1": round(targets[0], 4),
        "target_2": round(targets[1], 4),
        "target_3": round(targets[2], 4),
        "risk_reward": f"1:{reward / risk:.2f}",
        "confidence": "HIGH" if liquidity_score >= 75 and swept else "MEDIUM",
        "reason": f"Daily {daily_bias} + 4H {four_hour_bias}; 15dk {entry_type} mekanik teyidi",
        "liquidity_window": "HIGH" if liquidity_score >= 65 else "MEDIUM",
        "setup_time": _utc_text(last.timestamp),
        "max_position_size_pips": None,
        "position_risk_percent": risk_percent,
        "invalidation_level": round(stop, 4),
        "coordinates": {
            "entry": {"x": _utc_text(last.timestamp), "y": round(entry, 4)},
            "stop": {"x": _utc_text(last.timestamp), "y": round(stop, 4)},
            "targets": [{"x": _utc_text(last.timestamp), "y": round(target, 4)} for target in targets[:3]],
        },
    }


def analyze_mechanical_bist_setup(
    symbol: str, *, provider: BaseMarketDataProvider, settings, now: datetime | None = None,
) -> dict[str, Any]:
    """Produce a JSON-serializable closed-candle analysis for one BIST symbol."""

    end = now or datetime.now(timezone.utc)
    daily = _prepared(provider.get_ohlcv(symbol, "1d", end - timedelta(days=620), end))
    hourly = _prepared(provider.get_ohlcv(symbol, "1h", end - timedelta(days=130), end))
    minute_15 = _prepared(provider.get_ohlcv(symbol, "15m", end - timedelta(days=58), end))
    four_hour = _four_hour(hourly)
    weekly, monthly = _resample(daily, "W-FRI"), _resample(daily, "MS")
    if len(four_hour) < 3 or len(weekly) < 2 or len(monthly) < 2:
        raise ValueError("4H/haftalık/aylık yapı için yeterli kapanmış mum yok")

    daily_bias, four_hour_bias = _bar_bias(daily, 2), _bar_bias(four_hour, 2)
    weekly_bias, monthly_bias = _bar_bias(weekly, 3), _bar_bias(monthly, 3)
    points = _level_points(daily, weekly, monthly)
    zones = [*_fvg_zones(four_hour), *filter(None, (_order_block(four_hour),))]
    liquidity = compute_liquidity(daily)
    liquidity_score = liquidity.score if liquidity.available else 0.0
    minimum_liquidity = float(getattr(settings, "mechanical_setup_minimum_liquidity_score", 65.0))
    signal = _mechanical_signal(
        daily_bias=daily_bias, four_hour_bias=four_hour_bias, hourly=hourly, minute_15=minute_15,
        points=points, liquidity_score=liquidity_score, minimum_liquidity=minimum_liquidity,
        risk_percent=float(getattr(settings, "mechanical_setup_risk_per_trade_percent", 0.25)),
    )
    current = float(minute_15.iloc[-1].close)
    reason = (
        "Mekanik tetik henüz oluşmadı; seviye kırılımı/retesti ve 15dk kapanışı bekleniyor."
        if signal is None else None
    )
    return {
        "schema_version": "bist-mechanical-setup/v1",
        "symbol": symbol.upper().removesuffix(".IS"),
        "generated_at": end.isoformat(),
        "status": "ACTIVE" if signal else "WAIT",
        "current_price": round(current, 4),
        "timeframe_hierarchy": {
            "1M": {"bias": monthly_bias, "key_levels": [asdict(point) for point in points if "month" in point.label]},
            "1W": {"bias": weekly_bias, "key_levels": [asdict(point) for point in points if "week" in point.label]},
            "1D": {"bias": daily_bias, "key_levels": [asdict(point) for point in points if "day" in point.label or "twenty" in point.label or "five" in point.label]},
            "4H": {"bias": four_hour_bias, "swing_high": round(float(four_hour.tail(5)["high"].max()), 4), "swing_low": round(float(four_hour.tail(5)["low"].min()), 4)},
        },
        "structural_levels": [asdict(point) for point in points],
        "zones": [asdict(zone) for zone in zones],
        "liquidity": {
            "score": liquidity_score if liquidity.available else None,
            "average_turnover_try": liquidity.avg_turnover_20d_try if liquidity.available else None,
            "manipulation_risk": liquidity.manipulation_risk if liquidity.available else None,
            "gate": "PASS" if liquidity.available and liquidity_score >= minimum_liquidity and not liquidity.manipulation_risk else "BLOCKED",
        },
        "signal": signal,
        "invalidation_rules": [
            "30 dakika öncesindeki setup seviyesi ters yönde kapanışla kırılırsa iptal.",
            "Yüksek etkili planlı haberden 15 dakika önce yeni giriş iptal edilir.",
            "RR 1:2 altındaysa (sweep için 1:1.5) setup geçersizdir.",
            "BIST spotta SELL etiketi açığa satış emri değildir; koruma/azaltma senaryosudur.",
        ],
        "wait_reason": reason,
    }
