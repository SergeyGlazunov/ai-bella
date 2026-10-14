from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUPPORTED_MANIFEST_SCHEMA = 1
_EXCLUDED_FROM_CHECKSUM = frozenset({"READY", "manifest.json"})
_SUCCESS_OPERATION = "ok"
# Manifest schema v1 operation contract (Windows exporter Task 9 must match):
# - kb: DumpConfigToFiles + UpdateCfg + UpdateDBCfg must all be "ok"
# - klon_opl: DumpConfigToFiles must be "ok" (no configuration store update)
_REQUIRED_OPS_BY_DATABASE = {
    "kb": frozenset({"DumpConfigToFiles", "UpdateCfg", "UpdateDBCfg"}),
    "klon_opl": frozenset({"DumpConfigToFiles"}),
}
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")
_EXPORT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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


def _require_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PackageError(f"{field} must be an integer")
    return value


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise PackageError(f"{field} must be a string")
    if value == "":
        raise PackageError(f"{field} must be non-empty")
    return value


def _is_symlink(path: Path) -> bool:
    try:
        return bool(path.is_symlink() or os.path.islink(path))
    except OSError:
        return False


def _assert_real_directory(path: Path, *, label: str) -> None:
    if _is_symlink(path):
        raise PackageError(f"{label} must be a real directory, not a symlink")
    if not path.is_dir():
        raise PackageError(f"{label} is not a directory: {path}")


def _assert_safe_entry(root: Path, path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise PackageError(f"unsafe path: {path}") from exc
    if stat.S_ISLNK(mode) or _is_symlink(path):
        raise PackageError(
            f"symlink not allowed: {path.relative_to(root).as_posix()}"
        )
    if stat.S_ISDIR(mode):
        return
    if stat.S_ISREG(mode):
        try:
            resolved = path.resolve(strict=True)
            common = os.path.commonpath([str(root.resolve()), str(resolved)])
        except (OSError, ValueError) as exc:
            raise PackageError(f"path escapes package root: {path}") from exc
        if common != str(root.resolve()):
            raise PackageError(f"path escapes package root: {path}")
        return
    raise PackageError(
        f"unsupported file type: {path.relative_to(root).as_posix()}"
    )


def _reject_unsafe_paths(package_root: Path) -> None:
    _assert_real_directory(package_root, label="package root")
    root_resolved = package_root.resolve()
    for path in package_root.rglob("*"):
        _assert_safe_entry(root_resolved, path)


def count_source_files(package_root: Path) -> int:
    """Count regular files under ``src/`` (READY/manifest are outside src)."""
    src = package_root / "src"
    if _is_symlink(src) or not src.is_dir():
        raise PackageError("missing src directory")
    count = 0
    for path in src.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
            continue
        if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            raise PackageError(
                f"unsupported file type: {path.relative_to(package_root).as_posix()}"
            )
        count += 1
    return count


def compute_source_checksum(package_root: Path) -> str:
    """SHA-256 over sorted relative path, size and file SHA-256 lines.

    ``READY`` and ``manifest.json`` are excluded. Only regular files are hashed;
    special files must be rejected by structure checks before calling this.
    """
    _assert_real_directory(package_root, label="package root")
    root_resolved = package_root.resolve()
    lines: list[str] = []
    for path in sorted(
        package_root.rglob("*"),
        key=lambda p: p.relative_to(package_root).as_posix(),
    ):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
            continue
        if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            raise PackageError(
                f"unsupported file type: {path.relative_to(package_root).as_posix()}"
            )
        rel = path.relative_to(package_root).as_posix()
        if rel in _EXCLUDED_FROM_CHECKSUM:
            continue
        _assert_safe_entry(root_resolved, path)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        lines.append(f"{rel}\0{len(data)}\0{digest}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def artifact_identity(package: ValidatedPackage) -> str:
    """Stable identity for identical deliveries of the same export."""
    m = package.manifest
    return f"{m.database}:{m.export_id}:{m.checksum}"


def _parse_operations(raw: Any, database: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise PackageError("operations must be a mapping")
    ops: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise PackageError("operations keys must be non-empty strings")
        if not isinstance(value, str):
            raise PackageError(f"operations.{key} must be a string")
        ops[key] = value
    required = _REQUIRED_OPS_BY_DATABASE.get(database)
    if required is None:
        raise PackageError(f"unexpected database: {database}")
    for name in sorted(required):
        if name not in ops:
            raise PackageError(f"operations missing required {name}")
        if ops[name] != _SUCCESS_OPERATION:
            raise PackageError(
                f"operations.{name} must be {_SUCCESS_OPERATION!r}, got {ops[name]!r}"
            )
    return ops


def _parse_manifest(raw: dict[str, Any]) -> PackageManifest:
    schema_version = _require_int(raw.get("schema_version"), "schema_version")
    database = _require_str(raw.get("database"), "database")
    export_id = _require_str(raw.get("export_id"), "export_id")
    started_at = _require_str(raw.get("started_at"), "started_at")
    finished_at = _require_str(raw.get("finished_at"), "finished_at")
    platform_version = _require_str(raw.get("platform_version"), "platform_version")
    checksum = _require_str(raw.get("checksum"), "checksum")
    file_count = _require_int(raw.get("file_count"), "file_count")

    if schema_version != SUPPORTED_MANIFEST_SCHEMA:
        raise PackageError(f"unsupported schema_version: {schema_version}")
    if not _EXPORT_ID_RE.fullmatch(export_id):
        raise PackageError("invalid export_id")
    if ".." in database or "/" in database or "\\" in database:
        raise PackageError(f"invalid database name: {database}")
    if not _TIMESTAMP_RE.fullmatch(started_at):
        raise PackageError("started_at must be an ISO-8601 timestamp")
    if not _TIMESTAMP_RE.fullmatch(finished_at):
        raise PackageError("finished_at must be an ISO-8601 timestamp")
    if not _CHECKSUM_RE.fullmatch(checksum):
        raise PackageError("checksum must be a lowercase sha256 hex digest")
    if file_count < 0:
        raise PackageError("file_count must be non-negative")

    operations = _parse_operations(raw.get("operations"), database)
    return PackageManifest(
        schema_version=schema_version,
        database=database,
        export_id=export_id,
        started_at=started_at,
        finished_at=finished_at,
        platform_version=platform_version,
        operations=operations,
        file_count=file_count,
        checksum=checksum,
    )


def validate_package(
    path: Path, allowed_databases: frozenset[str]
) -> ValidatedPackage:
    root = Path(path)
    _assert_real_directory(root, label="package root")

    ready = root / "READY"
    if not ready.is_file() or _is_symlink(ready):
        raise PackageError("missing READY")
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file() or _is_symlink(manifest_path):
        raise PackageError("missing manifest.json")
    config_xml = root / "src" / "Configuration.xml"
    if not config_xml.is_file() or _is_symlink(config_xml):
        raise PackageError("missing src/Configuration.xml")

    _reject_unsafe_paths(root)

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PackageError(f"invalid manifest.json: {exc}") from exc
    if not isinstance(raw, dict):
        raise PackageError("manifest.json must be a JSON object")

    manifest = _parse_manifest(raw)
    if manifest.database not in allowed_databases:
        raise PackageError(f"unexpected database: {manifest.database}")

    actual_count = count_source_files(root)
    if actual_count != manifest.file_count:
        raise PackageError(
            f"file_count mismatch: manifest={manifest.file_count} actual={actual_count}"
        )

    actual = compute_source_checksum(root)
    if actual != manifest.checksum:
        raise PackageError("checksum mismatch")

    revision = f"{manifest.checksum}:{manifest.export_id}"
    return ValidatedPackage(path=root, manifest=manifest, revision=revision)


def _same_claim(existing: ValidatedPackage, expected: ValidatedPackage) -> bool:
    return (
        existing.revision == expected.revision
        and existing.manifest.database == expected.manifest.database
        and existing.manifest.export_id == expected.manifest.export_id
        and existing.manifest.checksum == expected.manifest.checksum
    )


def _verify_unchanged(package: ValidatedPackage) -> None:
    """Reject claim if files/manifest changed after validate_package."""
    current = validate_package(
        package.path, frozenset({package.manifest.database})
    )
    if current.revision != package.revision or current.manifest != package.manifest:
        raise PackageError("package changed after validation")


def claim_package(package: ValidatedPackage, accepted_root: Path) -> Path:
    """Atomically publish a validated package into ``accepted_root``.

    Ownership protocol: the caller must stop writing the inbox path after
    ``validate_package``. ``claim_package`` re-validates the on-disk snapshot
    against the ``ValidatedPackage`` before rename. Identical redelivery or a
    retry after a successful rename is idempotent when the accepted package
    matches the verified identity; differing content with the same export_id
    is a collision. Cross-filesystem rename is not emulated with copy.
    """
    accepted_root = Path(accepted_root)
    accepted_root.mkdir(parents=True, exist_ok=True)
    dest = accepted_root / f"ready-{package.manifest.export_id}"

    if _is_symlink(dest):
        raise PackageError(
            f"claim collision for export_id={package.manifest.export_id!r}"
        )
    if dest.exists():
        existing = validate_package(
            dest, frozenset({package.manifest.database})
        )
        if _same_claim(existing, package):
            return dest
        raise PackageError(
            f"claim collision for export_id={package.manifest.export_id!r}"
        )

    if not package.path.exists():
        raise PackageError(
            "package path missing and no matching accepted claim"
        )

    _verify_unchanged(package)
    try:
        package.path.replace(dest)
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.EXDEV:
            raise PackageError(
                "cross-filesystem rename is not supported"
            ) from exc
        raise
    return dest
