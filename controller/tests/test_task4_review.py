"""Task 4 independent review: temporary files only, no stand access."""
import errno
import hashlib
import json
import os
import socket
from pathlib import Path

import pytest

from contour.packages import (
    PackageError,
    claim_package,
    compute_source_checksum,
    validate_package,
)

ALLOWED = frozenset({"kb", "klon_opl"})


def package(tmp_path, name="pkg", **overrides):
    root = tmp_path / name
    (root / "src").mkdir(parents=True)
    (root / "src" / "Configuration.xml").write_bytes(b"<Configuration/>")
    (root / "src" / "Module.bsl").write_bytes(b"Procedure Test()\nEndProcedure")
    (root / "READY").write_bytes(b"")
    manifest = dict(
        schema_version=1,
        database="kb",
        export_id="export-1",
        started_at="2026-10-14T00:00:00Z",
        finished_at="2026-10-14T00:01:00Z",
        platform_version="8.3.25",
        operations={
            "DumpConfigToFiles": "ok",
            "UpdateCfg": "ok",
            "UpdateDBCfg": "ok",
        },
        file_count=2,
        checksum=compute_source_checksum(root),
    )
    manifest.update(overrides)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


@pytest.mark.parametrize("version", [True, 1.9, "1"])
def test_manifest_schema_requires_an_integer_not_coercion(tmp_path, version):
    root = package(tmp_path, schema_version=version)
    with pytest.raises(PackageError):
        validate_package(root, ALLOWED)


@pytest.mark.parametrize("count", [-1, 999, True])
def test_manifest_file_count_must_match_source_files(tmp_path, count):
    root = package(tmp_path, file_count=count)
    with pytest.raises(PackageError):
        validate_package(root, ALLOWED)


@pytest.mark.parametrize("export_id", [None, {}, []])
def test_export_id_requires_nonempty_text(tmp_path, export_id):
    root = package(tmp_path, export_id=export_id)
    with pytest.raises(PackageError):
        validate_package(root, ALLOWED)


@pytest.mark.parametrize(
    "operations",
    [{}, {"DumpConfigToFiles": "failed"}, {"DumpConfigToFiles": None}],
)
def test_dump_must_be_present_and_successful(tmp_path, operations):
    root = package(tmp_path, operations=operations)
    with pytest.raises(PackageError):
        validate_package(root, ALLOWED)


def test_checksum_matches_independent_golden_algorithm(tmp_path):
    root = package(tmp_path)
    files = [
        ("src/Configuration.xml", b"<Configuration/>"),
        ("src/Module.bsl", b"Procedure Test()\nEndProcedure"),
    ]
    lines = [
        f"{name}\0{len(data)}\0{hashlib.sha256(data).hexdigest()}"
        for name, data in files
    ]
    expected = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    assert compute_source_checksum(root) == expected


def test_identical_redelivery_claim_is_idempotent(tmp_path):
    first = package(tmp_path, "first")
    second = package(tmp_path, "second")
    a, b = validate_package(first, ALLOWED), validate_package(second, ALLOWED)
    dest = claim_package(a, tmp_path / "accepted")
    assert claim_package(b, tmp_path / "accepted") == dest
    assert validate_package(dest, ALLOWED).revision == a.revision


def test_content_mutation_alone_is_not_rehashed_on_claim(tmp_path):
    """Claim uses verify_checksum=False; content drift without manifest change is allowed."""
    root = package(tmp_path)
    valid = validate_package(root, ALLOWED)
    (root / "src" / "Module.bsl").write_bytes(b"changed after validation")
    dest = claim_package(valid, tmp_path / "accepted")
    assert dest.name == f"ready-{valid.manifest.export_id}"


def test_manifest_change_after_validation_is_not_claimed(tmp_path):
    root = package(tmp_path)
    valid = validate_package(root, ALLOWED)
    raw = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    raw["database"] = "klon_opl"
    (root / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(PackageError):
        claim_package(valid, tmp_path / "accepted")


def test_claim_of_same_object_after_success_is_recoverable(tmp_path):
    root = package(tmp_path)
    valid = validate_package(root, ALLOWED)
    dest = claim_package(valid, tmp_path / "accepted")
    assert claim_package(valid, tmp_path / "accepted") == dest


def test_different_content_cannot_overwrite_existing_claim(tmp_path):
    first = package(tmp_path, "first")
    second = package(tmp_path, "second")
    (second / "src" / "Module.bsl").write_bytes(b"different export")
    raw = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    raw["checksum"] = compute_source_checksum(second)
    (second / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    a, b = validate_package(first, ALLOWED), validate_package(second, ALLOWED)
    dest = claim_package(a, tmp_path / "accepted")
    with pytest.raises(PackageError):
        claim_package(b, tmp_path / "accepted")
    assert validate_package(dest, ALLOWED).revision == a.revision


def test_cross_filesystem_rename_never_falls_back_to_partial_copy(tmp_path, monkeypatch):
    root = package(tmp_path)
    valid = validate_package(root, ALLOWED)

    def exdev(self, dest):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(Path, "replace", exdev)
    with pytest.raises((PackageError, OSError)):
        claim_package(valid, tmp_path / "accepted")
    assert root.is_dir()
    assert not (tmp_path / "accepted" / "ready-export-1").exists()


def test_bad_json_is_rejected(tmp_path):
    root = package(tmp_path)
    (root / "manifest.json").write_text("{", encoding="utf-8")
    with pytest.raises(PackageError):
        validate_package(root, ALLOWED)


@pytest.mark.skipif(os.name != "posix", reason="Linux filesystem case")
def test_root_symlink_is_not_a_real_claimed_package(tmp_path):
    original = package(tmp_path, "original")
    link = tmp_path / "root-alias"
    link.symlink_to(original, target_is_directory=True)
    with pytest.raises(PackageError):
        validate_package(link, ALLOWED)


@pytest.mark.skipif(os.name != "posix", reason="Linux filesystem case")
def test_fifo_cannot_be_accepted_unhashed(tmp_path):
    root = package(tmp_path)
    os.mkfifo(root / "src" / "unhashed-fifo")
    with pytest.raises(PackageError):
        validate_package(root, ALLOWED)


@pytest.mark.skipif(os.name != "posix", reason="Linux filesystem case")
def test_unix_socket_cannot_be_accepted_unhashed(tmp_path):
    root = package(tmp_path)
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(root / "src" / "unhashed-socket"))
        with pytest.raises(PackageError):
            validate_package(root, ALLOWED)
