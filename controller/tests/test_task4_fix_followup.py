import json
import os
import subprocess
from pathlib import Path

import pytest

from contour.packages import PackageError, compute_source_checksum, validate_package


def package(parent, name="pkg", **overrides):
    root = parent / name
    (root / "src").mkdir(parents=True)
    (root / "src" / "Configuration.xml").write_bytes(b"<Configuration/>")
    (root / "READY").write_bytes(b"")
    raw = dict(
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
        file_count=1,
        checksum=compute_source_checksum(root),
    )
    raw.update(overrides)
    (root / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    return root


@pytest.mark.parametrize(
    "overrides",
    [
        {"started_at": "2026-99-99T25:70:90Z"},
        {"started_at": "2026-02-30T00:00:00Z"},
        {"finished_at": "2026-10-13T00:00:00Z"},
    ],
)
def test_manifest_timestamps_are_valid_and_ordered(tmp_path, overrides):
    root = package(tmp_path, **overrides)
    with pytest.raises(PackageError):
        validate_package(root, frozenset({"kb"}))


def test_manifest_timestamps_compare_across_timezones(tmp_path):
    """Offsets must compare as absolute moments, not as text."""
    root = package(
        tmp_path,
        started_at="2026-10-14T10:00:00+10:00",
        finished_at="2026-10-14T00:30:00+00:00",  # 30m after started
    )
    result = validate_package(root, frozenset({"kb"}))
    assert result.manifest.started_at.endswith("+10:00")


def test_allowed_extra_package_profile_is_not_rejected_by_name(tmp_path):
    root = package(
        tmp_path, database="extra_db", operations={"DumpConfigToFiles": "ok"}
    )
    result = validate_package(root, frozenset({"extra_db"}))
    assert result.manifest.database == "extra_db"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction case")
def test_windows_root_junction_is_rejected(tmp_path):
    original = package(tmp_path, name="original")
    alias = tmp_path / "junction"
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(alias), str(original)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        pytest.skip("NTFS junction unavailable")
    try:
        with pytest.raises(PackageError):
            validate_package(alias, frozenset({"kb"}))
    finally:
        if alias.exists():
            alias.rmdir()
