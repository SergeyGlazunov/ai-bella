# Shared Windows → Linux package export helpers (Task 9).
# Checksum contract must match controller/src/contour/packages.py compute_source_checksum.
# Package build and publish only (no container engine, no WSL launcher, no DB engine).

Set-StrictMode -Version Latest

function Get-RelativePosixPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$FullPath
    )
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $fileFull = [IO.Path]::GetFullPath($FullPath)
    if (-not $fileFull.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "path escapes root: $FullPath"
    }
    $rel = $fileFull.Substring($rootFull.Length).TrimStart('\', '/')
    return ($rel -replace '\\', '/')
}

function Get-FileSha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $fs = [IO.File]::OpenRead($Path)
        try {
            $hash = $sha.ComputeHash($fs)
        }
        finally {
            $fs.Dispose()
        }
        return ([BitConverter]::ToString($hash) -replace '-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

<#
.SYNOPSIS
  SHA-256 over sorted relative path, size and file SHA-256 (POSIX paths).
  Excludes READY and manifest.json. Matches Python compute_source_checksum.
#>
function New-SourceChecksum {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourcePath
    )
    $root = [IO.Path]::GetFullPath($SourcePath)
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw "SourcePath is not a directory: $root"
    }

    $excluded = @{
        'READY'         = $true
        'manifest.json' = $true
    }

    # Collect then sort by relative POSIX path with Ordinal comparer so the
    # order matches Python sorted(..., key=as_posix) regardless of PS culture.
    $byRel = New-Object 'System.Collections.Generic.Dictionary[string,System.IO.FileInfo]'
    Get-ChildItem -LiteralPath $root -Recurse -File -Force | ForEach-Object {
        $rel = Get-RelativePosixPath -Root $root -FullPath $_.FullName
        if ($excluded.ContainsKey($rel)) { return }
        $byRel[$rel] = $_
    }
    $relKeys = New-Object string[] $byRel.Count
    $byRel.Keys.CopyTo($relKeys, 0)
    [Array]::Sort($relKeys, [StringComparer]::Ordinal)

    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($rel in $relKeys) {
        $f = $byRel[$rel]
        $len = $f.Length
        $digest = Get-FileSha256Hex -Path $f.FullName
        $lines.Add("${rel}`0${len}`0${digest}")
    }

    $payload = [string]::Join("`n", $lines)
    $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash($bytes)
        return ([BitConverter]::ToString($hash) -replace '-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-SourceFileCount {
    param([Parameter(Mandatory = $true)][string]$PackageRoot)
    $src = Join-Path $PackageRoot 'src'
    if (-not (Test-Path -LiteralPath $src -PathType Container)) {
        throw "missing src/: $src"
    }
    return @(Get-ChildItem -LiteralPath $src -Recurse -File -Force).Count
}

<#
.SYNOPSIS
  Build manifest hashtable (schema_version=1) with checksum and file_count from SourcePath package root.
#>
function New-ExportManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Database,
        [Parameter(Mandatory = $true)][string]$ExportId,
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][hashtable]$Operations,
        [Parameter(Mandatory = $true)][string]$PlatformVersion,
        [string]$StartedAt = "",
        [string]$FinishedAt = ""
    )

    if ($Database -match '[\./\\]' -or $Database -match '\.\.') {
        throw "invalid database name: $Database"
    }
    if ($ExportId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') {
        throw "invalid export_id: $ExportId"
    }

    $root = [IO.Path]::GetFullPath($SourcePath)
    $pkgRoot = $null
    $tempPkg = $null
    try {
        if (Test-Path -LiteralPath (Join-Path $root 'src\Configuration.xml') -PathType Leaf) {
            $pkgRoot = $root
        }
        elseif (Test-Path -LiteralPath (Join-Path $root 'Configuration.xml') -PathType Leaf) {
            # Dump layout: stage a temporary package root so checksum paths are src/...
            $tempPkg = Join-Path ([IO.Path]::GetTempPath()) ("export-pkg-" + [guid]::NewGuid().ToString('N'))
            $srcDir = Join-Path $tempPkg 'src'
            New-Item -ItemType Directory -Force -Path $srcDir | Out-Null
            $roboCode = 0
            # Cap retries: default robocopy is 1e6 retries × 30s on a single error.
            & robocopy.exe $root $srcDir /E /NFL /NDL /NJH /NJS /nc /ns /np /R:2 /W:2 | Out-Null
            $roboCode = [int]$LASTEXITCODE
            if ($roboCode -ge 8) { throw "robocopy failed exit=$roboCode while staging dump for manifest" }
            $pkgRoot = $tempPkg
        }
        else {
            throw "SourcePath must contain Configuration.xml or src/Configuration.xml: $root"
        }

        $ops = @{}
        foreach ($k in $Operations.Keys) {
            $ops[[string]$k] = [string]$Operations[$k]
        }

        if (-not $StartedAt) {
            $StartedAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        }
        if (-not $FinishedAt) {
            $FinishedAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        }

        $fileCount = Get-SourceFileCount -PackageRoot $pkgRoot
        $checksum = New-SourceChecksum -SourcePath $pkgRoot

        return [ordered]@{
            schema_version   = 1
            database         = $Database
            export_id        = $ExportId
            started_at       = $StartedAt
            finished_at      = $FinishedAt
            platform_version = $PlatformVersion
            operations       = $ops
            file_count       = [int]$fileCount
            checksum         = $checksum
        }
    }
    finally {
        if ($tempPkg -and (Test-Path -LiteralPath $tempPkg)) {
            Remove-Item -LiteralPath $tempPkg -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Write-ExportManifestFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Manifest
    )
    $json = $Manifest | ConvertTo-Json -Depth 8 -Compress:$false
    # ConvertTo-Json may reorder; keep UTF-8 no BOM
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [IO.File]::WriteAllText($Path, $json + "`n", $utf8)
}

function Test-PackageIdentityMatch {
    param(
        [Parameter(Mandatory = $true)][string]$ReadyPath,
        [Parameter(Mandatory = $true)]$Manifest
    )
    $manifestPath = Join-Path $ReadyPath 'manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath)) { return $false }
    $existing = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    return (
        [string]$existing.database -eq [string]$Manifest.database -and
        [string]$existing.export_id -eq [string]$Manifest.export_id -and
        [string]$existing.checksum -eq [string]$Manifest.checksum
    )
}

function Test-PackageContentComplete {
    param([Parameter(Mandatory = $true)][string]$PackagePath)
    $manifestPath = Join-Path $PackagePath 'manifest.json'
    $cfg = Join-Path $PackagePath 'src\Configuration.xml'
    return (
        (Test-Path -LiteralPath $manifestPath -PathType Leaf) -and
        (Test-Path -LiteralPath $cfg -PathType Leaf)
    )
}

function Get-ManifestChecksumValue {
    param($Manifest)
    if ($Manifest -is [hashtable] -or $Manifest -is [System.Collections.Specialized.OrderedDictionary]) {
        return [string]$Manifest['checksum']
    }
    return [string]$Manifest.checksum
}

function Get-ManifestFileCountValue {
    param($Manifest)
    if ($Manifest -is [hashtable] -or $Manifest -is [System.Collections.Specialized.OrderedDictionary]) {
        return [int]$Manifest['file_count']
    }
    return [int]$Manifest.file_count
}

function Test-PackageTreeMatchesManifest {
    <#
    .SYNOPSIS
      True when on-disk src tree matches manifest file_count and checksum.
      Structural presence alone is not enough (interrupted copy may drop files).
    #>
    param(
        [Parameter(Mandatory = $true)][string]$PackagePath,
        [Parameter(Mandatory = $true)]$Manifest
    )
    if (-not (Test-PackageContentComplete -PackagePath $PackagePath)) {
        return $false
    }
    if (-not (Test-PackageIdentityMatch -ReadyPath $PackagePath -Manifest $Manifest)) {
        return $false
    }
    $actualChecksum = New-SourceChecksum -SourcePath $PackagePath
    $actualCount = Get-SourceFileCount -PackageRoot $PackagePath
    $expectedChecksum = Get-ManifestChecksumValue -Manifest $Manifest
    $expectedCount = Get-ManifestFileCountValue -Manifest $Manifest
    return (
        $actualChecksum -eq $expectedChecksum -and
        $actualCount -eq $expectedCount
    )
}

<#
.SYNOPSIS
  Copy dump tree into inbox as .incoming-<id>, write manifest, publish ready-<id>, create READY last.
#>
function Publish-ExportPackage {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourcePath,
        [Parameter(Mandatory = $true)]
        [string]$DestinationRoot,
        [Parameter(Mandatory = $true)]
        $Manifest
    )

    $dump = [IO.Path]::GetFullPath($SourcePath)
    $destRoot = [IO.Path]::GetFullPath($DestinationRoot)
    $exportId = [string]$Manifest.export_id
    if (-not $exportId) { throw "Manifest.export_id is empty" }

    $cfgXml = Join-Path $dump 'Configuration.xml'
    if (-not (Test-Path -LiteralPath $cfgXml -PathType Leaf)) {
        throw "Dump missing Configuration.xml"
    }

    New-Item -ItemType Directory -Force -Path $destRoot | Out-Null

    $readyName = "ready-$exportId"
    $incomingName = ".incoming-$exportId"
    $readyPath = Join-Path $destRoot $readyName
    $incomingPath = Join-Path $destRoot $incomingName

    $readyMarker = Join-Path $readyPath 'READY'
    if (Test-Path -LiteralPath $readyPath) {
        $hasReady = Test-Path -LiteralPath $readyMarker -PathType Leaf
        if ($hasReady) {
            if (Test-PackageIdentityMatch -ReadyPath $readyPath -Manifest $Manifest) {
                Write-Host "package already published (idempotent): $readyName"
                return $readyPath
            }
            throw "claim collision for export_id=$exportId"
        }
        # Renamed but READY missing (interrupted publish). Finish only when the
        # on-disk tree matches this delivery's checksum/file_count; otherwise
        # remove and rebuild from the dump. After writing READY the consumer may
        # claim immediately — do not re-check inbox paths.
        if (Test-PackageTreeMatchesManifest -PackagePath $readyPath -Manifest $Manifest) {
            [IO.File]::WriteAllBytes($readyMarker, [byte[]]@())
            Write-Host "completed interrupted publish (READY written): $readyName"
            return $readyPath
        }
        Remove-Item -LiteralPath $readyPath -Recurse -Force -ErrorAction Stop
        Write-Host "removed incomplete package without READY: $readyName"
    }

    if (Test-Path -LiteralPath $incomingPath) {
        Remove-Item -LiteralPath $incomingPath -Recurse -Force -ErrorAction Stop
    }

    $renamed = $false
    try {
        $srcDir = Join-Path $incomingPath 'src'
        New-Item -ItemType Directory -Force -Path $srcDir | Out-Null

        # Copy dump → incoming/src (robocopy exit 0-7 = success). Cap retries.
        $null = & robocopy.exe $dump $srcDir /E /NFL /NDL /NJH /NJS /nc /ns /np /R:2 /W:2
        $roboCode = [int]$LASTEXITCODE
        if ($roboCode -ge 8) {
            throw "robocopy failed exit=$roboCode"
        }
        if (-not (Test-Path -LiteralPath (Join-Path $srcDir 'Configuration.xml'))) {
            throw "copy incomplete: Configuration.xml missing in incoming/src"
        }

        # Refresh checksum/file_count against the package root being published
        $checksum = New-SourceChecksum -SourcePath $incomingPath
        $fileCount = Get-SourceFileCount -PackageRoot $incomingPath
        if ($Manifest -is [hashtable] -or $Manifest -is [System.Collections.Specialized.OrderedDictionary]) {
            $Manifest['checksum'] = $checksum
            $Manifest['file_count'] = [int]$fileCount
        }
        else {
            $Manifest.checksum = $checksum
            $Manifest.file_count = [int]$fileCount
        }

        $manifestPath = Join-Path $incomingPath 'manifest.json'
        Write-ExportManifestFile -Path $manifestPath -Manifest $Manifest
        if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
            throw "manifest.json missing before publish"
        }

        # Atomic-ish publish: rename incoming → ready, verify content, READY last.
        # After READY the consumer may claim_package (move) immediately — do not
        # re-check inbox paths after writing the marker.
        Move-Item -LiteralPath $incomingPath -Destination $readyPath -Force
        $renamed = $true

        if (-not (Test-PackageContentComplete -PackagePath $readyPath)) {
            throw "package incomplete after rename (manifest/src)"
        }

        [IO.File]::WriteAllBytes($readyMarker, [byte[]]@())
        # Creating READY hands the package to the consumer; treat as success.

        Write-Host "published package: $readyName files=$fileCount"
        return $readyPath
    }
    catch {
        if (-not $renamed -and (Test-Path -LiteralPath $incomingPath)) {
            # leave .incoming-* for operator inspection; do not create READY
            Write-Host "publish failed; left incomplete: $incomingName"
        }
        elseif ($renamed -and (Test-Path -LiteralPath $readyPath) -and -not (Test-Path -LiteralPath $readyMarker)) {
            # failed after rename but before READY — rename back to incoming
            $rollback = Join-Path $destRoot $incomingName
            if (-not (Test-Path -LiteralPath $rollback)) {
                Move-Item -LiteralPath $readyPath -Destination $rollback -Force -ErrorAction SilentlyContinue
            }
            Write-Host "publish failed after rename; rolled back to $incomingName"
        }
        throw
    }
}

Export-ModuleMember -Function @(
    'New-SourceChecksum',
    'New-ExportManifest',
    'Publish-ExportPackage'
)
