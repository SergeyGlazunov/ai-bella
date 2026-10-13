<#
.SYNOPSIS
  КБ: store+dump → /srv/ai2/kb/src → graph full → code-index (ai2-bsl-kb)

.DESCRIPTION
  Запуск: Windows PowerShell. Других операций с хранилищем/конфигуратором скрипт не делает.
  Запрещены произвольные ключи DESIGNER; нет режимов «только dump» / «пропустить store».

  После копирования выгрузки — полный прогон metadata-indexer (METADATA_FORCE_REINDEX),
  без apply_changes: КБ маленькая (~10 мин), затем code-index через ai2-bsl-kb.
  Нужен Docker Engine (без docker compose plugin).

  Права учётки хранилища на стороне 1С тоже должны быть минимальными (только получение
  изменений). Скрипт — второй контур ограничения.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\kb\Update-And-Export-Kb.ps1
#>
[CmdletBinding()]
param(
    [string]$Profile = ""
)

$ErrorActionPreference = "Stop"

function Read-KbProfile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Profile not found: $Path"
    }
    $cfg = [IO.File]::ReadAllText($Path, [Text.UTF8Encoding]::new($false)) | ConvertFrom-Json
    foreach ($req in @(
            "exePath", "server", "ref", "ibUser", "ibPassword",
            "storeUrl", "storeUser", "storePassword"
        )) {
        if (-not $cfg.$req) { throw "Profile field empty: $req" }
    }
    return $cfg
}

function Get-LogText {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    $bytes = [IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        return [Text.Encoding]::UTF8.GetString($bytes)
    }
    try {
        return [Text.Encoding]::GetEncoding(1251).GetString($bytes)
    }
    catch {
        return [Text.Encoding]::Default.GetString($bytes)
    }
}

function Invoke-DesignerAllowed {
    param(
        [ValidateSet("store-update", "dump-xml")]
        [string]$Op,
        $Cfg,
        [string]$LogDir,
        [string]$Stamp,
        [string]$ExportPath = ""
    )

    $log = Join-Path $LogDir "$Op-$Stamp.log"
    $connect = "/S`"$($Cfg.server)\$($Cfg.ref)`""

    # Белый список argv: ничего кроме этих двух операций (+ UpdateDBCfg внутри store-update).
    switch ($Op) {
        "store-update" {
            $extra = @(
                "/ConfigurationRepositoryF`"$($Cfg.storeUrl)`"",
                "/ConfigurationRepositoryN`"$($Cfg.storeUser)`"",
                "/ConfigurationRepositoryP`"$($Cfg.storePassword)`"",
                "/ConfigurationRepositoryUpdateCfg",
                "-force",
                "/UpdateDBCfg"
            )
        }
        "dump-xml" {
            if (-not $ExportPath) { throw "dump-xml requires ExportPath" }
            $extra = @(
                "/DumpConfigToFiles",
                "`"$ExportPath`""
            )
        }
        default { throw "Operation not allowed: $Op" }
    }

    $argList = @(
        "DESIGNER",
        $connect,
        "/N`"$($Cfg.ibUser)`"",
        "/P`"$($Cfg.ibPassword)`"",
        "/DisableStartupDialogs",
        "/Out`"$log`""
    ) + $extra

    Write-Host "=== $Op start $(Get-Date -Format o) ==="
    Write-Host "ib: $($Cfg.server)\$($Cfg.ref)"
    Write-Host "log: $log"

    if (-not (Test-Path -LiteralPath $Cfg.exePath)) {
        throw "1cv8 not found: $($Cfg.exePath)"
    }

    $sw = [Diagnostics.Stopwatch]::StartNew()
    $p = Start-Process -FilePath $Cfg.exePath -ArgumentList $argList -Wait -PassThru -NoNewWindow
    $sw.Stop()
    Write-Host "$Op exit=$($p.ExitCode) elapsed_sec=$([math]::Round($sw.Elapsed.TotalSeconds, 1))"

    $txt = Get-LogText -Path $log
    if ($txt.Trim().Length -gt 0) {
        Write-Host "---- log ----"
        Write-Host $txt.TrimEnd()
        Write-Host "-------------"
    }

    if ($p.ExitCode -ne 0) {
        throw "$Op failed exit=$($p.ExitCode). See $log"
    }
}

$common = Join-Path $PSScriptRoot "..\common\Docker-Index.ps1"
. (Resolve-Path $common).Path

function Wait-Neo4jHealthy {
    param(
        [string]$Container = "ai2-neo4j-kb",
        [int]$TimeoutSec = 180
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $null = docker inspect $Container 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "Neo4j container not found: $Container"
        }
        docker start $Container 2>$null | Out-Null
        $sw = [Diagnostics.Stopwatch]::StartNew()
        while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
            $health = (docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" $Container 2>$null)
            if ($health -eq "healthy" -or $health -eq "running") {
                if ($health -eq "healthy") { return }
                Start-Sleep -Seconds 3
                return
            }
            Start-Sleep -Seconds 3
        }
        throw "Neo4j not healthy within ${TimeoutSec}s: $Container"
    }
    finally {
        $ErrorActionPreference = $prev
    }
}

function Invoke-KbGraphFullReindex {
    param(
        [string]$ReferenceContainer = "ai2-metadata-indexer-kb",
        [string]$Neo4jContainer = "ai2-neo4j-kb",
        [string]$SrcLinux,
        [string]$LogDir,
        [string]$Stamp
    )

    $log = Join-Path $LogDir "graph-reindex-$Stamp.log"
    # Create log early so Task Scheduler failures leave a trail (inspect used to abort under Stop).
    "graph-reindex prepare $(Get-Date -Format o) ref=$ReferenceContainer neo4j=$Neo4jContainer" |
        Set-Content -LiteralPath $log -Encoding UTF8

    $inspectCode = Invoke-DockerNative -DockerArgs @("inspect", $ReferenceContainer)
    if ($inspectCode -ne 0) {
        throw "Reference indexer container missing: $ReferenceContainer (deploy ai2-mcp-kb first). See $log"
    }

    Wait-Neo4jHealthy -Container $Neo4jContainer

    # Capture JSON under Continue — native docker stderr must not abort the nightly job.
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $inspectJson = & docker inspect $ReferenceContainer 2>$null
        $inspectExit = [int]$LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $prevEap
    }
    if ($inspectExit -ne 0 -or -not $inspectJson) {
        throw "docker inspect failed for $ReferenceContainer exit=$inspectExit. See $log"
    }
    $ref = ($inspectJson | ConvertFrom-Json)[0]
    if (-not $ref) { throw "Failed to parse docker inspect for $ReferenceContainer. See $log" }

    $image = [string]$ref.Config.Image
    if (-not $image) { throw "No image on $ReferenceContainer" }

    $network = $null
    if ($ref.NetworkSettings.Networks) {
        $network = @($ref.NetworkSettings.Networks.PSObject.Properties.Name)[0]
    }
    if (-not $network) { $network = "ai2-mcp-kb_net" }

    $runName = "ai2-metadata-indexer-kb-nightly-$Stamp"
    $null = Invoke-DockerNative -DockerArgs @("rm", "-f", $runName)

    $dockerArgs = @(
        "run", "--rm",
        "--name", $runName,
        "--network", $network,
        "-v", "${SrcLinux}:/data/1c-xml:ro",
        "-w", "/app",
        "-e", "METADATA_FORCE_REINDEX=true",
        "-e", "PYTHONUNBUFFERED=1"
    )

    $skipEnvPrefix = @(
        "PATH=", "LANG=", "GPG_KEY=", "PYTHON_VERSION=", "PYTHON_SHA256=",
        "METADATA_FORCE_REINDEX=", "PYTHONUNBUFFERED="
    )
    foreach ($e in @($ref.Config.Env)) {
        $skip = $false
        foreach ($p in $skipEnvPrefix) {
            if ($e.StartsWith($p)) { $skip = $true; break }
        }
        if ($skip) { continue }
        $dockerArgs += "-e", $e
    }

    $dockerArgs += $image, "python", "/app/mcp_metadata_indexer.py"

    Write-Host "=== graph-reindex (full) start $(Get-Date -Format o) ==="
    Write-Host "image: $image"
    Write-Host "network: $network"
    Write-Host "src: $SrcLinux -> /data/1c-xml"
    Write-Host "force: METADATA_FORCE_REINDEX=true"
    Write-Host "log: $log"

    Add-Content -LiteralPath $log -Value "image=$image network=$network src=$SrcLinux" -Encoding UTF8

    $sw = [Diagnostics.Stopwatch]::StartNew()
    $exit = Invoke-DockerNative -DockerArgs $dockerArgs -TeeFile $log -AppendTee
    $sw.Stop()
    Write-Host "graph-reindex exit=$exit elapsed_sec=$([math]::Round($sw.Elapsed.TotalSeconds, 1))"

    if ($exit -ne 0) {
        throw "graph-reindex failed exit=$exit. See $log"
    }
}

# --- main ---
$scriptDir = $PSScriptRoot
if (-not $env:DOCKER_HOST) {
    $env:DOCKER_HOST = "tcp://127.0.0.1:2375"
}
if (-not $Profile) {
    $Profile = Join-Path $scriptDir "kb-connection.json"
}
$Profile = [IO.Path]::GetFullPath($Profile)
$cfg = Read-KbProfile -Path $Profile

$logDir = if ($cfg.logDir) { [string]$cfg.logDir } else { Join-Path $scriptDir "..\..\logs\kb" }
$logDir = [IO.Path]::GetFullPath($logDir)
$staging = if ($cfg.exportStaging) { [string]$cfg.exportStaging } else { Join-Path $scriptDir "..\..\workdir\kb-export" }
$staging = [IO.Path]::GetFullPath($staging)
$destLinux = if ($cfg.exportDestLinux) { [string]$cfg.exportDestLinux } else { "/srv/ai2/kb/src" }

New-Item -ItemType Directory -Force -Path $logDir, $staging | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

$skipGraphReindex = $false
if ($null -ne $cfg.skipGraphReindex) {
    $skipGraphReindex = [bool]$cfg.skipGraphReindex
}
$indexerRef = if ($cfg.graphIndexerContainer) { [string]$cfg.graphIndexerContainer } else { "ai2-metadata-indexer-kb" }
$neo4jRef = if ($cfg.graphNeo4jContainer) { [string]$cfg.graphNeo4jContainer } else { "ai2-neo4j-kb" }
$bslContainer = if ($cfg.bslContainer) { [string]$cfg.bslContainer } else { "ai2-bsl-kb" }
$skipCodeIndex = $false
if ($null -ne $cfg.skipCodeIndex) { $skipCodeIndex = [bool]$cfg.skipCodeIndex }

Write-Host "profile: $Profile"
Write-Host "allowed ops: store-update (UpdateCfg+UpdateDBCfg), dump-xml, copy, graph-reindex(full), code-index"
Write-Host "staging: $staging"
Write-Host "dest:    $destLinux"
Write-Host "graph-reindex: $(if ($skipGraphReindex) { 'SKIP' } else { "full via $indexerRef" })"
Write-Host "code-index: $(if ($skipCodeIndex) { 'SKIP' } else { $bslContainer })"

# (1) хранилище → конфигурация ИБ + применение к БД
Invoke-DesignerAllowed -Op "store-update" -Cfg $cfg -LogDir $logDir -Stamp $stamp

# (2) выгрузка в файлы
Get-ChildItem -LiteralPath $staging -Force -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $staging | Out-Null
Invoke-DesignerAllowed -Op "dump-xml" -Cfg $cfg -LogDir $logDir -Stamp $stamp -ExportPath $staging

$cfgXml = Join-Path $staging "Configuration.xml"
if (-not (Test-Path -LiteralPath $cfgXml)) {
    throw "Dump finished but Configuration.xml missing in $staging"
}
$fileCount = (Get-ChildItem -LiteralPath $staging -Recurse -File | Measure-Object).Count
Write-Host "dump files: $fileCount"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "docker not found; needed to copy into $destLinux"
}
Copy-WinTreeToLinuxPath -StagingWin $staging -DestLinux $destLinux -VolumePrefix "kb-export-stage"

# (3) полная переиндексация графа КБ (без apply_changes)
if (-not $skipGraphReindex) {
    Invoke-KbGraphFullReindex `
        -ReferenceContainer $indexerRef `
        -Neo4jContainer $neo4jRef `
        -SrcLinux $destLinux `
        -LogDir $logDir `
        -Stamp $stamp
}

# (4) code-index
if (-not $skipCodeIndex) {
    Invoke-Ai2BslCodeIndex -Container $bslContainer -LogDir $logDir -Stamp $stamp
}

Write-Host "=== ALL DONE $(Get-Date -Format o) ==="
