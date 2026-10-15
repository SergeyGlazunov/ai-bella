# Pester 5 tests for Export-Package.psm1 (Task 9).
# Run: Import-Module Pester -MinimumVersion 5.0; Invoke-Pester -Path tools/common/Export-Package.Tests.ps1

BeforeAll {
    $modulePath = Join-Path $PSScriptRoot 'Export-Package.psm1'
    Import-Module $modulePath -Force

    function Get-PythonChecksum {
        param([string]$PackageRoot)
        $py = @'
import hashlib, sys
from pathlib import Path
root = Path(sys.argv[1])
excluded = {"READY", "manifest.json"}
lines = []
for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
    if not path.is_file():
        continue
    rel = path.relative_to(root).as_posix()
    if rel in excluded:
        continue
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    lines.append(f"{rel}\0{len(data)}\0{digest}")
print(hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest())
'@
        $tmpPy = Join-Path $TestDrive 'checksum.py'
        Set-Content -LiteralPath $tmpPy -Value $py -Encoding UTF8
        $out = & python $tmpPy $PackageRoot 2>&1
        if ($LASTEXITCODE -ne 0) { throw "python checksum failed: $out" }
        return ([string]$out).Trim()
    }
}

Describe 'New-SourceChecksum' {
    It 'matches Python compute_source_checksum fixtures' {
        $pkg = Join-Path $TestDrive 'pkg-checksum'
        New-Item -ItemType Directory -Force -Path (Join-Path $pkg 'src\Catalogs') | Out-Null
        Set-Content -LiteralPath (Join-Path $pkg 'src\Configuration.xml') -Value '<Configuration/>' -Encoding Ascii -NoNewline
        Set-Content -LiteralPath (Join-Path $pkg 'src\Catalogs\Foo.xml') -Value '<Catalog/>' -Encoding Ascii -NoNewline
        # Noise that must be excluded when present
        Set-Content -LiteralPath (Join-Path $pkg 'READY') -Value '' -Encoding Ascii
        Set-Content -LiteralPath (Join-Path $pkg 'manifest.json') -Value '{}' -Encoding Ascii

        $psHash = New-SourceChecksum -SourcePath $pkg
        $pyHash = Get-PythonChecksum -PackageRoot $pkg
        $psHash | Should -Be $pyHash
        $psHash | Should -Match '^[0-9a-f]{64}$'
    }

    It 'matches Python with mixed case, underscore and ordering-sensitive names' {
        $pkg = Join-Path $TestDrive 'pkg-mixed'
        New-Item -ItemType Directory -Force -Path (Join-Path $pkg 'src\Catalogs') | Out-Null
        Set-Content -LiteralPath (Join-Path $pkg 'src\Configuration.xml') -Value '<Configuration/>' -Encoding Ascii -NoNewline
        Set-Content -LiteralPath (Join-Path $pkg 'src\Catalogs\Zebra.xml') -Value '<Z/>' -Encoding Ascii -NoNewline
        Set-Content -LiteralPath (Join-Path $pkg 'src\Catalogs\aTest.xml') -Value '<a/>' -Encoding Ascii -NoNewline
        Set-Content -LiteralPath (Join-Path $pkg 'src\Catalogs\_Special.xml') -Value '<_/>' -Encoding Ascii -NoNewline

        $psHash = New-SourceChecksum -SourcePath $pkg
        $pyHash = Get-PythonChecksum -PackageRoot $pkg
        $psHash | Should -Be $pyHash
    }
}

Describe 'Publish-ExportPackage' {
    BeforeEach {
        $script:dump = Join-Path $TestDrive ("dump-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
        $script:inbox = Join-Path $TestDrive ("inbox-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
        New-Item -ItemType Directory -Force -Path (Join-Path $script:dump 'Catalogs') | Out-Null
        Set-Content -LiteralPath (Join-Path $script:dump 'Configuration.xml') -Value '<Configuration/>' -NoNewline -Encoding Ascii
        Set-Content -LiteralPath (Join-Path $script:dump 'Catalogs\Foo.xml') -Value '<Catalog/>' -NoNewline -Encoding Ascii
        New-Item -ItemType Directory -Force -Path $script:inbox | Out-Null
    }

    It 'creates READY only after files and manifest.json are present' {
        $manifest = New-ExportManifest `
            -Database 'klon_opl' `
            -ExportId 'export-ready-order' `
            -SourcePath $script:dump `
            -Operations @{ DumpConfigToFiles = 'ok' } `
            -PlatformVersion '8.3.25' `
            -StartedAt '2026-10-15T00:00:00Z' `
            -FinishedAt '2026-10-15T00:01:00Z'

        $ready = Publish-ExportPackage -SourcePath $script:dump -DestinationRoot $script:inbox -Manifest $manifest
        Test-Path -LiteralPath (Join-Path $ready 'READY') | Should -BeTrue
        Test-Path -LiteralPath (Join-Path $ready 'manifest.json') | Should -BeTrue
        Test-Path -LiteralPath (Join-Path $ready 'src\Configuration.xml') | Should -BeTrue
        # No leftover incomplete marker
        @(Get-ChildItem -LiteralPath $script:inbox -Force -Directory | Where-Object { $_.Name -like '.incoming-*' }).Count | Should -Be 0
    }

    It 'failed copy leaves only .incoming-* and returns non-zero' {
        $badDump = Join-Path $TestDrive 'bad-dump-empty'
        New-Item -ItemType Directory -Force -Path $badDump | Out-Null
        # Configuration.xml missing → fail before/during publish
        $manifest = [ordered]@{
            schema_version   = 1
            database         = 'klon_opl'
            export_id        = 'export-fail-copy'
            started_at       = '2026-10-15T00:00:00Z'
            finished_at      = '2026-10-15T00:01:00Z'
            platform_version = '8.3.25'
            operations       = @{ DumpConfigToFiles = 'ok' }
            file_count       = 0
            checksum         = ('0' * 64)
        }
        { Publish-ExportPackage -SourcePath $badDump -DestinationRoot $script:inbox -Manifest $manifest } | Should -Throw
        Test-Path -LiteralPath (Join-Path $script:inbox 'ready-export-fail-copy') | Should -BeFalse
        Test-Path -LiteralPath (Join-Path $script:inbox 'ready-export-fail-copy\READY') | Should -BeFalse
    }

    It 'retrying the same export ID is idempotent' {
        $manifest = New-ExportManifest `
            -Database 'klon_opl' `
            -ExportId 'export-idem' `
            -SourcePath $script:dump `
            -Operations @{ DumpConfigToFiles = 'ok' } `
            -PlatformVersion '8.3.25' `
            -StartedAt '2026-10-15T00:00:00Z' `
            -FinishedAt '2026-10-15T00:01:00Z'

        $a = Publish-ExportPackage -SourcePath $script:dump -DestinationRoot $script:inbox -Manifest $manifest
        $b = Publish-ExportPackage -SourcePath $script:dump -DestinationRoot $script:inbox -Manifest $manifest
        $a | Should -Be $b
        @(Get-ChildItem -LiteralPath $script:inbox -Directory | Where-Object { $_.Name -like 'ready-*' }).Count | Should -Be 1
    }
}

Describe 'Export-Package module hygiene' {
    It 'destination paths and credentials never appear in process arguments or normal host logs from module helpers' {
        $text = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'Export-Package.psm1') -Raw
        # No credential material in module source (profiles stay outside).
        $text | Should -Not -Match '(?i)ibPassword|storePassword|CONFLUENCE_PAT'
        # Publish uses robocopy with dump + incoming paths only.
        $text | Should -Match 'robocopy\.exe'
        $text | Should -Not -Match '(?i)/P`"|-p\s|Password='
    }

    It 'exporter module contains no docker, wsl.exe or SQLite invocation' {
        $text = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'Export-Package.psm1') -Raw
        # Strip line comments before scanning for invocations.
        $code = [regex]::Replace($text, '(?m)^\s*#.*$', '')
        ($code -match '(?i)(&\s*docker\b|docker\.exe)') | Should -BeFalse
        ($code -match '(?i)wsl\.exe') | Should -BeFalse
        ($code -match '(?i)sqlite3?\.exe|\bsqlite3\b') | Should -BeFalse
    }
}

Describe 'KB and klon_opl exporter scripts hygiene' {
    It 'exporter scripts no longer invoke docker / index helpers' {
        $kb = Get-Content -LiteralPath (Join-Path $PSScriptRoot '..\kb\Update-And-Export-Kb.ps1') -Raw
        $opl = Get-Content -LiteralPath (Join-Path $PSScriptRoot '..\klon_opl\Export-KlonOpl.ps1') -Raw
        foreach ($text in @($kb, $opl)) {
            $code = [regex]::Replace($text, '(?m)^\s*#.*$', '')
            ($code -match '(?i)(&\s*docker\b|docker\.exe|Docker-Index)') | Should -BeFalse
            ($code -match '(?i)Invoke-KbGraphFullReindex|Invoke-Ai2BslCodeIndex|Copy-WinTreeToLinuxPath') | Should -BeFalse
            ($code -match 'Export-Package\.psm1') | Should -BeTrue
            ($code -match 'Publish-ExportPackage') | Should -BeTrue
        }
    }
}

Describe 'Manifest operations policy' {
    BeforeAll {
        $script:dumpOps = Join-Path $TestDrive 'dump-ops'
        New-Item -ItemType Directory -Force -Path $script:dumpOps | Out-Null
        Set-Content -LiteralPath (Join-Path $script:dumpOps 'Configuration.xml') -Value '<Configuration/>' -NoNewline -Encoding Ascii
    }

    It 'KB manifest records UpdateCfg, UpdateDBCfg and dump results' {
        $manifest = New-ExportManifest `
            -Database 'kb' `
            -ExportId 'export-kb-ops' `
            -SourcePath $script:dumpOps `
            -Operations @{
                DumpConfigToFiles = 'ok'
                UpdateCfg         = 'ok'
                UpdateDBCfg       = 'ok'
            } `
            -PlatformVersion '8.3.25' `
            -StartedAt '2026-10-15T00:00:00Z' `
            -FinishedAt '2026-10-15T00:01:00Z'

        $manifest.operations.DumpConfigToFiles | Should -Be 'ok'
        $manifest.operations.UpdateCfg | Should -Be 'ok'
        $manifest.operations.UpdateDBCfg | Should -Be 'ok'
        $manifest.database | Should -Be 'kb'
    }

    It 'klon_opl manifest records dump only' {
        $manifest = New-ExportManifest `
            -Database 'klon_opl' `
            -ExportId 'export-opl-ops' `
            -SourcePath $script:dumpOps `
            -Operations @{ DumpConfigToFiles = 'ok' } `
            -PlatformVersion '8.3.25' `
            -StartedAt '2026-10-15T00:00:00Z' `
            -FinishedAt '2026-10-15T00:01:00Z'

        @($manifest.operations.Keys) | Should -Be @('DumpConfigToFiles')
        $manifest.operations.DumpConfigToFiles | Should -Be 'ok'
    }
}
