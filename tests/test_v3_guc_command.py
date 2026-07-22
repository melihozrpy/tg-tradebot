from __future__ import annotations

from app.config.settings import get_settings
from app.models.database import RelativeStrengthPeriod
from app.services.relative_strength_service import (
    compute_symbol_relative_strength,
    persist_relative_strength,
)
from app.services.sector_service import set_sector_mapping
from app.telegram.message_templates_v3 import format_guc


def test_compute_symbol_relative_strength_xu100_only(mock_provider):
    settings = get_settings()
    result = compute_symbol_relative_strength(mock_provider, "THYAO", settings)
    assert result.symbol == "THYAO"
    assert result.xu100 is not None
    assert result.xu100.benchmark_symbol == settings.xu100_symbol
    # Sektor eslestirmesi yoksa sector sonucu None olmalidir (uydurulmaz).
    assert result.sector is None or result.sector_name is not None


def test_compute_symbol_relative_strength_with_sector(mock_provider):
    settings = get_settings()
    set_sector_mapping("TESTSYM", "TESTSEC.IS", "Test Sektörü")
    result = compute_symbol_relative_strength(mock_provider, "TESTSYM", settings)
    assert result.sector is not None
    assert result.sector_name == "Test Sektörü"
    assert result.sector.benchmark_symbol == "TESTSEC.IS"


def test_persist_relative_strength_writes_rows(mock_provider, db_session):
    settings = get_settings()
    result = compute_symbol_relative_strength(mock_provider, "ASELS", settings)
    added = persist_relative_strength(db_session, result)
    assert added > 0
    rows = db_session.query(RelativeStrengthPeriod).filter(RelativeStrengthPeriod.symbol == "ASELS").all()
    assert len(rows) == added
    assert all(r.benchmark in ("xu100", "sektor") for r in rows)


def test_format_guc_renders_periods_and_no_fake_data_note(mock_provider):
    settings = get_settings()
    result = compute_symbol_relative_strength(mock_provider, "THYAO", settings)
    text = format_guc("THYAO", result)
    assert "THYAO" in text
    assert "GÖRECELİ GÜÇ" in text
    assert "XU100'e göre" in text
    assert "yatırım tavsiyesi değildir" in text
