<#
.SYNOPSIS
  klon_opl: выгрузить конфигурацию → /srv/ai2/klon_opl/src → code-index (ai2-bsl-klon-opl)

.DESCRIPTION
  Запуск: Windows PowerShell. Хранилища нет — DumpConfigToFiles + copy + code-index.
  Белый список ключей DESIGNER: только dump-xml.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\klon_opl\Export-KlonOpl.ps1
#>
[CmdletBinding()]
param(
    [string]$Profile = ""
)

$ErrorActionPreference = "Stop"

function Read-KlonOplProfile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Profile not found: $Path"
    }
    $cfg = [IO.File]::ReadAllText($Path, [Text.UTF8Encoding]::new($false)) | ConvertFrom-Json
    foreach ($req in @("exePath", "server", "ref", "ibUser", "ibPassword")) {
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

function Invoke-DesignerDump {
    param(
        $Cfg,
        [string]$LogDir,
        [string]$Stamp,
        [string]$ExportPath
    )

    $log = Join-Path $LogDir "dump-xml-$Stamp.log"
    $connect = "/S`"$($Cfg.server)\$($Cfg.ref)`""

    $argList = @(
        "DESIGNER",
        $connect,
        "/N`"$($Cfg.ibUser)`"",
        "/P`"$($Cfg.ibPassword)`"",
        "/DisableStartupDialogs",
        "/Out`"$log`"",
        "/DumpConfigToFiles",
        "`"$ExportPath`""
    )

    Write-Host "=== dump-xml start $(Get-Date -Format o) ==="
    Write-Host "ib: $($Cfg.server)\$($Cfg.ref)"
    Write-Host "log: $log"

    if (-not (Test-Path -LiteralPath $Cfg.exePath)) {
        throw "1cv8 not found: $($Cfg.exePath)"
    }

    $sw = [Diagnostics.Stopwatch]::StartNew()
    $p = Start-Process -FilePath $Cfg.exePath -ArgumentList $argList -Wait -PassThru -NoNewWindow
    $sw.Stop()
    Write-Host "dump-xml exit=$($p.ExitCode) elapsed_sec=$([math]::Round($sw.Elapsed.TotalSeconds, 1))"

    $txt = Get-LogText -Path $log
    if ($txt.Trim().Length -gt 0) {
        Write-Host "---- log ----"
        Write-Host $txt.TrimEnd()
        Write-Host "-------------"
    }

    if ($p.ExitCode -ne 0) {
        throw "dump-xml failed exit=$($p.ExitCode). See $log"
    }
}

$common = Join-Path $PSScriptRoot "..\common\Docker-Index.ps1"
. (Resolve-Path $common).Path

# --- main ---
$scriptDir = $PSScriptRoot
if (-not $Profile) {
    $Profile = Join-Path $scriptDir "klon_opl-connection.json"
}
$Profile = [IO.Path]::GetFullPath($Profile)
$cfg = Read-KlonOplProfile -Path $Profile

$logDir = if ($cfg.logDir) { [string]$cfg.logDir } else { Join-Path $scriptDir "..\..\logs\klon_opl" }
$logDir = [IO.Path]::GetFullPath($logDir)
$staging = if ($cfg.exportStaging) { [string]$cfg.exportStaging } else { Join-Path $scriptDir "..\..\workdir\klon_opl-export" }
$staging = [IO.Path]::GetFullPath($staging)
$destLinux = if ($cfg.exportDestLinux) { [string]$cfg.exportDestLinux } else { "/srv/ai2/klon_opl/src" }
$bslContainer = if ($cfg.bslContainer) { [string]$cfg.bslContainer } else { "ai2-bsl-klon-opl" }
$skipCodeIndex = $false
if ($null -ne $cfg.skipCodeIndex) { $skipCodeIndex = [bool]$cfg.skipCodeIndex }

New-Item -ItemType Directory -Force -Path $logDir, $staging | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

Write-Host "profile: $Profile"
Write-Host "allowed ops: dump-xml, copy, code-index (no store)"
Write-Host "staging: $staging"
Write-Host "dest:    $destLinux"
Write-Host "code-index: $(if ($skipCodeIndex) { 'SKIP' } else { $bslContainer })"

Get-ChildItem -LiteralPath $staging -Force -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $staging | Out-Null
Invoke-DesignerDump -Cfg $cfg -LogDir $logDir -Stamp $stamp -ExportPath $staging

$cfgXml = Join-Path $staging "Configuration.xml"
if (-not (Test-Path -LiteralPath $cfgXml)) {
    throw "Dump finished but Configuration.xml missing in $staging"
}
$fileCount = (Get-ChildItem -LiteralPath $staging -Recurse -File | Measure-Object).Count
Write-Host "dump files: $fileCount"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "docker not found; needed to copy into $destLinux"
}
Copy-WinTreeToLinuxPath -StagingWin $staging -DestLinux $destLinux -VolumePrefix "klon-opl-export-stage"

if (-not $skipCodeIndex) {
    Invoke-Ai2BslCodeIndex -Container $bslContainer -LogDir $logDir -Stamp $stamp
}

Write-Host "=== ALL DONE $(Get-Date -Format o) ==="
