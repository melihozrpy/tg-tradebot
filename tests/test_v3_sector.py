from __future__ import annotations

import tempfile
from pathlib import Path

from app.services.sector_service import get_sector_info, list_sector_mappings, set_sector_mapping


def test_unmapped_symbol_returns_none_never_fabricated(monkeypatch):
    from app.config import settings as settings_module

    tmp_path = Path(tempfile.mkdtemp()) / "sector_map_test.yaml"
    monkeypatch.setattr(settings_module.get_settings(), "sector_map_path", str(tmp_path))

    info = get_sector_info("ZZZZNOTMAPPED")
    assert info is None


def test_user_added_mapping_is_retrievable(monkeypatch):
    from app.config import settings as settings_module

    tmp_path = Path(tempfile.mkdtemp()) / "sector_map_test2.yaml"
    monkeypatch.setattr(settings_module.get_settings(), "sector_map_path", str(tmp_path))

    set_sector_mapping("TESTSYM", "XTEST.IS", "Test Sektoru")
    info = get_sector_info("TESTSYM")
    assert info is not None
    assert info.sector_index == "XTEST.IS"
    assert info.sector_name == "Test Sektoru"


def test_default_sector_map_has_known_symbols():
    mappings = list_sector_mappings()
    assert isinstance(mappings, dict)
