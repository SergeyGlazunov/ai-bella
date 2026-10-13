from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUPPORTED_MANIFEST_SCHEMA = 1
_EXCLUDED_FROM_CHECKSUM = frozenset({"READY", "manifest.json"})


class PackageError(ValueError):
    """Package is incomplete, unsafe or fails validation."""


@dataclass(frozen=True)
class PackageManifest:
    schema_version: int
    database: str
    export_id: str
    started_at: str
    finished_at: str
    platform_version: str
    operations: dict[str, str]
    file_count: int
    checksum: str


@dataclass(frozen=True)
class ValidatedPackage:
    path: Path
    manifest: PackageManifest
    revision: str


def compute_source_checksum(package_root: Path) -> str:
    """SHA-256 over sorted relative path, size and file SHA-256 lines.

    ``READY`` and ``manifest.json`` are excluded. Paths use POSIX separators.
    """
    root = package_root.resolve()
    lines: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in _EXCLUDED_FROM_CHECKSUM:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        size = path.stat().st_size
        lines.append(f"{rel}\0{size}\0{digest}")
    payload = "\n".join(lines).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def artifact_identity(package: ValidatedPackage) -> str:
    """Stable identity for identical deliveries of the same export."""
    m = package.manifest
    return f"{m.database}:{m.export_id}:{m.checksum}"


def _require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise PackageError(f"missing {label}")


def _parse_manifest(raw: dict[str, Any]) -> PackageManifest:
    try:
        schema_version = int(raw["schema_version"])
        operations = raw["operations"]
        if not isinstance(operations, dict):
            raise PackageError("manifest.operations must be a mapping")
        ops = {str(k): str(v) for k, v in operations.items()}
        return PackageManifest(
            schema_version=schema_version,
            database=str(raw["database"]),
            export_id=str(raw["export_id"]),
            started_at=str(raw["started_at"]),
            finished_at=str(raw["finished_at"]),
            platform_version=str(raw["platform_version"]),
            operations=ops,
            file_count=int(raw["file_count"]),
            checksum=str(raw["checksum"]),
        )
    except KeyError as exc:
        raise PackageError(f"manifest missing field: {exc.args[0]}") from exc
    except (TypeError, ValueError) as exc:
        raise PackageError(f"invalid manifest: {exc}") from exc


def _reject_unsafe_paths(package_root: Path) -> None:
    root = package_root.resolve()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise PackageError(
                f"symlink not allowed: {path.relative_to(root).as_posix()}"
            )
        try:
            resolved = path.resolve()
            common = os.path.commonpath([str(root), str(resolved)])
        except (OSError, ValueError) as exc:
            raise PackageError(f"path escapes package root: {path}") from exc
        if common != str(root):
            raise PackageError(f"path escapes package root: {path}")


def validate_package(
    path: Path, allowed_databases: frozenset[str]
) -> ValidatedPackage:
    root = Path(path)
    if not root.is_dir():
        raise PackageError(f"package path is not a directory: {root}")

    _require_file(root / "READY", "READY")
    manifest_path = root / "manifest.json"
    _require_file(manifest_path, "manifest.json")
    _require_file(root / "src" / "Configuration.xml", "src/Configuration.xml")

    _reject_unsafe_paths(root)

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PackageError(f"invalid manifest.json: {exc}") from exc
    if not isinstance(raw, dict):
        raise PackageError("manifest.json must be a JSON object")

    manifest = _parse_manifest(raw)
    if manifest.schema_version != SUPPORTED_MANIFEST_SCHEMA:
        raise PackageError(
            f"unsupported schema_version: {manifest.schema_version}"
        )
    if not manifest.export_id or "/" in manifest.export_id or "\\" in manifest.export_id:
        raise PackageError("invalid export_id")
    if ".." in manifest.database or "/" in manifest.database or "\\" in manifest.database:
        raise PackageError(f"invalid database name: {manifest.database}")
    if manifest.database not in allowed_databases:
        raise PackageError(f"unexpected database: {manifest.database}")

    actual = compute_source_checksum(root)
    if actual != manifest.checksum:
        raise PackageError("checksum mismatch")

    revision = f"{manifest.checksum}:{manifest.export_id}"
    return ValidatedPackage(path=root, manifest=manifest, revision=revision)


def claim_package(package: ValidatedPackage, accepted_root: Path) -> Path:
    """Atomically move a validated package into ``accepted_root`` via rename/replace."""
    accepted_root = Path(accepted_root)
    accepted_root.mkdir(parents=True, exist_ok=True)
    dest = accepted_root / f"ready-{package.manifest.export_id}"
    if dest.exists():
        raise PackageError(f"accepted package already exists: {dest}")
    # Path.replace is an atomic rename on the same filesystem.
    package.path.replace(dest)
    return dest
