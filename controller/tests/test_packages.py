from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from contour.packages import (
    PackageError,
    artifact_identity,
    claim_package,
    compute_source_checksum,
    validate_package,
)

ALLOWED = frozenset({"kb", "klon_opl"})


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _checksum_for(root: Path) -> str:
    return compute_source_checksum(root)


def _make_package(
    tmp_path: Path,
    *,
    name: str = "pkg",
    database: str = "kb",
    export_id: str = "export-1",
    ready: bool = True,
    manifest: bool = True,
    configuration_xml: bool = True,
    checksum: str | None = None,
    schema_version: int = 1,
    extra_files: dict[str, bytes] | None = None,
    manifest_overrides: dict | None = None,
) -> Path:
    root = tmp_path / name
    files: dict[str, bytes] = {
        "src/Catalogs/Foo.xml": b"<Catalog/>",
    }
    if configuration_xml:
        files["src/Configuration.xml"] = b"<Configuration/>"
    if extra_files:
        files.update(extra_files)
    _write_tree(root, files)
    if ready:
        (root / "READY").write_text("", encoding="utf-8")
    if manifest:
        if database == "kb":
            operations = {
                "DumpConfigToFiles": "ok",
                "UpdateCfg": "ok",
                "UpdateDBCfg": "ok",
            }
        else:
            operations = {"DumpConfigToFiles": "ok"}
        src_file_count = sum(1 for rel in files if rel.startswith("src/"))
        body = {
            "schema_version": schema_version,
            "database": database,
            "export_id": export_id,
            "started_at": "2026-10-14T00:00:00Z",
            "finished_at": "2026-10-14T00:01:00Z",
            "platform_version": "8.3.25",
            "operations": operations,
            "file_count": src_file_count,
            "checksum": checksum if checksum is not None else _checksum_for(root),
        }
        if manifest_overrides:
            body.update(manifest_overrides)
        (root / "manifest.json").write_text(
            json.dumps(body, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return root


def test_valid_package_returns_database_export_id_and_checksum(tmp_path: Path):
    pkg = _make_package(tmp_path)
    before = {
        p.relative_to(pkg).as_posix(): _sha256_file(p)
        for p in pkg.rglob("*")
        if p.is_file()
    }
    validated = validate_package(pkg, ALLOWED)
    assert validated.manifest.database == "kb"
    assert validated.manifest.export_id == "export-1"
    assert validated.manifest.checksum == _checksum_for(pkg)
    assert validated.revision == f"{validated.manifest.checksum}:{validated.manifest.export_id}"
    after = {
        p.relative_to(pkg).as_posix(): _sha256_file(p)
        for p in pkg.rglob("*")
        if p.is_file()
    }
    assert before == after


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"ready": False}, "READY"),
        ({"manifest": False}, "manifest"),
        ({"configuration_xml": False}, "Configuration.xml"),
    ],
)
def test_missing_required_entries_rejected(tmp_path: Path, kwargs, match):
    pkg = _make_package(tmp_path, **kwargs)
    with pytest.raises(PackageError, match=match):
        validate_package(pkg, ALLOWED)


def test_checksum_mismatch_rejected(tmp_path: Path):
    pkg = _make_package(tmp_path, checksum="0" * 64)
    with pytest.raises(PackageError, match="checksum"):
        validate_package(pkg, ALLOWED)


def test_unsupported_schema_version_rejected(tmp_path: Path):
    pkg = _make_package(tmp_path, schema_version=99)
    with pytest.raises(PackageError, match="schema_version"):
        validate_package(pkg, ALLOWED)


def test_unexpected_database_rejected(tmp_path: Path):
    pkg = _make_package(tmp_path, database="ubd")
    with pytest.raises(PackageError, match="database"):
        validate_package(pkg, ALLOWED)


def test_database_name_path_traversal_rejected(tmp_path: Path):
    pkg = _make_package(tmp_path, database="../kb")
    with pytest.raises(PackageError, match="database"):
        validate_package(pkg, ALLOWED)


def test_symlink_escape_in_package_rejected(tmp_path: Path):
    pkg = _make_package(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    link = pkg / "src" / "evil-link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(PackageError, match="path|symlink|escape"):
        validate_package(pkg, ALLOWED)


def test_identical_deliveries_share_artifact_identity(tmp_path: Path):
    a = _make_package(tmp_path, name="a", export_id="export-9")
    b = _make_package(tmp_path, name="b", export_id="export-9")
    va = validate_package(a, ALLOWED)
    vb = validate_package(b, ALLOWED)
    assert artifact_identity(va) == artifact_identity(vb)
    assert va.manifest.checksum == vb.manifest.checksum


def test_claim_package_uses_atomic_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pkg = _make_package(tmp_path, name="incoming")
    validated = validate_package(pkg, ALLOWED)
    accepted = tmp_path / "accepted"
    accepted.mkdir()
    calls: list[tuple[Path, Path]] = []
    real_replace = Path.replace

    def tracking_replace(self: Path, target: Path):
        calls.append((self, Path(target)))
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", tracking_replace)
    dest = claim_package(validated, accepted)
    assert dest == accepted / f"ready-{validated.manifest.export_id}"
    assert dest.is_dir()
    assert (dest / "READY").is_file()
    assert not pkg.exists()
    assert calls, "claim must use Path.replace for same-filesystem atomic rename"
    assert calls[0][0] == validated.path
    assert calls[0][1] == dest
