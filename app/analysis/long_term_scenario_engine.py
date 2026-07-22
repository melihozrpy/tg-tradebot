from __future__ import annotations

"""Kanıta dayalı uzun vadeli boğa/ayı senaryoları.

Uzun vadeli ``kanıt gücü`` bir gerçekleşme olasılığı değildir. Puan; bağımsız
teknik kaynakların, trendin, likiditenin, göreceli gücün, değerlemenin ve veri
kalitesinin senaryoyu ne ölçüde desteklediğini açıklayan sınırlı bir bileşik
göstergedir. Kısa vadeli sinyal güveninden ayrı bir model ve alanda tutulur.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd

from app.utils.financial_formatter import percent_change, price_multiple, round_money


@dataclass
class EvidenceScoreBreakdown:
    technical_structure: float = 0.0
    trend: float = 0.0
    volume_liquidity: float = 0.0
    relative_strength: float = 0.0
    fundamental_valuation: float = 0.0
    data_quality_penalty: float = 0.0
    speculation_risk_penalty: float = 0.0
    total: float = 0.0
    explanations: list[str] = field(default_factory=list)


@dataclass
class LongTermScenarioZone:
    direction: str
    scenario_class: str
    technical_role: str
    low: float
    high: float
    mid: float
    required_change_percent: float
    required_price_multiple: float
    evidence_strength: float
    time_horizon: str
    activation_conditions: list[str] = field(default_factory=list)
    invalidation_conditions: list[str] = field(default_factory=list)
    intermediate_resistances: list[float] = field(default_factory=list)
    supports_to_hold: list[float] = field(default_factory=list)
    volume_condition: str = ""
    market_condition: str = ""
    fundamental_support: str = "Veri yetersiz"
    speculation_risk: str = "Veri yetersiz"
    evidence: list[str] = field(default_factory=list)
    evidence_categories: list[str] = field(default_factory=list)
    score_breakdown: EvidenceScoreBreakdown = field(default_factory=EvidenceScoreBreakdown)
    target_market_cap: Optional[float] = None

    @property
    def confidence(self) -> float:
        """Eski DB/çağıran kod için geriye uyumlu salt-okunur takma ad."""

        return self.evidence_strength


@dataclass
class LongTermScenarioResult:
    current_price: float
    reliable: bool
    note: str = ""
    short_term_target: Optional[LongTermScenarioZone] = None
    medium_term_target: Optional[LongTermScenarioZone] = None
    long_term_main_target: Optional[LongTermScenarioZone] = None
    strong_bull: Optional[LongTermScenarioZone] = None
    extreme_bull: Optional[LongTermScenarioZone] = None
    near_pullback: Optional[LongTermScenarioZone] = None
    medium_term_support: Optional[LongTermScenarioZone] = None
    long_term_bottom: Optional[LongTermScenarioZone] = None
    extreme_negative: Optional[LongTermScenarioZone] = None
    generated_at: Optional[pd.Timestamp] = None
    long_term_trend: str = "Veri yetersiz"
    evidence_strength: Optional[float] = None
    data_quality_score: Optional[float] = None
    missing_data: list[str] = field(default_factory=list)
    extreme_bull_note: str = "Aşırı boğa senaryosu için yeterli kanıt yok."

    def all_scenarios(self) -> list[LongTermScenarioZone]:
        return [
            item
            for item in (
                self.short_term_target,
                self.medium_term_target,
                self.long_term_main_target,
                self.strong_bull,
                self.extreme_bull,
                self.near_pullback,
                self.medium_term_support,
                self.long_term_bottom,
                self.extreme_negative,
            )
            if item is not None
        ]


@dataclass
class _Candidate:
    low: float
    high: float
    mid: float
    sources: list[str]
    categories: set[str]
    base_strength: float
    role_hint: str

    @property
    def independent_source_count(self) -> int:
        return len(self.categories)


def _valid_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if not required.issubset(df.columns):
        return pd.DataFrame()
    out = df.copy().sort_values("timestamp").reset_index(drop=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["timestamp", "open", "high", "low", "close"])
    return out.loc[(out[["open", "high", "low", "close"]] > 0).all(axis=1)].reset_index(drop=True)


def _atr_value(df: pd.DataFrame) -> float:
    previous = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous).abs(),
            (df["low"] - previous).abs(),
        ],
        axis=1,
    ).max(axis=1)
    value = float(true_range.tail(20).median()) if len(true_range.dropna()) else 0.0
    return max(value, float(df["close"].iloc[-1]) * 0.0025)


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    work = df.set_index("timestamp").sort_index()
    result = work.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open", "high", "low", "close"])
    return result.reset_index()


def _ema_value(series: pd.Series, period: int) -> Optional[float]:
    if len(series) < max(4, period // 2):
        return None
    value = float(series.ewm(span=period, adjust=False).mean().iloc[-1])
    return value if np.isfinite(value) and value > 0 else None


def _trend_profile(df: pd.DataFrame) -> tuple[str, float, list[str], list[_Candidate]]:
    weekly = _resample(df, "W-FRI")
    monthly = _resample(df, "ME")
    score = 0.0
    evidence: list[str] = []
    candidates: list[_Candidate] = []
    daily_atr = _atr_value(df)
    for frame, label, weight in ((weekly, "haftalık", 1.0), (monthly, "aylık", 1.35)):
        if len(frame) < 8:
            continue
        close = frame["close"].astype(float)
        fast = _ema_value(close, 20)
        slow = _ema_value(close, 50)
        long_100 = _ema_value(close, 100)
        long_200 = _ema_value(close, 200)
        latest = float(close.iloc[-1])
        if fast is not None:
            score += (3.0 if latest >= fast else -3.0) * weight
            evidence.append(f"{label} EMA20")
        if fast is not None and slow is not None:
            score += (3.5 if fast >= slow else -3.5) * weight
            evidence.append(f"{label} EMA20/50 yapısı")
        if len(close) >= 6:
            slope = float(close.tail(6).pct_change().mean())
            score += float(np.clip(slope * 220, -3.5, 3.5)) * weight
        for period, value in ((20, fast), (50, slow), (100, long_100), (200, long_200)):
            if value is None:
                continue
            width = max(daily_atr * (0.35 if label == "haftalık" else 0.55), value * 0.001)
            candidates.append(
                _Candidate(
                    value - width,
                    value + width,
                    value,
                    [f"{label} EMA{period}"],
                    {f"{label}_ema"},
                    58.0 + (5.0 if label == "aylık" else 0.0) + (3.0 if period >= 100 else 0.0),
                    "dinamik destek/direnç",
                )
            )
    score = float(np.clip(score, -14.0, 14.0))
    if score >= 8:
        label = "Güçlü yükseliş"
    elif score >= 3:
        label = "Yükseliş"
    elif score <= -8:
        label = "Güçlü düşüş"
    elif score <= -3:
        label = "Düşüş"
    else:
        label = "Yatay / kararsız"
    return label, score, evidence, candidates


def _level_candidates(levels_result: Any) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    if levels_result is None:
        return candidates
    try:
        for level in levels_result.all_zones():
            mid = float(level.mid)
            low = float(getattr(level, "low", mid))
            high = float(getattr(level, "high", mid))
            if not np.isfinite(mid) or mid <= 0:
                continue
            timeframe = str(getattr(level, "timeframe", "teknik"))
            sources = [f"{timeframe} teknik bölge"] + [str(x) for x in getattr(level, "sources", []) or []]
            categories = {f"{timeframe}_seviye"}
            for source in getattr(level, "sources", []) or []:
                source_text = str(source).casefold()
                if "fibonacci" in source_text:
                    categories.add("fibonacci")
                if "volume" in source_text or "hacim" in source_text or "vwap" in source_text:
                    categories.add("volume_profile")
                if "ema" in source_text:
                    categories.add("ema")
                if "tepe" in source_text or "dip" in source_text or "swing" in source_text:
                    categories.add("historical")
            candidates.append(
                _Candidate(
                    min(low, high),
                    max(low, high),
                    mid,
                    list(dict.fromkeys(sources)),
                    categories,
                    float(getattr(level, "confidence", 50.0) or 50.0),
                    str(getattr(level, "strength_class", "teknik bölge")),
                )
            )
    except Exception:  # noqa: BLE001 - opsiyonel seviye bağlamı
        return candidates
    return candidates


def _derived_candidates(df: pd.DataFrame, current: float, atr: float) -> tuple[list[_Candidate], bool]:
    recent = df.tail(min(len(df), 2000))
    historical_high = float(recent["high"].max())
    historical_low = float(recent["low"].min())
    span = historical_high - historical_low
    candidates: list[_Candidate] = []

    def add(price: float, source: str, category: str, strength: float, role: str, width_factor: float = 0.45) -> None:
        if not np.isfinite(price) or price <= 0:
            return
        width = max(atr * width_factor, price * 0.0005)
        candidates.append(_Candidate(price - width, price + width, price, [source], {category}, strength, role))

    add(historical_high, "tarihî ana tepe", "historical", 68.0, "tarihî direnç")
    add(historical_low, "tarihî ana dip", "historical", 68.0, "tarihî destek")
    if span > atr:
        for ratio, strength in ((0.382, 60.0), (0.5, 61.0), (0.618, 64.0), (0.786, 58.0)):
            add(historical_low + span * ratio, f"Fibonacci %{ratio * 100:.1f}", "fibonacci", strength, "Fibonacci bölgesi")
        for ratio, strength in ((1.272, 58.0), (1.618, 55.0)):
            add(historical_low + span * ratio, f"Fibonacci extension {ratio:.3f}", "fibonacci_extension", strength, "Fibonacci uzatma bölgesi")

    # Hacim profilinde yalnızca gerçekten hacim biriken düğümler kullanılır.
    if len(recent) >= 60 and float(recent["volume"].clip(lower=0).sum()) > 0:
        low, high = float(recent["low"].min()), float(recent["high"].max())
        if high > low:
            edges = np.linspace(low, high, 37)
            centers = (edges[:-1] + edges[1:]) / 2
            totals = np.zeros(len(centers))
            typical = ((recent["high"] + recent["low"] + recent["close"]) / 3).to_numpy(float)
            for price, volume in zip(typical, recent["volume"].fillna(0).to_numpy(float)):
                index = int(np.clip(np.searchsorted(edges, price, side="right") - 1, 0, len(centers) - 1))
                totals[index] += max(0.0, volume)
            for index in np.argsort(totals)[::-1][:3]:
                if totals[index] > 0:
                    add(float(centers[index]), "hacimli fiyat bölgesi", "volume_profile", 62.0, "hacim profili düğümü", 0.55)

    log_valid = False
    weekly = _resample(recent, "W-FRI")
    if len(weekly) >= 104 and (weekly["close"] > 0).all():
        x = np.arange(len(weekly), dtype=float)
        logs = np.log(weekly["close"].astype(float).to_numpy())
        slope, intercept = np.polyfit(x, logs, 1)
        residual_std = float(np.std(logs - (slope * x + intercept)))
        r_squared_den = float(np.sum((logs - logs.mean()) ** 2))
        r_squared = 1.0 - float(np.sum((logs - (slope * x + intercept)) ** 2)) / r_squared_den if r_squared_den > 0 else 0.0
        log_valid = bool(np.isfinite(residual_std) and residual_std < 0.45 and r_squared >= 0.20)
        if log_valid:
            for future, label in ((52, "logaritmik kanal 1 yıl"), (104, "logaritmik kanal 2 yıl")):
                center = float(np.exp(intercept + slope * (len(weekly) - 1 + future)))
                upper = float(np.exp(np.log(center) + residual_std))
                strength = 62.0 if slope > 0 else 48.0
                candidates.append(
                    _Candidate(
                        min(center, upper),
                        max(center, upper),
                        (center + upper) / 2,
                        [label],
                        {"log_channel"},
                        strength,
                        "logaritmik trend kanalı",
                    )
                )
    return candidates, log_valid


def _merge_candidates(candidates: list[_Candidate], atr: float) -> list[_Candidate]:
    valid = [item for item in candidates if np.isfinite(item.mid) and item.mid > 0]
    valid.sort(key=lambda item: item.mid)
    tolerance = max(atr * 0.85, 0.01)
    merged: list[_Candidate] = []
    for item in valid:
        if merged and max(item.low, merged[-1].low) <= min(item.high, merged[-1].high) + tolerance:
            previous = merged[-1]
            weight_a = max(previous.base_strength, 1.0)
            weight_b = max(item.base_strength, 1.0)
            previous.mid = (previous.mid * weight_a + item.mid * weight_b) / (weight_a + weight_b)
            previous.low = min(previous.low, item.low)
            previous.high = max(previous.high, item.high)
            previous.sources = list(dict.fromkeys(previous.sources + item.sources))
            previous.categories.update(item.categories)
            previous.base_strength = min(96.0, max(previous.base_strength, item.base_strength) + min(12.0, len(previous.categories) * 2.0))
            if len(previous.categories) > 1:
                previous.role_hint = "çok kaynaklı teknik bölge"
        else:
            merged.append(item)
    return merged


def _relative_score(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if np.isfinite(number) else None
    if not getattr(value, "available", False):
        return None
    number = getattr(value, "relative_score", None)
    return float(number) if number is not None and np.isfinite(number) else None


def _valuation_contribution(valuation_result: Any) -> tuple[float, str, bool]:
    if valuation_result is None or not getattr(valuation_result, "applicable", False):
        return 0.0, "Veri yetersiz", False
    classification = str(getattr(valuation_result, "classification", "Veri yetersiz"))
    mapping = {
        "Çok iskontolu": 8.0,
        "İskontolu": 5.0,
        "Makul": 1.0,
        "Primli": -5.0,
        "Aşırı primli": -8.0,
    }
    contribution = mapping.get(classification, 0.0)
    stale = bool(getattr(valuation_result, "data_is_stale", False))
    if stale:
        contribution *= 0.5
    available = classification not in {"Veri yetersiz", "Uygulanamaz"}
    return contribution, classification if available else "Veri yetersiz", available


def _regime_contribution(market_regime: Any, direction: str) -> tuple[float, str]:
    regime = str(getattr(market_regime, "regime", market_regime or ""))
    positive = {"guclu_yukselis": 5.0, "zayif_yukselis": 2.5}
    negative = {"guclu_dusus": -5.0, "zayif_dusus": -2.5, "dagitim": -3.0, "asiri_volatil": -2.0}
    raw = positive.get(regime, negative.get(regime, 0.0))
    contribution = raw if direction == "yükseliş" else -raw
    return contribution, regime


def _score_candidate(
    candidate: _Candidate,
    *,
    current: float,
    direction: str,
    trend_score: float,
    liquidity_score: Optional[float],
    relative_strength: Any,
    sector_relative_strength: Any,
    valuation_result: Any,
    market_regime: Any,
    data_quality_score: Optional[float],
    manipulation_risk: bool,
) -> EvidenceScoreBreakdown:
    explanations: list[str] = []
    source_bonus = min(18.0, candidate.independent_source_count * 4.5 + max(0, len(candidate.sources) - 1) * 1.5)
    technical = min(44.0, 15.0 + candidate.base_strength * 0.16 + source_bonus)
    trend = trend_score if direction == "yükseliş" else -trend_score
    trend = float(np.clip(trend, -14.0, 14.0))

    multiple = price_multiple(candidate.mid, current) or 1.0
    far = multiple >= 1.8 if direction == "yükseliş" else multiple <= 0.55
    volume = 0.0
    if liquidity_score is not None:
        if liquidity_score >= 70:
            volume = 6.0
        elif liquidity_score >= 50:
            volume = 3.0
        elif liquidity_score < 20:
            volume = -24.0 if far else -16.0
        elif liquidity_score < 40:
            volume = -14.0 if far else -8.0
        explanations.append(f"Likidite puanı {liquidity_score:.0f}/100")

    rs_values = [value for value in (_relative_score(relative_strength), _relative_score(sector_relative_strength)) if value is not None]
    rs_contribution = 0.0
    if rs_values:
        average = sum(rs_values) / len(rs_values)
        rs_contribution = float(np.clip((average - 50.0) / 5.0, -8.0, 8.0))
        if direction == "düşüş":
            rs_contribution *= -1
        explanations.append(f"Göreceli güç ortalaması {average:.0f}/100")

    valuation, valuation_label, valuation_available = _valuation_contribution(valuation_result)
    if direction == "düşüş":
        valuation *= -1
    if valuation_available:
        explanations.append(f"Temel değerleme: {valuation_label}")

    regime, regime_label = _regime_contribution(market_regime, direction)
    if regime_label:
        explanations.append(f"Piyasa rejimi: {regime_label}")
    trend = float(np.clip(trend + regime, -16.0, 16.0))

    quality_penalty = 0.0
    if data_quality_score is not None:
        quality_penalty = -float(np.clip((80.0 - data_quality_score) * 0.45, 0.0, 27.0))
        if quality_penalty:
            explanations.append(f"Veri kalitesi {data_quality_score:.0f}/100")

    speculation_penalty = 0.0
    if direction == "yükseliş":
        if multiple >= 3.0:
            speculation_penalty -= 16.0
        elif multiple >= 2.0:
            speculation_penalty -= 9.0
        elif multiple >= 1.5:
            speculation_penalty -= 4.0
    if manipulation_risk:
        speculation_penalty -= 12.0
        explanations.append("Anormal fiyat/hacim riski")

    total = technical + trend + volume + rs_contribution + valuation + quality_penalty + speculation_penalty
    total = float(np.clip(total, 0.0, 100.0))
    if data_quality_score is not None:
        if data_quality_score < 50:
            total = min(total, 49.0)
        elif data_quality_score < 70:
            total = min(total, 64.0)
    return EvidenceScoreBreakdown(
        technical_structure=round(technical, 1),
        trend=round(trend, 1),
        volume_liquidity=round(volume, 1),
        relative_strength=round(rs_contribution, 1),
        fundamental_valuation=round(valuation, 1),
        data_quality_penalty=round(quality_penalty, 1),
        speculation_risk_penalty=round(speculation_penalty, 1),
        total=round(total, 1),
        explanations=explanations,
    )


def _market_note(market_context: Optional[dict], market_regime: Any) -> str:
    parts: list[str] = []
    if market_context:
        if market_context.get("xu100_trend"):
            parts.append(f"XU100 {market_context['xu100_trend']}")
        if market_context.get("sector_trend"):
            parts.append(f"sektör {market_context['sector_trend']}")
    regime = getattr(market_regime, "regime", None)
    if regime:
        parts.append(f"rejim {regime}")
    return "; ".join(parts)


def _make_zone(
    candidate: _Candidate,
    *,
    direction: str,
    scenario_class: str,
    technical_role: str,
    current: float,
    horizon: str,
    score: EvidenceScoreBreakdown,
    resistances: list[float],
    supports: list[float],
    liquidity_score: Optional[float],
    fundamental_support: str,
    market_context: Optional[dict],
    market_regime: Any,
    target_market_cap: Optional[float],
) -> LongTermScenarioZone:
    if direction == "yükseliş":
        activation = [f"{candidate.high:.2f} TL üzerinde kapanış ve kalıcılık"]
        invalidation = [
            f"{supports[-1]:.2f} TL ana desteği altında kapanış" if supports else "Ana teknik desteğin kaybı"
        ]
    else:
        activation = [f"{candidate.low:.2f} TL altında kapanış"]
        invalidation = [
            f"{resistances[0]:.2f} TL üzerine dönüş" if resistances else "Kırılan bölgenin geri alınması"
        ]
    multiple = price_multiple(candidate.mid, current)
    speculation = "Yüksek" if direction == "yükseliş" and multiple is not None and multiple >= 3 else (
        "Yüksek" if score.speculation_risk_penalty <= -12 else "Orta" if score.speculation_risk_penalty < 0 else "Düşük-Orta"
    )
    volume_condition = "Kapanışın olağan hacmin üzerinde teyidi" if liquidity_score is not None else ""
    return LongTermScenarioZone(
        direction=direction,
        scenario_class=scenario_class,
        technical_role=technical_role,
        low=round(candidate.low, 2),
        high=round(candidate.high, 2),
        mid=round(candidate.mid, 2),
        required_change_percent=percent_change(candidate.mid, current) or 0.0,
        required_price_multiple=multiple or 0.0,
        evidence_strength=score.total,
        time_horizon=horizon,
        activation_conditions=activation,
        invalidation_conditions=invalidation,
        intermediate_resistances=[round(x, 2) for x in resistances if current < x < candidate.mid],
        supports_to_hold=[round(x, 2) for x in supports],
        volume_condition=volume_condition,
        market_condition=_market_note(market_context, market_regime),
        fundamental_support=fundamental_support,
        speculation_risk=speculation,
        evidence=list(dict.fromkeys(candidate.sources)),
        evidence_categories=sorted(candidate.categories),
        score_breakdown=score,
        target_market_cap=round_money(target_market_cap),
    )


def _first_distinct(items: list[_Candidate], start_index: int, minimum_gap: float) -> tuple[Optional[_Candidate], int]:
    if start_index >= len(items):
        return None, start_index
    base = items[start_index - 1].mid if start_index > 0 else None
    for index in range(start_index, len(items)):
        if base is None or abs(items[index].mid - base) >= minimum_gap:
            return items[index], index
    return None, len(items)


def compute_long_term_scenarios(
    df: pd.DataFrame,
    current_price: float,
    *,
    levels_result=None,
    liquidity_score: Optional[float] = None,
    market_context: Optional[dict] = None,
    fundamental_support: Optional[str] = None,
    valuation_result: Any = None,
    relative_strength: Any = None,
    sector_relative_strength: Any = None,
    market_regime: Any = None,
    data_quality_score: Optional[float] = None,
    shares_outstanding: Optional[float] = None,
    current_market_cap: Optional[float] = None,
    manipulation_risk: bool = False,
) -> LongTermScenarioResult:
    data = _valid_df(df)
    current = round_money(current_price)
    if current is None or current <= 0 or len(data) < 60:
        return LongTermScenarioResult(
            current_price=current or 0.0,
            reliable=False,
            note="Uzun vadeli senaryo için veri yetersiz.",
            data_quality_score=data_quality_score,
        )

    atr = _atr_value(data)
    trend_label, trend_score, _trend_evidence, ema_candidates = _trend_profile(data)
    derived, log_valid = _derived_candidates(data, current, atr)
    merged = _merge_candidates(_level_candidates(levels_result) + ema_candidates + derived, atr)
    supports = [item for item in merged if item.mid < current - atr * 0.15]
    resistances = [item for item in merged if item.mid > current + atr * 0.15]
    support_prices = [item.mid for item in supports]
    resistance_prices = [item.mid for item in resistances]

    _, valuation_label, valuation_available = _valuation_contribution(valuation_result)
    fundamental_label = fundamental_support or (valuation_label if valuation_available else "Veri yetersiz")
    missing: list[str] = []
    if liquidity_score is None:
        missing.append("likidite")
    if _relative_score(relative_strength) is None:
        missing.append("XU100 göreceli güç")
    if _relative_score(sector_relative_strength) is None:
        missing.append("sektör göreceli güç")
    if not valuation_available:
        missing.append("temel değerleme")
    if data_quality_score is None:
        missing.append("veri kalite puanı")
    if not log_valid:
        missing.append("doğrulanmış logaritmik kanal")

    result = LongTermScenarioResult(
        current_price=current,
        reliable=bool(supports or resistances),
        note="" if supports or resistances else "Teknik olarak ayrıştırılabilir bölge bulunamadı.",
        generated_at=pd.Timestamp.now(tz="UTC"),
        long_term_trend=trend_label,
        data_quality_score=data_quality_score,
        missing_data=missing,
    )

    def zone_for(candidate: _Candidate, direction: str, label: str, role: str, horizon: str) -> LongTermScenarioZone:
        score = _score_candidate(
            candidate,
            current=current,
            direction=direction,
            trend_score=trend_score,
            liquidity_score=liquidity_score,
            relative_strength=relative_strength,
            sector_relative_strength=sector_relative_strength,
            valuation_result=valuation_result,
            market_regime=market_regime,
            data_quality_score=data_quality_score,
            manipulation_risk=manipulation_risk,
        )
        target_cap = None
        if shares_outstanding and shares_outstanding > 0:
            target_cap = candidate.mid * shares_outstanding
        elif current_market_cap and current_market_cap > 0:
            target_cap = current_market_cap * (candidate.mid / current)
        return _make_zone(
            candidate,
            direction=direction,
            scenario_class=label,
            technical_role=role,
            current=current,
            horizon=horizon,
            score=score,
            resistances=resistance_prices,
            supports=support_prices[-3:],
            liquidity_score=liquidity_score,
            fundamental_support=fundamental_label,
            market_context=market_context,
            market_regime=market_regime,
            target_market_cap=target_cap,
        )

    # Roller yalnızca fiyat sırasıyla değil; mesafe, bağımsız kaynak ve puan eşiğiyle atanır.
    if resistances:
        first = resistances[0]
        result.short_term_target = zone_for(first, "yükseliş", "İlk önemli direnç", first.role_hint, "Kısa vade")
        medium, medium_index = _first_distinct(resistances, 1, max(atr * 1.5, current * 0.04))
        if medium is not None:
            result.medium_term_target = zone_for(medium, "yükseliş", "Ana hedef adayı", medium.role_hint, "Orta vade")
        main_start = medium_index + 1 if medium is not None else 1
        for candidate in resistances[main_start:]:
            candidate_zone = zone_for(candidate, "yükseliş", "Uzun vadeli ana hedef", candidate.role_hint, "Uzun vade")
            if candidate.independent_source_count >= 2 and candidate_zone.evidence_strength >= 45:
                result.long_term_main_target = candidate_zone
                main_start = resistances.index(candidate) + 1
                break
        for candidate in resistances[main_start:]:
            candidate_zone = zone_for(candidate, "yükseliş", "Güçlü boğa senaryosu", candidate.role_hint, "Uzun vade")
            if candidate.independent_source_count >= 2 and candidate_zone.evidence_strength >= 45:
                result.strong_bull = candidate_zone
                break

    # Aşırı boğa: tarih, doğrulanmış kanal/extension, piyasa değeri, likidite ve
    # en az üç bağımsız kaynak birlikte yoksa kesinlikle üretilmez.
    far_candidates = [item for item in resistances if item.mid >= current * 1.8]
    for candidate in reversed(far_candidates):
        categories = candidate.categories
        has_projection = bool({"log_channel", "fibonacci_extension"} & categories)
        target_cap_known = bool((shares_outstanding and shares_outstanding > 0) or (current_market_cap and current_market_cap > 0))
        target_multiple = candidate.mid / current
        market_cap_acceptable = target_cap_known and target_multiple <= 4.0
        zone = zone_for(candidate, "yükseliş", "Aşırı boğa senaryosu", candidate.role_hint, "Uzun vade")
        if (
            len(data) >= 756
            and log_valid
            and has_projection
            and candidate.independent_source_count >= 3
            and liquidity_score is not None
            and liquidity_score >= 45
            and market_cap_acceptable
            and zone.evidence_strength >= 45
        ):
            zone.speculation_risk = "Yüksek; henüz doğrulanmış değildir"
            result.extreme_bull = zone
            result.extreme_bull_note = ""
            break

    descending = list(reversed(supports))
    if descending:
        first = descending[0]
        result.near_pullback = zone_for(first, "düşüş", "İlk önemli destek", first.role_hint, "Kısa vade")
        main, main_index = _first_distinct(descending, 1, max(atr * 1.5, current * 0.04))
        if main is not None:
            main_zone = zone_for(main, "düşüş", "Ana dip bölgesi", main.role_hint, "Orta vade")
            if main.independent_source_count >= 2 or main_zone.evidence_strength >= 42:
                result.medium_term_support = main_zone
        bottom_start = main_index + 1 if main is not None else 1
        for candidate in descending[bottom_start:]:
            zone = zone_for(candidate, "düşüş", "Uzun vadeli ana dip", candidate.role_hint, "Uzun vade")
            if candidate.independent_source_count >= 2 and zone.evidence_strength >= 40:
                result.long_term_bottom = zone
                break
        if len(data) >= 756:
            for candidate in reversed(descending):
                zone = zone_for(candidate, "düşüş", "Aşırı negatif senaryo", candidate.role_hint, "Uzun vade")
                if candidate.mid >= current * 0.30 and candidate.independent_source_count >= 3 and zone.evidence_strength >= 42:
                    zone.speculation_risk = "Yüksek risk senaryosu; kesin dip tahmini değildir"
                    result.extreme_negative = zone
                    break

    strengths = [zone.evidence_strength for zone in result.all_scenarios()]
    result.evidence_strength = round(sum(strengths) / len(strengths), 1) if strengths else None
    return result
