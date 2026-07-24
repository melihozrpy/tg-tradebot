import os

import numpy as np
import pandas as pd

from app.analysis.smart_money_engine import detect_smart_money
from app.services.alarm_sound_service import generate_alarm_wav, normalize_sound
from app.services.alert_service import create_alert, evaluate_alert
from app.services.company_analysis_service import analyze_company, format_company_analysis


def test_simple_price_alarm_touches_target_and_keeps_sound(db_session):
    from app.models.database import User
    user = User(telegram_user_id=987654, total_capital=100_000)
    db_session.add(user); db_session.commit(); db_session.refresh(user)
    alert = create_alert(db_session, user, "THYAO", "fiyat", 9.20, "radar")
    assert alert.threshold_text == "radar"
    assert evaluate_alert(db_session, alert, current_price=9.21) is not None


def test_alarm_sounds_are_valid_wav_files():
    for sound in ("zil", "radar", "acil"):
        path = generate_alarm_wav(sound)
        assert os.path.getsize(path) > 1000
    assert normalize_sound("bilinmeyen") == "zil"


def test_smart_money_detects_fvg_and_structure():
    rows = 90; base = 20 + np.sin(np.arange(rows) / 4) * 2 + np.linspace(0, 4, rows)
    df = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=rows, freq="B", tz="UTC"),
        "open": base, "close": base + .3,
        "high": base + .8, "low": base - .6, "volume": 1_000_000,
    })
    df.loc[45, "low"] = df.loc[43, "high"] + 1.2
    df.loc[45, "open"] = df.loc[45, "low"] + .1
    df.loc[45, "close"] = df.loc[45, "low"] + .4
    df.loc[45, "high"] = df.loc[45, "low"] + .8
    result = detect_smart_money(df)
    assert result.fvg
    assert all(event.kind in {"BOS", "MSS"} for event in result.structure)


def test_company_analysis_is_rule_based_and_fail_closed():
    class FakeTicker:
        info = {
            "longName": "Örnek Sanayi", "sector": "Industrials", "industry": "Machinery",
            "longBusinessSummary": "Makine üretir.", "revenueGrowth": .18,
            "profitMargins": .12, "debtToEquity": 45, "freeCashflow": 5_000_000,
            "returnOnEquity": .22,
            "trailingPE": 9.5, "priceToBook": 1.8, "enterpriseToEbitda": 6.2,
            "totalDebt": 40_000_000, "totalCash": 15_000_000,
        }
        periods = pd.to_datetime(["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"])
        quarterly_income_stmt = pd.DataFrame(
            [[90_000_000, 100_000_000, 115_000_000, 135_000_000],
             [8_000_000, 10_000_000, 13_000_000, 18_000_000],
             [14_000_000, 16_000_000, 19_000_000, 25_000_000]],
            index=["Total Revenue", "Net Income", "EBITDA"], columns=periods,
        )
        quarterly_balance_sheet = pd.DataFrame(
            [[40_000_000] * 4], index=["Total Debt"], columns=periods,
        )
        quarterly_cashflow = pd.DataFrame(
            [[7_000_000, 8_000_000, 9_000_000, 12_000_000]],
            index=["Operating Cash Flow"], columns=periods,
        )
    result = analyze_company("ORNEK", ticker_factory=lambda _symbol: FakeTicker())
    text = format_company_analysis(result)
    assert result.status == "GÜÇLÜ"
    assert "Yükselişi destekleyebilecek" in text
    assert "Resmî KAP" in text
    assert "SON ÇEYREK DEĞİŞİMLERİ" in text
    assert "Ciro:" in text and "Net kâr:" in text
    assert "F/K: 9.50x" in text and "Net borç:" in text
    assert result.financial_period == "2025-12-31"
