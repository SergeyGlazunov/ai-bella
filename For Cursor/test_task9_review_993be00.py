"""Task 9 package export review checks. Temp dumps only; no 1C / live Docker."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from contour.packages import compute_source_checksum, validate_package

REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / "tools" / "common" / "Export-Package.psm1"


def _have_shell(exe: str) -> bool:
    return shutil.which(exe) is not None


def _ps_run(exe: str, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _ps_checksum(exe: str, package_root: Path) -> str:
    script = f"""
Import-Module {_quote(MODULE)} -Force
New-SourceChecksum -SourcePath {_quote(package_root)}
"""
    result = _ps_run(exe, script)
    if result.returncode != 0:
        raise AssertionError(
            f"{exe} checksum failed rc={result.returncode}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    assert lines, f"{exe} produced no checksum: {result.stdout!r}"
    return lines[-1]


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


@pytest.mark.parametrize(
    "label,files",
    [
        (
            "basic",
            {
                "src/Configuration.xml": b"<Configuration/>",
                "src/Catalogs/Foo.xml": b"<Catalog/>",
            },
        ),
        (
            "mixed-names",
            {
                "src/Configuration.xml": b"<Configuration/>",
                "src/Catalogs/Zebra.xml": b"<Z/>",
                "src/Catalogs/aTest.xml": b"<a/>",
                "src/Catalogs/_Special.xml": b"<_/>",
            },
        ),
    ],
)
@pytest.mark.parametrize(
    "shell",
    [
        pytest.param("powershell", marks=pytest.mark.skipif(
            not _have_shell("powershell"), reason="Windows PowerShell 5.1 missing"
        )),
        pytest.param("pwsh", marks=pytest.mark.skipif(
            not _have_shell("pwsh"), reason="PowerShell 7 missing"
        )),
    ],
)
def test_real_checksum_contract(tmp_path: Path, label: str, files: dict, shell: str):
    pkg = tmp_path / f"pkg-{label}-{shell}"
    _write_tree(pkg, files)
    (pkg / "READY").write_text("", encoding="ascii")
    (pkg / "manifest.json").write_text("{}", encoding="ascii")
    py = compute_source_checksum(pkg)
    ps = _ps_checksum(shell, pkg)
    assert ps == py, f"{shell}/{label}: ps={ps} py={py}"


@pytest.mark.parametrize(
    "shell",
    [
        pytest.param("powershell", marks=pytest.mark.skipif(
            not _have_shell("powershell"), reason="Windows PowerShell 5.1 missing"
        )),
        pytest.param("pwsh", marks=pytest.mark.skipif(
            not _have_shell("pwsh"), reason="PowerShell 7 missing"
        )),
    ],
)
def test_published_package_is_accepted_by_real_python_validator(
    tmp_path: Path, shell: str
):
    dump = tmp_path / "dump"
    inbox = tmp_path / "inbox"
    _write_tree(
        dump,
        {
            "Configuration.xml": b"<Configuration/>",
            "Catalogs/Zebra.xml": b"<Z/>",
            "Catalogs/aTest.xml": b"<a/>",
            "Catalogs/_Special.xml": b"<_/>",
        },
    )
    inbox.mkdir()
    script = f"""
Import-Module {_quote(MODULE)} -Force
$dump = {_quote(dump)}
$inbox = {_quote(inbox)}
$m = New-ExportManifest -Database 'klon_opl' -ExportId 'export-mixed' `
  -SourcePath $dump -Operations @{{ DumpConfigToFiles = 'ok' }} `
  -PlatformVersion '8.3.25' -StartedAt '2026-10-15T00:00:00Z' `
  -FinishedAt '2026-10-15T00:01:00Z'
$ready = Publish-ExportPackage -SourcePath $dump -DestinationRoot $inbox -Manifest $m
Write-Output $ready
"""
    result = _ps_run(shell, script)
    assert result.returncode == 0, result.stdout + result.stderr
    ready = Path([ln.strip() for ln in result.stdout.splitlines() if ln.strip()][-1])
    package = validate_package(ready, frozenset({"klon_opl"}))
    assert package.manifest.export_id == "export-mixed"
    assert (ready / "READY").is_file()


@pytest.mark.parametrize(
    "shell",
    [
        pytest.param("powershell", marks=pytest.mark.skipif(
            not _have_shell("powershell"), reason="Windows PowerShell 5.1 missing"
        )),
        pytest.param("pwsh", marks=pytest.mark.skipif(
            not _have_shell("pwsh"), reason="PowerShell 7 missing"
        )),
    ],
)
def test_retry_missing_ready_does_not_report_published(tmp_path: Path, shell: str):
    dump = tmp_path / "dump"
    inbox = tmp_path / "inbox"
    _write_tree(
        dump,
        {"Configuration.xml": b"<Configuration/>", "Catalogs/Foo.xml": b"<Catalog/>"},
    )
    inbox.mkdir()
    export_id = "export-interrupted"
    ready = inbox / f"ready-{export_id}"
    # State after rename but before READY: complete content, no READY marker.
    script_prep = f"""
Import-Module {_quote(MODULE)} -Force
$dump = {_quote(dump)}
$inbox = {_quote(inbox)}
$m = New-ExportManifest -Database 'klon_opl' -ExportId '{export_id}' `
  -SourcePath $dump -Operations @{{ DumpConfigToFiles = 'ok' }} `
  -PlatformVersion '8.3.25' -StartedAt '2026-10-15T00:00:00Z' `
  -FinishedAt '2026-10-15T00:01:00Z'
$ready = Publish-ExportPackage -SourcePath $dump -DestinationRoot $inbox -Manifest $m
Remove-Item -LiteralPath (Join-Path $ready 'READY') -Force
$m | ConvertTo-Json -Compress | Set-Content -LiteralPath {_quote(tmp_path / 'manifest-reuse.json')} -Encoding utf8
Write-Output $ready
"""
    prep = _ps_run(shell, script_prep)
    assert prep.returncode == 0, prep.stdout + prep.stderr
    assert ready.is_dir()
    assert not (ready / "READY").exists()

    script_retry = f"""
Import-Module {_quote(MODULE)} -Force
$dump = {_quote(dump)}
$inbox = {_quote(inbox)}
$raw = Get-Content -LiteralPath {_quote(tmp_path / 'manifest-reuse.json')} -Raw -Encoding utf8 | ConvertFrom-Json
$m = [ordered]@{{
  schema_version = 1
  database = [string]$raw.database
  export_id = [string]$raw.export_id
  started_at = [string]$raw.started_at
  finished_at = [string]$raw.finished_at
  platform_version = [string]$raw.platform_version
  operations = @{{ DumpConfigToFiles = 'ok' }}
  file_count = [int]$raw.file_count
  checksum = [string]$raw.checksum
}}
try {{
  $out = Publish-ExportPackage -SourcePath $dump -DestinationRoot $inbox -Manifest $m
  Write-Output ("PATH=" + $out)
  if (Test-Path -LiteralPath (Join-Path $out 'READY')) {{ Write-Output 'HAS_READY=1' }}
  else {{ Write-Output 'HAS_READY=0' }}
}} catch {{
  Write-Output ("ERR=" + $_.Exception.Message)
  exit 1
}}
"""
    retry = _ps_run(shell, script_retry)
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert "HAS_READY=1" in retry.stdout
    assert (ready / "READY").is_file()
    # Must not claim success without READY (regression was: success + no READY).
    assert "already published" not in retry.stdout.lower() or (ready / "READY").is_file()


@pytest.mark.parametrize(
    "shell",
    [
        pytest.param("powershell", marks=pytest.mark.skipif(
            not _have_shell("powershell"), reason="Windows PowerShell 5.1 missing"
        )),
        pytest.param("pwsh", marks=pytest.mark.skipif(
            not _have_shell("pwsh"), reason="PowerShell 7 missing"
        )),
    ],
)
def test_consumer_can_claim_immediately_after_ready(tmp_path: Path, shell: str):
    """READY hands off to the consumer; inbox may vanish before Publish returns.

    Deterministic claim: a background watcher moves the ready-* tree on the first
    sighting of READY (same moment the old code re-checked manifest/READY and
    threw). Publish must still succeed.
    """
    dump = tmp_path / "dump"
    inbox = tmp_path / "inbox"
    accepted = tmp_path / "accepted"
    _write_tree(
        dump,
        {"Configuration.xml": b"<Configuration/>", "Catalogs/Foo.xml": b"<Catalog/>"},
    )
    inbox.mkdir()
    accepted.mkdir()
    export_id = "export-claim"
    ready_name = f"ready-{export_id}"
    script = f"""
Import-Module {_quote(MODULE)} -Force
$dump = {_quote(dump)}
$inbox = {_quote(inbox)}
$accepted = {_quote(accepted)}
$readyPath = Join-Path $inbox '{ready_name}'
$dest = Join-Path $accepted '{ready_name}'

# Deterministic consumer: move on first READY observation (claim_package).
$job = Start-Job -ScriptBlock {{
  param($readyPath, $dest)
  for ($i = 0; $i -lt 400; $i++) {{
    $marker = Join-Path $readyPath 'READY'
    if (Test-Path -LiteralPath $marker) {{
      Move-Item -LiteralPath $readyPath -Destination $dest -Force -ErrorAction SilentlyContinue
      return 'moved'
    }}
    Start-Sleep -Milliseconds 25
  }}
  return 'timeout'
}} -ArgumentList $readyPath, $dest

$m = New-ExportManifest -Database 'klon_opl' -ExportId '{export_id}' `
  -SourcePath $dump -Operations @{{ DumpConfigToFiles = 'ok' }} `
  -PlatformVersion '8.3.25' -StartedAt '2026-10-15T00:00:00Z' `
  -FinishedAt '2026-10-15T00:01:00Z'
$err = $null
try {{
  $out = Publish-ExportPackage -SourcePath $dump -DestinationRoot $inbox -Manifest $m
  Write-Output ("PATH=" + $out)
  Write-Output 'PUBLISH_OK=1'
}} catch {{
  $err = $_.Exception.Message
  Write-Output ("ERR=" + $err)
  Write-Output 'PUBLISH_OK=0'
}}
$jobResult = Wait-Job $job -Timeout 30 | Receive-Job
Remove-Job $job -Force -ErrorAction SilentlyContinue
Write-Output ("JOB=" + $jobResult)
if ($err) {{ exit 1 }}
if (-not (Test-Path -LiteralPath $dest)) {{
  # Slow host: claim after return is still valid if publish succeeded.
  if (Test-Path -LiteralPath $readyPath) {{
    Move-Item -LiteralPath $readyPath -Destination $dest -Force
  }}
}}
if (Test-Path -LiteralPath (Join-Path $dest 'READY')) {{ Write-Output 'CLAIMED=1' }}
"""
    result = _ps_run(shell, script)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PUBLISH_OK=1" in result.stdout
    assert "manifest.json missing after publish" not in result.stdout
    assert "READY missing after publish" not in result.stdout
    claimed = accepted / ready_name
    assert claimed.is_dir()
    assert (claimed / "READY").is_file()
    assert (claimed / "manifest.json").is_file()
    validate_package(claimed, frozenset({"klon_opl"}))


def test_robocopy_invocations_cap_retries():
    text = MODULE.read_text(encoding="utf-8")
    # Strip comments for a coarse scan of calls.
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
    code = "\n".join(lines)
    assert code.count("robocopy.exe") >= 2
    assert code.count("/R:2") >= 2
    assert code.count("/W:2") >= 2
