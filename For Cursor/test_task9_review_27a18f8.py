"""Recovery branch must follow the same READY handoff contract as normal publish."""
import sys
import shutil
import json
import os
import subprocess
from pathlib import Path
import pytest
import contour.packages as packages

MODULE = Path(packages.__file__).resolve().parents[3] / 'tools/common/Export-Package.psm1'


@pytest.mark.parametrize('shell', ['powershell', 'pwsh'])
@pytest.mark.parametrize('case', ['claim-on-recovery', 'missing-source-file'])
def test_interrupted_publish_recovery(tmp_path, shell, case):
    if not shutil.which(shell):
        pytest.skip('shell missing')
    dump = tmp_path / 'dump'
    (dump / 'Catalogs').mkdir(parents=True)
    (dump / 'Configuration.xml').write_bytes(b'<Configuration/>')
    (dump / 'Catalogs/Foo.xml').write_bytes(b'<Catalog/>')
    ready = tmp_path / 'inbox/ready-one'
    shutil.copytree(dump, ready / 'src')
    # Correct manifest, but READY was not committed by the interrupted process.
    manifest = dict(schema_version=1, database='klon_opl', export_id='one',
        started_at='2026-10-15T00:00:00Z', finished_at='2026-10-15T00:01:00Z',
        platform_version='8.3.25', operations={'DumpConfigToFiles':'ok'},
        file_count=2, checksum=packages.compute_source_checksum(ready))
    (ready / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    if case == 'missing-source-file':
        (ready / 'src/Catalogs/Foo.xml').unlink()
    hook = r'''
& (Get-Module Export-Package) {
    function script:Test-Path {
        param([string]$LiteralPath, [string]$PathType)
        if ($LiteralPath -like '*ready-one*READY' -and [IO.File]::Exists($LiteralPath)) {
            [IO.Directory]::Move((Split-Path -Parent $LiteralPath), (Join-Path $env:REVIEW_ROOT 'accepted-one'))
        }
        if ($PathType) { Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath -PathType $PathType }
        else { Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath }
    }
}
''' if case == 'claim-on-recovery' else ''
    script = r'''
$ErrorActionPreference='Stop'
Import-Module $env:REVIEW_MODULE -Force
$dump = Join-Path $env:REVIEW_ROOT 'dump'
$m = New-ExportManifest -Database klon_opl -ExportId one -SourcePath $dump -Operations @{DumpConfigToFiles='ok'} -PlatformVersion '8.3.25'
''' + hook + r'''
Publish-ExportPackage -SourcePath $dump -DestinationRoot (Join-Path $env:REVIEW_ROOT 'inbox') -Manifest $m
'''
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', script],
        env=dict(os.environ, REVIEW_MODULE=str(MODULE), REVIEW_ROOT=str(tmp_path)),
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (result.stdout, result.stderr)
    accepted = tmp_path / 'accepted-one'
    packages.validate_package(accepted if accepted.exists() else ready, frozenset({'klon_opl'}))
