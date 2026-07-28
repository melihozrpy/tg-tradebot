from __future__ import annotations

import json
import zipfile

import pytest

from scripts.build_release import (
    MANIFEST_NAME,
    ReleaseSafetyError,
    build_release,
    verify_archive,
)


def _clean_source(tmp_path):
    root = tmp_path / "clean-project"
    (root / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "migrations" / "versions").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "app" / "main.py").write_text("APP_NAME = 'MERGEN QUANT'\n", encoding="utf-8")
    (root / "tests" / "test_smoke.py").write_text("def test_smoke(): assert True\n", encoding="utf-8")
    (root / "migrations" / "versions" / "0001.py").write_text("revision = '0001'\n", encoding="utf-8")
    (root / "scripts" / "build_release.py").write_text("# release helper placeholder\n", encoding="utf-8")
    (root / "README.md").write_text("# MERGEN QUANT\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='mergen-quant'\nversion='1.0'\n", encoding="utf-8")
    (root / ".env.example").write_text("TELEGRAM_BOT_TOKEN=replace_with_your_token\n", encoding="utf-8")
    (root / ".gitignore").write_text(".env\n*.db\n", encoding="utf-8")
    (root / ".releaseignore").write_text(".env\n*.db\n", encoding="utf-8")
    return root


def test_22_release_refuses_source_containing_dotenv(tmp_path):
    source = _clean_source(tmp_path)
    (source / ".env").write_text("TOKEN=not-printed\n", encoding="utf-8")
    with pytest.raises(ReleaseSafetyError, match="ortam dosyası"):
        build_release(source, tmp_path / "release.zip")


def test_23_release_refuses_source_containing_real_database(tmp_path):
    source = _clean_source(tmp_path)
    (source / "runtime.db").write_bytes(b"SQLite format 3\x00")
    with pytest.raises(ReleaseSafetyError, match="veritabanı"):
        build_release(source, tmp_path / "release.zip")


def test_24_release_stops_when_secret_pattern_is_detected(tmp_path):
    source = _clean_source(tmp_path)
    fake_secret = "gsk_" + "1234567890abcdefghijklmnop"
    (source / "app" / "secret.py").write_text(f"KEY = '{fake_secret}'\n", encoding="utf-8")
    with pytest.raises(ReleaseSafetyError, match="olası gizli değer"):
        build_release(source, tmp_path / "release.zip")


def test_25_clean_release_zip_is_created_successfully(tmp_path):
    source = _clean_source(tmp_path)
    archive, archive_hash, manifest = build_release(source, tmp_path / "release.zip")
    assert archive.exists() and archive.stat().st_size > 0
    assert len(archive_hash) == 64
    assert manifest["file_count"] >= 8
    assert archive.with_suffix(".zip.sha256").exists()


def test_26_release_zip_excludes_runtime_cache_pyc_database_and_dotenv(tmp_path):
    source = _clean_source(tmp_path)
    (source / ".venv" / "Lib").mkdir(parents=True)
    (source / ".venv" / "Lib" / "runtime.pyc").write_bytes(b"compiled")
    (source / "app" / "__pycache__").mkdir()
    (source / "app" / "__pycache__" / "main.pyc").write_bytes(b"compiled")
    (source / "data" / "cache").mkdir(parents=True)
    (source / "data" / "cache" / "chart.png").write_bytes(b"png")
    archive, _, _ = build_release(source, tmp_path / "release.zip")
    with zipfile.ZipFile(archive) as zipped:
        lowered = [name.casefold() for name in zipped.namelist()]
    assert not any(".venv" in name or "__pycache__" in name or name.endswith(".pyc") for name in lowered)
    assert not any(name.endswith((".db", ".sqlite", ".log")) for name in lowered)
    assert ".env" not in lowered
    assert ".env.example" in lowered


def test_27_manifest_and_file_hash_verification_passes(tmp_path):
    source = _clean_source(tmp_path)
    archive, _, original = build_release(source, tmp_path / "release.zip")
    verified = verify_archive(archive)
    assert verified == original
    with zipfile.ZipFile(archive) as zipped:
        manifest = json.loads(zipped.read(MANIFEST_NAME).decode("utf-8"))
        assert manifest["file_count"] == len(manifest["files"])
        assert all(len(item["sha256"]) == 64 for item in manifest["files"])


def test_28_release_includes_documented_example_png(tmp_path):
    source = _clean_source(tmp_path)
    (source / "docs" / "examples").mkdir(parents=True)
    (source / "docs" / "examples" / "smxm_report.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    archive, _, _ = build_release(source, tmp_path / "release.zip")
    with zipfile.ZipFile(archive) as zipped:
        assert "docs/examples/smxm_report.png" in zipped.namelist()


def test_29_release_skips_isolated_pytest_runtime_database(tmp_path):
    source = _clean_source(tmp_path)
    (source / ".test-tmp").mkdir()
    (source / ".test-tmp" / "runtime.db").write_bytes(b"SQLite format 3\x00")
    archive, _, _ = build_release(source, tmp_path / "release.zip")
    with zipfile.ZipFile(archive) as zipped:
        assert not any(name.startswith(".test-tmp/") for name in zipped.namelist())


def test_30_release_accepts_runtime_settings_secret_reference(tmp_path):
    source = _clean_source(tmp_path)
    (source / "app" / "provider.py").write_text(
        "client = Client(api_key=settings.licensed_market_data_api_key)\n",
        encoding="utf-8",
    )
    archive, _, _ = build_release(source, tmp_path / "release.zip")
    assert archive.exists()
