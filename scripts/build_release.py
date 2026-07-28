from __future__ import annotations

"""MERGEN QUANT güvenli release arşivi üreticisi.

Kaynak klasörde gerçek ``.env`` veya veritabanı varsa fail-closed davranır.
Arşive yalnızca açıkça izin verilen kaynak/test/migration/dokümantasyon
dosyaları alınır. Secret eşleşmesinin değeri hiçbir hata/çıktıda yazılmaz.
"""

import argparse
import hashlib
import json
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable


DEFAULT_ARCHIVE_NAME = "mergen-quant-stage5g-backtest-paper-validation.zip"
MANIFEST_NAME = "RELEASE_MANIFEST.json"

ALLOWED_DIRECTORIES = {"app", "tests", "migrations", "scripts", "data", "data_csv", "docs"}
ALLOWED_ROOT_FILES = {
    ".env.example",
    ".gitignore",
    ".releaseignore",
    "README.md",
    "pyproject.toml",
    "alembic.ini",
    "Dockerfile",
    "docker-compose.yml",
    "docker-entrypoint.sh",
    "run_bot.py",
}
ALLOWED_SUFFIXES = {".py", ".yaml", ".yml", ".toml", ".ini", ".md", ".txt", ".csv", ".json", ".example"}
SKIP_DIRECTORY_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".test-tmp",
    ".mypy_cache",
    ".ruff_cache",
    "cache",
    "logs",
    "build",
    "dist",
    "release",
}
DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
FORBIDDEN_ARCHIVE_PARTS = {".env", ".venv", "venv", "__pycache__", "cache", "logs"}
FORBIDDEN_ARCHIVE_SUFFIXES = {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".log", ".bak", ".backup"}

SECRET_PATTERNS = (
    re.compile(r"\bgsk_[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret(?:_key)?|access[_-]?token|bot[_-]?token|password)\b"
        r"\s*[:=]\s*[\"']?([A-Za-z0-9_./:+-]{16,})"
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
)
PLACEHOLDER_TOKENS = ("example", "replace", "placeholder", "your_", "your-", "change_me", "changeme", "dummy", "test-token")


class ReleaseSafetyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArchiveEntry:
    path: str
    sha256: str
    size: int


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative(path: Path, source: Path) -> str:
    return path.relative_to(source).as_posix()


def _walk_without_runtime_dirs(source: Path) -> Iterable[Path]:
    for path in source.rglob("*"):
        relative_parts = path.relative_to(source).parts
        if any(part in SKIP_DIRECTORY_NAMES for part in relative_parts):
            continue
        if path.is_file():
            yield path


def assert_source_is_safe(source: Path) -> None:
    """Gerçek ortam dosyası/DB görüldüğünde paketlemeyi tamamen reddeder."""

    for path in _walk_without_runtime_dirs(source):
        relative = _relative(path, source)
        name = path.name.casefold()
        suffix = path.suffix.casefold()
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            raise ReleaseSafetyError(f"Release reddedildi: ortam dosyası bulundu ({relative}).")
        if suffix in DATABASE_SUFFIXES or name.endswith((".db-wal", ".db-shm", ".db-journal")):
            raise ReleaseSafetyError(f"Release reddedildi: veritabanı dosyası bulundu ({relative}).")


def _is_allowed(path: Path, source: Path) -> bool:
    relative = path.relative_to(source)
    if len(relative.parts) == 1:
        return relative.name in ALLOWED_ROOT_FILES
    if relative.parts[0] not in ALLOWED_DIRECTORIES:
        return False
    if relative.parts[0] == "docs" and path.suffix.casefold() == ".png":
        return True
    return path.suffix.casefold() in ALLOWED_SUFFIXES or path.name in {"Dockerfile"}


def collect_release_files(source: Path) -> list[Path]:
    files = [path for path in _walk_without_runtime_dirs(source) if _is_allowed(path, source)]
    files.sort(key=lambda path: _relative(path, source).casefold())
    if not files:
        raise ReleaseSafetyError("Release reddedildi: izin verilen kaynak dosyası bulunamadı.")
    return files


def _looks_like_placeholder(text: str) -> bool:
    lowered = text.casefold()
    return any(token in lowered for token in PLACEHOLDER_TOKENS)


def _looks_like_runtime_reference(match: re.Match[str]) -> bool:
    """Ayar nesnesi/env referansını gömülü gizli değer sanma."""

    value = match.group(1) if match.lastindex else match.group(0)
    normalized = value.strip("\"'").casefold()
    return normalized.startswith(
        ("settings.", "self.settings.", "config.", "os.environ", "getenv(", "${")
    )


def scan_for_secrets(files: Iterable[Path], source: Path) -> None:
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                context = text[max(0, match.start() - 40) : min(len(text), match.end() + 40)]
                if _looks_like_placeholder(context) or _looks_like_runtime_reference(match):
                    continue
                # Eşleşen gizli değer bilinçli olarak çıktıya eklenmez.
                raise ReleaseSafetyError(
                    f"Release reddedildi: olası gizli değer bulundu ({_relative(path, source)})."
                )


def _assert_archive_path_safe(name: str) -> None:
    path = PurePosixPath(name)
    lowered_parts = {part.casefold() for part in path.parts}
    if lowered_parts & FORBIDDEN_ARCHIVE_PARTS:
        raise ReleaseSafetyError(f"Arşiv doğrulaması başarısız: yasaklı yol ({name}).")
    suffix = path.suffix.casefold()
    if suffix in FORBIDDEN_ARCHIVE_SUFFIXES:
        raise ReleaseSafetyError(f"Arşiv doğrulaması başarısız: yasaklı dosya türü ({name}).")
    if path.name.casefold().startswith(".env") and path.name != ".env.example":
        raise ReleaseSafetyError(f"Arşiv doğrulaması başarısız: ortam dosyası ({name}).")


def build_manifest(source: Path, files: list[Path]) -> tuple[dict, dict[str, bytes]]:
    payloads: dict[str, bytes] = {}
    entries: list[dict] = []
    for path in files:
        name = _relative(path, source)
        _assert_archive_path_safe(name)
        data = path.read_bytes()
        payloads[name] = data
        entries.append({"path": name, "sha256": _sha256_bytes(data), "size": len(data)})
    manifest = {
        "project": "MERGEN QUANT",
        "release": "Tüm Hisseler ve SMXM Günlük Rapor Sistemi",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "file_count": len(entries),
        "files": entries,
        "excluded": [
            ".env and credentials",
            "virtual environments and Python caches",
            "runtime caches, logs and generated charts",
            "databases, backups and user/portfolio data",
        ],
    }
    return manifest, payloads


def verify_archive(archive: Path) -> dict:
    with zipfile.ZipFile(archive, "r") as zipped:
        names = zipped.namelist()
        if len(names) != len(set(names)):
            raise ReleaseSafetyError("Arşiv doğrulaması başarısız: yinelenen dosya adı.")
        for name in names:
            _assert_archive_path_safe(name)
        if MANIFEST_NAME not in names:
            raise ReleaseSafetyError("Arşiv doğrulaması başarısız: manifest yok.")
        manifest = json.loads(zipped.read(MANIFEST_NAME).decode("utf-8"))
        expected_names = {item["path"] for item in manifest.get("files", [])} | {MANIFEST_NAME}
        if set(names) != expected_names:
            raise ReleaseSafetyError("Arşiv doğrulaması başarısız: manifest/dosya listesi uyuşmuyor.")
        for item in manifest["files"]:
            data = zipped.read(item["path"])
            if len(data) != item["size"] or _sha256_bytes(data) != item["sha256"]:
                raise ReleaseSafetyError(
                    f"Arşiv doğrulaması başarısız: dosya hash uyuşmazlığı ({item['path']})."
                )
        return manifest


def build_release(source: Path, output: Path) -> tuple[Path, str, dict]:
    source = source.resolve()
    output = output.resolve()
    if not source.is_dir():
        raise ReleaseSafetyError("Release reddedildi: kaynak klasör bulunamadı.")
    assert_source_is_safe(source)
    files = collect_release_files(source)
    scan_for_secrets(files, source)
    manifest, payloads = build_manifest(source, files)

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zipped:
        for name, data in payloads.items():
            zipped.writestr(name, data)
        zipped.writestr(MANIFEST_NAME, manifest_bytes)

    verified_manifest = verify_archive(output)
    archive_hash = sha256_file(output)
    hash_path = output.with_suffix(output.suffix + ".sha256")
    hash_path.write_text(f"{archive_hash}  {output.name}\n", encoding="ascii")
    return output, archive_hash, verified_manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MERGEN QUANT güvenli release ZIP üreticisi")
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path.cwd() / DEFAULT_ARCHIVE_NAME)
    parser.add_argument("--verify-only", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.verify_only:
            manifest = verify_archive(args.verify_only.resolve())
            print(f"Doğrulama başarılı: {manifest['file_count']} dosya")
            return 0
        output, archive_hash, manifest = build_release(args.source, args.output)
        print(f"Release hazır: {output}")
        print(f"Dosya sayısı: {manifest['file_count']}")
        print(f"SHA-256: {archive_hash}")
        return 0
    except (ReleaseSafetyError, OSError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
