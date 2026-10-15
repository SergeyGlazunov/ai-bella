<#
.SYNOPSIS
  КБ: store+dump → immutable package in Linux inbox (no Docker / index).

.DESCRIPTION
  Запуск: Windows PowerShell. Только операции 1С (UpdateCfg+UpdateDBCfg, DumpConfigToFiles)
  и публикация пакета ready-<export-id> с READY последним.
  Индексация (graph / BSL) — на стороне Linux orchestrator (contour).

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

function Get-PlatformVersionLabel {
    param([string]$ExePath)
    try {
        $v = [Diagnostics.FileVersionInfo]::GetVersionInfo($ExePath)
        if ($v.ProductVersion) { return [string]$v.ProductVersion }
        if ($v.FileVersion) { return [string]$v.FileVersion }
    }
    catch { }
    return "unknown"
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

# --- main ---
$scriptDir = $PSScriptRoot
Import-Module (Join-Path $scriptDir "..\common\Export-Package.psm1") -Force

if (-not $Profile) {
    $Profile = Join-Path $scriptDir "kb-connection.json"
}
$Profile = [IO.Path]::GetFullPath($Profile)
$cfg = Read-KbProfile -Path $Profile

$logDir = if ($cfg.logDir) { [string]$cfg.logDir } else { Join-Path $scriptDir "..\..\logs\kb" }
$logDir = [IO.Path]::GetFullPath($logDir)
$staging = if ($cfg.exportStaging) { [string]$cfg.exportStaging } else { Join-Path $scriptDir "..\..\workdir\kb-export" }
$staging = [IO.Path]::GetFullPath($staging)
$packageInbox = if ($cfg.packageInbox) { [string]$cfg.packageInbox } else { Join-Path $scriptDir "..\..\workdir\inbox\kb" }
$packageInbox = [IO.Path]::GetFullPath($packageInbox)

New-Item -ItemType Directory -Force -Path $logDir, $staging, $packageInbox | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$startedAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$platformVersion = if ($cfg.platformVersion) { [string]$cfg.platformVersion } else { Get-PlatformVersionLabel -ExePath $cfg.exePath }

Write-Host "profile: $Profile"
Write-Host "allowed ops: store-update (UpdateCfg+UpdateDBCfg), dump-xml, package-publish"
Write-Host "staging: $staging"
Write-Host "inbox:   $packageInbox"

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

$finishedAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$manifest = New-ExportManifest `
    -Database "kb" `
    -ExportId $stamp `
    -SourcePath $staging `
    -Operations @{
        DumpConfigToFiles = "ok"
        UpdateCfg         = "ok"
        UpdateDBCfg       = "ok"
    } `
    -PlatformVersion $platformVersion `
    -StartedAt $startedAt `
    -FinishedAt $finishedAt

$null = Publish-ExportPackage -SourcePath $staging -DestinationRoot $packageInbox -Manifest $manifest

Write-Host "=== ALL DONE $(Get-Date -Format o) ==="
