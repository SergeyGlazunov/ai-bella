# Pester 5 tests for Export-Package.psm1 (Task 9).
# Run: Import-Module Pester -MinimumVersion 5.0; Invoke-Pester -Path tools/common/Export-Package.Tests.ps1

BeforeAll {
    $modulePath = Join-Path $PSScriptRoot 'Export-Package.psm1'
    Import-Module $modulePath -Force
}

Describe 'New-SourceChecksum' {
    It 'returns stable 64-hex from identity fields without reading src files' {
        $hash = New-SourceChecksum `
            -Database 'klon_opl' `
            -ExportId 'export-1' `
            -FileCount 42 `
            -FinishedAt '2026-10-15T00:01:00Z'
        $hash | Should -Match '^[0-9a-f]{64}$'
        $again = New-SourceChecksum `
            -Database 'klon_opl' `
            -ExportId 'export-1' `
            -FileCount 42 `
            -FinishedAt '2026-10-15T00:01:00Z'
        $again | Should -Be $hash
    }

    It 'changes when export_id or file_count changes' {
        $a = New-SourceChecksum -Database 'kb' -ExportId 'e1' -FileCount 1 -FinishedAt '2026-10-15T00:01:00Z'
        $b = New-SourceChecksum -Database 'kb' -ExportId 'e2' -FileCount 1 -FinishedAt '2026-10-15T00:01:00Z'
        $c = New-SourceChecksum -Database 'kb' -ExportId 'e1' -FileCount 2 -FinishedAt '2026-10-15T00:01:00Z'
        $a | Should -Not -Be $b
        $a | Should -Not -Be $c
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
        @(Get-ChildItem -LiteralPath $script:inbox -Force -Directory | Where-Object { $_.Name -like '.incoming-*' }).Count | Should -Be 0
        $manifest.checksum | Should -Match '^[0-9a-f]{64}$'
    }

    It 'failed copy leaves only .incoming-* and returns non-zero' {
        $badDump = Join-Path $TestDrive 'bad-dump-empty'
        New-Item -ItemType Directory -Force -Path $badDump | Out-Null
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

        $first = Publish-ExportPackage -SourcePath $script:dump -DestinationRoot $script:inbox -Manifest $manifest
        $second = Publish-ExportPackage -SourcePath $script:dump -DestinationRoot $script:inbox -Manifest $manifest
        $second | Should -Be $first
    }
}
