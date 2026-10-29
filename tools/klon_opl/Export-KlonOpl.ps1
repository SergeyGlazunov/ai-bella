<#
.SYNOPSIS
  klon_opl: DumpConfigToFiles → immutable package in Linux inbox (no Docker / index).

.DESCRIPTION
  Запуск: Windows PowerShell. Хранилища нет — только dump + публикация пакета.
  Индексация BSL — на стороне Linux orchestrator (contour).

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

# --- main ---
$scriptDir = $PSScriptRoot
Import-Module (Join-Path $scriptDir "..\common\Export-Package.psm1") -Force

if (-not $Profile) {
    $Profile = Join-Path $scriptDir "klon_opl-connection.json"
}
$Profile = [IO.Path]::GetFullPath($Profile)
$cfg = Read-KlonOplProfile -Path $Profile

$logDir = if ($cfg.logDir) { [string]$cfg.logDir } else { Join-Path $scriptDir "..\..\logs\klon_opl" }
$logDir = [IO.Path]::GetFullPath($logDir)
$staging = if ($cfg.exportStaging) { [string]$cfg.exportStaging } else { Join-Path $scriptDir "..\..\workdir\klon_opl-export" }
$staging = [IO.Path]::GetFullPath($staging)
$packageInbox = if ($cfg.packageInbox) { [string]$cfg.packageInbox } else { Join-Path $scriptDir "..\..\workdir\inbox\klon_opl" }
$packageInbox = [IO.Path]::GetFullPath($packageInbox)

New-Item -ItemType Directory -Force -Path $logDir, $staging, $packageInbox | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$startedAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$platformVersion = if ($cfg.platformVersion) { [string]$cfg.platformVersion } else { Get-PlatformVersionLabel -ExePath $cfg.exePath }

Write-Host "profile: $Profile"
Write-Host "allowed ops: dump-xml, package-publish (no store)"
Write-Host "staging: $staging"
Write-Host "inbox:   $packageInbox"

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

$finishedAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$manifest = New-ExportManifest `
    -Database "klon_opl" `
    -ExportId $stamp `
    -SourcePath $staging `
    -Operations @{ DumpConfigToFiles = "ok" } `
    -PlatformVersion $platformVersion `
    -StartedAt $startedAt `
    -FinishedAt $finishedAt

$readyPath = Publish-ExportPackage -SourcePath $staging -DestinationRoot $packageInbox -Manifest $manifest
# \\wsl$ drops are root:root 755; contour claim rename needs write on the package dir.
& (Join-Path $scriptDir "..\common\Set-WslPackageOwner.ps1") -WinPath $readyPath

Write-Host "=== ALL DONE $(Get-Date -Format o) ==="
