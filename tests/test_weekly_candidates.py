from __future__ import annotations

from datetime import datetime, timezone

import matplotlib.pyplot as plt
import pandas as pd

from app.analysis.pattern_engine import ChartPattern
from app.analysis.screener_engine import DailyTopPick, DailyTopPicksRunResult
from app.services.vivid_chart_style import save_terminal_png
from app.services import weekly_candidates_service
from app.services.weekly_candidates_service import format_weekly_candidates_report


def _report(*, picks=()):
    return DailyTopPicksRunResult(
        scanned=100,
        failed=0,
        picks=tuple(picks),
        fundamental_checked=1,
        fundamental_verified=1,
        created_at=datetime(2026, 10, 26, 6, 15, tzinfo=timezone.utc),
    )


def _pick(score: int = 82) -> DailyTopPick:
    return DailyTopPick(
        symbol="THYAO",
        score=score,
        technical_confirmations=7,
        price=310.0,
        entry_low=300.0,
        entry_high=302.0,
        stop=296.0,
        tp1=312.0,
        tp2=320.0,
        rr=2.2,
        target_potential_percent=5.0,
        pattern=ChartPattern("Yükselen Üçgen", "continuation", "bullish", True, 90, 302.0, 320.0, "Teyitli."),
        reasons=("EMA20/50 üstü", "RVOL teyidi", "İlk retest"),
        confirmation_instruction="Teyit beklenir.",
        fundamental_score=80,
        fundamental_status="GÜÇLÜ",
        fundamental_source="test",
    )


def test_weekly_candidate_report_is_capped_and_retest_first() -> None:
    text = format_weekly_candidates_report(_report(picks=(_pick(), _pick(), _pick())))

    assert text.count("<b>") == 3  # heading + at most two candidates
    assert "Alım bölgesi" in text
    assert "H1" in text and "H2" in text
    assert "Eğitim amaçlı genel analizdir" in text


def test_weekly_candidate_report_allows_no_trade() -> None:
    text = format_weekly_candidates_report(_report())

    assert "Bu hafta eşiği geçen aday yok" in text
    assert "Eğitim amaçlı genel analizdir" in text


def test_terminal_png_is_exact_telegram_frame(tmp_path) -> None:
    from PIL import Image

    figure = plt.figure(figsize=(16, 9))
    target = tmp_path / "terminal.png"
    try:
        save_terminal_png(figure, target)
        with Image.open(target) as image:
            assert image.size == (1600, 900)
    finally:
        plt.close(figure)


def test_weekly_candidate_card_uses_terminal_png(monkeypatch, tmp_path) -> None:
    index = pd.date_range("2026-08-01", periods=45, tz="UTC", freq="D")
    frame = pd.DataFrame(
        {
            "open": [100 + index_ * 0.1 for index_ in range(45)],
            "high": [101 + index_ * 0.1 for index_ in range(45)],
            "low": [99 + index_ * 0.1 for index_ in range(45)],
            "close": [100.4 + index_ * 0.1 for index_ in range(45)],
            "volume": [1_000_000] * 45,
        },
        index=index,
    )

    class Provider:
        def get_ohlcv(self, *_args):
            return frame

    class Settings:
        timezone_name = "Europe/Istanbul"
        report_chart_output_dir = str(tmp_path)

    monkeypatch.setattr(weekly_candidates_service, "build_market_data_provider", lambda _settings: Provider())
    image_path = weekly_candidates_service.render_weekly_candidates_card(_report(picks=(_pick(),)), Settings())

    assert image_path is not None
    from PIL import Image

    with Image.open(image_path) as image:
        assert image.size == (1600, 900)
