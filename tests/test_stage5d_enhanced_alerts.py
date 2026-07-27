from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.analysis.timeframe_levels_engine import (
    TIMEFRAME_DAILY,
    TIMEFRAME_MONTHLY,
    TIMEFRAME_WEEKLY,
    LevelDetail,
    MultiTimeframeLevelsResult,
    TimeframeLevelResult,
)
from app.data.base_provider import BaseMarketDataProvider, DataFreshness, DataUnavailableError
from app.models.database import EnhancedAlarmRule, EnhancedAlarmTriggerEvent, NewsImpactSnapshot, User
from app.services.enhanced_alert_service import (
    AlarmEvaluationContext,
    create_enhanced_alarm_rule,
    evaluate_enhanced_alarm,
    normalize_enhanced_alert_type,
    scan_enhanced_alarms,
)
from app.telegram.bot import build_telegram_application


def _df(periods=100, close=105.0, freq="1B"):
    dates = pd.date_range(end="2025-12-31", periods=periods, freq=freq, tz="UTC")
    closes = np.full(periods, close)
    return pd.DataFrame(
        {
            "timestamp": dates,
            "open": closes,
            "high": closes + 1,
            "low": closes - 1,
            "close": closes,
            "volume": np.full(periods, 1_000_000.0),
        }
    )


def _zone(mid, timeframe, support=True, confidence=80.0):
    return LevelDetail(
        low=mid - 1,
        high=mid + 1,
        mid=mid,
        confidence=confidence,
        touches=4,
        rejections=3,
        last_test_date="2025-01-01",
        sources=["swing_low" if support else "swing_high"],
        volume_confirmed=True,
        timeframe=timeframe,
        active=True,
    )


def _levels(support=100.0, resistance=110.0):
    def tf(name):
        return TimeframeLevelResult(
            timeframe=name,
            reliable=True,
            note="",
            support_1=_zone(support, name, True),
            main_support=_zone(support, name, True),
            resistance_1=_zone(resistance, name, False),
            main_resistance=_zone(resistance, name, False),
        )

    return MultiTimeframeLevelsResult(tf(TIMEFRAME_DAILY), tf(TIMEFRAME_WEEKLY), tf(TIMEFRAME_MONTHLY))


def _user(db):
    user = User(telegram_user_id=9991)
    db.add(user)
    db.commit()
    return user


def _context(df, timeframe=TIMEFRAME_DAILY, levels=None, signal=None, **kwargs):
    return AlarmEvaluationContext(
        symbol="SVGYO",
        timeframe=timeframe,
        df=df,
        levels=levels or _levels(),
        signal=signal,
        liquidity_score=80,
        provider="fake",
        **kwargs,
    )


def test_daily_support_touch_alarm(db_session):
    user = _user(db_session)
    rule = create_enhanced_alarm_rule(db_session, user, "SVGYO", "gunluk_destek")
    df = _df()
    df.loc[len(df) - 1, ["open", "high", "low", "close"]] = [102, 103, 100, 101]
    trigger = evaluate_enhanced_alarm(db_session, rule, _context(df))
    assert trigger is not None
    assert trigger.level_low == 99


def test_weekly_support_touch_alarm(db_session):
    user = _user(db_session)
    rule = create_enhanced_alarm_rule(db_session, user, "SVGYO", "haftalik_destek")
    df = _df(freq="7D")
    df.loc[len(df) - 1, ["open", "high", "low", "close"]] = [102, 103, 100, 101]
    trigger = evaluate_enhanced_alarm(db_session, rule, _context(df, TIMEFRAME_WEEKLY))
    assert trigger is not None
    assert trigger.timeframe == TIMEFRAME_WEEKLY


def test_monthly_resistance_breakout_alarm(db_session):
    user = _user(db_session)
    rule = create_enhanced_alarm_rule(db_session, user, "SVGYO", "aylik_direnc_kirilimi")
    df = _df(freq="31D", close=109)
    df.loc[len(df) - 2, "close"] = 110
    df.loc[len(df) - 1, ["open", "high", "low", "close", "volume"]] = [110, 114, 109, 112, 3_000_000]
    trigger = evaluate_enhanced_alarm(db_session, rule, _context(df, TIMEFRAME_MONTHLY))
    assert trigger is not None
    assert trigger.strong_confirmation


def test_confluence_zone_alarm(db_session):
    user = _user(db_session)
    rule = create_enhanced_alarm_rule(db_session, user, "SVGYO", "ortak_destek")
    df = _df()
    df.loc[len(df) - 1, ["high", "low", "close"]] = [102, 100, 101]
    confluence = SimpleNamespace(low=99.5, high=101.0, mid=100.25, confidence=92, sources=[], role_reversal=False)
    trigger = evaluate_enhanced_alarm(
        db_session, rule, _context(df, confluence_supports=[confluence])
    )
    assert trigger is not None
    assert trigger.level_confidence == 92


def test_volume_confirmed_breakout_is_strong(db_session):
    user = _user(db_session)
    rule = create_enhanced_alarm_rule(db_session, user, "SVGYO", "direnc_kirilimi")
    df = _df(close=109)
    df.loc[len(df) - 2, "close"] = 110
    df.loc[len(df) - 1, ["open", "high", "low", "close", "volume"]] = [110, 114, 109, 112, 3_000_000]
    trigger = evaluate_enhanced_alarm(db_session, rule, _context(df))
    assert trigger is not None and trigger.strong_confirmation


def test_low_volume_breakout_is_not_strong(db_session):
    user = _user(db_session)
    rule = create_enhanced_alarm_rule(db_session, user, "SVGYO", "direnc_kirilimi")
    df = _df(close=109)
    df.loc[len(df) - 2, "close"] = 110
    df.loc[len(df) - 1, ["open", "high", "low", "close", "volume"]] = [110, 114, 109, 112, 400_000]
    trigger = evaluate_enhanced_alarm(db_session, rule, _context(df))
    assert trigger is not None
    assert not trigger.strong_confirmation
    assert "sahte kırılım" in trigger.main_risk


def test_false_breakout_risk_alarm(db_session):
    user = _user(db_session)
    rule = create_enhanced_alarm_rule(db_session, user, "SVGYO", "sahte_kirilim_riski")
    df = _df(close=109)
    df.loc[len(df) - 2, "close"] = 110
    df.loc[len(df) - 1, ["open", "high", "low", "close", "volume"]] = [110, 114, 109, 112, 300_000]
    trigger = evaluate_enhanced_alarm(db_session, rule, _context(df))
    assert trigger is not None
    assert "yeterli hacim" in trigger.main_risk


def test_stop_alarm(db_session):
    user = _user(db_session)
    rule = create_enhanced_alarm_rule(db_session, user, "SVGYO", "stop")
    df = _df(close=105)
    df.loc[len(df) - 1, "low"] = 94
    signal = SimpleNamespace(entry_zone=(100, 102), entry_trigger=103, stop_price=95, target_1=110, target_2=115, target_3=120, risk_reward=2.5)
    trigger = evaluate_enhanced_alarm(db_session, rule, _context(df, signal=signal))
    assert trigger is not None and trigger.level_low == 95


@pytest.mark.parametrize(("target", "price"), [(1, 110), (2, 115), (3, 120)])
def test_target_alarms(db_session, target, price):
    user = _user(db_session)
    rule = create_enhanced_alarm_rule(db_session, user, "SVGYO", "hedef", [str(target)])
    df = _df(close=105)
    df.loc[len(df) - 1, "high"] = 125
    signal = SimpleNamespace(entry_zone=(100, 102), entry_trigger=103, stop_price=95, target_1=110, target_2=115, target_3=120, risk_reward=2.5)
    trigger = evaluate_enhanced_alarm(db_session, rule, _context(df, signal=signal))
    assert trigger is not None and trigger.level_low == price


def test_same_candle_alarm_deduplicated(db_session):
    user = _user(db_session)
    rule = create_enhanced_alarm_rule(db_session, user, "SVGYO", "gunluk_destek")
    df = _df()
    df.loc[len(df) - 1, ["high", "low", "close"]] = [102, 100, 101]
    context = _context(df)
    assert evaluate_enhanced_alarm(db_session, rule, context) is not None
    assert evaluate_enhanced_alarm(db_session, rule, context) is None
    assert db_session.query(EnhancedAlarmTriggerEvent).filter_by(rule_id=rule.id).count() == 1


def test_alarm_cooldown_blocks_new_candle(db_session):
    user = _user(db_session)
    rule = create_enhanced_alarm_rule(db_session, user, "SVGYO", "gunluk_destek", cooldown_minutes=120)
    df = _df()
    df.loc[len(df) - 1, ["high", "low", "close"]] = [102, 100, 101]
    now = datetime.now(timezone.utc)
    assert evaluate_enhanced_alarm(db_session, rule, _context(df), now=now) is not None
    rule.last_state_key = None
    db_session.commit()
    next_row = df.iloc[-1].copy()
    next_row["timestamp"] = df.iloc[-1]["timestamp"] + timedelta(days=1)
    df2 = pd.concat([df, pd.DataFrame([next_row])], ignore_index=True)
    assert evaluate_enhanced_alarm(db_session, rule, _context(df2), now=now + timedelta(minutes=30)) is None


def test_restart_preserves_alarm_dedup_state(db_session):
    user = _user(db_session)
    rule = create_enhanced_alarm_rule(db_session, user, "SVGYO", "gunluk_destek")
    df = _df()
    df.loc[len(df) - 1, ["high", "low", "close"]] = [102, 100, 101]
    assert evaluate_enhanced_alarm(db_session, rule, _context(df)) is not None
    db_session.expire_all()
    reloaded = db_session.query(EnhancedAlarmRule).filter_by(id=rule.id).one()
    assert evaluate_enhanced_alarm(db_session, reloaded, _context(df)) is None


@pytest.mark.parametrize(
    ("alert_type", "context_values"),
    [
        ("haber_etkisi", {"news_score": 75}),
        ("negatif_haber_etkisi", {"news_score": -75}),
        ("xu100_guc", {"xu100_strength": 82}),
        ("xu100_guc_alt", {"xu100_strength": 20}),
        ("sektor_guc", {"sector_strength": 84}),
        ("sektor_lideri", {"sector_strength": 84, "xu100_strength": 65}),
        ("xu100_ayrisma", {"xu100_strength": 80}),
    ],
)
def test_market_and_news_alarm_families(db_session, alert_type, context_values):
    user = _user(db_session)
    rule = create_enhanced_alarm_rule(db_session, user, "SVGYO", alert_type)
    trigger = evaluate_enhanced_alarm(db_session, rule, _context(_df(), **context_values))
    assert trigger is not None
    assert trigger.alert_type == alert_type


@pytest.mark.parametrize(
    ("alert_type", "scenario_attr"),
    [("ana_dip_senaryosu", "decline_main"), ("guclu_yukselis_senaryosu", "rise_breakout")],
)
def test_price_scenario_alarm_uses_real_engine_field_names(db_session, alert_type, scenario_attr):
    user = _user(db_session)
    rule = create_enhanced_alarm_rule(db_session, user, "SVGYO", alert_type)
    scenario = SimpleNamespace(**{scenario_attr: SimpleNamespace(low=104.0, high=106.0)})
    trigger = evaluate_enhanced_alarm(
        db_session,
        rule,
        _context(_df(close=105.0), price_scenario=scenario),
    )
    assert trigger is not None
    assert trigger.level_low == 104.0


def test_market_regime_first_observation_is_baseline_then_change_triggers(db_session):
    user = _user(db_session)
    rule = create_enhanced_alarm_rule(db_session, user, "SVGYO", "piyasa_rejimi")
    df = _df()
    assert evaluate_enhanced_alarm(
        db_session, rule, _context(df, market_regime="yatay", previous_market_regime=None)
    ) is None
    assert rule.last_state_key == "piyasa_rejimi:yatay"

    next_row = df.iloc[-1].copy()
    next_row["timestamp"] = df.iloc[-1]["timestamp"] + timedelta(days=1)
    changed_df = pd.concat([df, pd.DataFrame([next_row])], ignore_index=True)
    trigger = evaluate_enhanced_alarm(
        db_session,
        rule,
        _context(changed_df, market_regime="guclu_yukselis", previous_market_regime="yatay"),
    )
    assert trigger is not None
    assert trigger.state_key == "piyasa_rejimi:guclu_yukselis"


class _PartialProvider(BaseMarketDataProvider):
    name = "partial"

    def __init__(self):
        self.calls = []

    def get_ohlcv(self, symbol, timeframe, start, end):
        self.calls.append(symbol)
        if symbol == "FAIL":
            raise DataUnavailableError("test failure")
        frame = _df(periods=260, close=100)
        frame["close"] = np.linspace(80, 120, len(frame))
        frame["open"] = frame["close"] - 0.2
        frame["high"] = frame["close"] + 1
        frame["low"] = frame["close"] - 1
        return frame

    def get_quote(self, symbol):
        raise NotImplementedError

    def get_index_data(self, index_symbol, timeframe):
        return self.get_ohlcv(index_symbol, timeframe, datetime.now(timezone.utc) - timedelta(days=500), datetime.now(timezone.utc))

    def is_market_open(self):
        return False

    def get_data_freshness(self, symbol, timeframe):
        return DataFreshness(symbol, timeframe, None, False, 0, self.name)

    def health_check(self):
        return {"status": "ok", "provider": self.name}


@pytest.mark.asyncio
async def test_one_symbol_failure_does_not_stop_alarm_scan(db_session):
    user = _user(db_session)
    create_enhanced_alarm_rule(db_session, user, "FAIL", "rsi_asiri_alim")
    create_enhanced_alarm_rule(db_session, user, "OKAY", "rsi_asiri_alim")
    provider = _PartialProvider()
    settings = SimpleNamespace()
    await scan_enhanced_alarms(None, db_session, provider, settings)
    assert provider.calls == ["FAIL", "OKAY"]


@pytest.mark.asyncio
async def test_scheduled_scan_loads_persisted_news_score(db_session):
    user = _user(db_session)
    rule = create_enhanced_alarm_rule(db_session, user, "OKAY", "haber_etkisi", ["60"])
    db_session.add(
        NewsImpactSnapshot(
            symbol="OKAY",
            window_label="24h",
            article_count=2,
            impact_score=72,
            confidence_score=80,
        )
    )
    db_session.commit()
    provider = _PartialProvider()
    settings = SimpleNamespace(xu100_symbol="XU100.IS")
    await scan_enhanced_alarms(None, db_session, provider, settings)
    assert db_session.query(EnhancedAlarmTriggerEvent).filter_by(rule_id=rule.id).count() == 1


def test_alarm_message_contains_required_provider_and_quality_fields(db_session):
    user = _user(db_session)
    rule = create_enhanced_alarm_rule(db_session, user, "SVGYO", "gunluk_destek")
    df = _df()
    df.loc[len(df) - 1, ["high", "low", "close"]] = [102, 100, 101]
    trigger = evaluate_enhanced_alarm(db_session, rule, _context(df, xu100_strength=78, sector_strength=81, market_regime="pozitif"))
    assert "MONTANA MELİH HİSSE BOT — ALARM" in trigger.message
    assert "Veri sağlayıcısı: fake" in trigger.message
    assert "XU100 göreceli gücü: 78" in trigger.message


@pytest.mark.parametrize(
    ("raw", "canonical", "timeframe"),
    [
        ("haftalik_direnc_kirilimi", "direnc_kirilimi", TIMEFRAME_WEEKLY),
        ("ortak_destek", "ortak_destek", None),
        ("hacim_patlamasi", "hacim_patlamasi", None),
        ("rsi_asiri_satim", "rsi_asiri_satim", None),
    ],
)
def test_natural_alarm_aliases(raw, canonical, timeframe):
    result = normalize_enhanced_alert_type(raw)
    assert result[:2] == (canonical, timeframe)


def test_new_alarm_commands_registered(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    from app.config.settings import get_settings

    get_settings.cache_clear()
    app = build_telegram_application()
    commands = {
        command
        for group in app.handlers.values()
        for handler in group
        for command in getattr(handler, "commands", set())
    }
    assert {"alarm_kur", "alarmlar", "alarm_sil", "alarm_durdur", "alarm_ac", "alarm_detay", "veri_durumu"}.issubset(commands)
    get_settings.cache_clear()
