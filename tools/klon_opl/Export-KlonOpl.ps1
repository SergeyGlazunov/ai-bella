<#
.SYNOPSIS
  klon_opl: выгрузить конфигурацию в файлы → /srv/ai2/klon_opl/src

.DESCRIPTION
  Запуск: Windows PowerShell. Хранилища нет — только DumpConfigToFiles + copy в WSL.
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

function Copy-DumpToLinux {
    param(
        [string]$StagingWin,
        [string]$DestLinux
    )
    if (-not (Test-Path -LiteralPath (Join-Path $StagingWin "Configuration.xml"))) {
        throw "Staging missing Configuration.xml: $StagingWin"
    }

    Write-Host "=== copy $StagingWin -> $DestLinux ==="
    $vol = "klon-opl-export-stage-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
    docker volume create $vol | Out-Null
    try {
        $cid = docker create -v "${vol}:/data" alpine
        if (-not $cid) { throw "docker create failed" }
        try {
            docker cp "$StagingWin/." "${cid}:/data/"
            if ($LASTEXITCODE -ne 0) { throw "docker cp into volume failed" }
        }
        finally {
            docker rm $cid | Out-Null
        }

        docker run --rm `
            -v "${vol}:/src:ro" `
            -v "${DestLinux}:/dest" `
            alpine sh -c @'
set -e
mkdir -p /dest
find /dest -mindepth 1 -maxdepth 1 -exec rm -rf {} +
cp -a /src/. /dest/
test -f /dest/Configuration.xml
test -f /dest/ConfigDumpInfo.xml
echo COPY_OK
find /dest -type f | wc -l
'@
        if ($LASTEXITCODE -ne 0) { throw "copy into $DestLinux failed" }
    }
    finally {
        docker volume rm $vol 2>$null | Out-Null
    }
}

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

New-Item -ItemType Directory -Force -Path $logDir, $staging | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"

Write-Host "profile: $Profile"
Write-Host "allowed ops: dump-xml, copy (no store)"
Write-Host "staging: $staging"
Write-Host "dest:    $destLinux"

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
Copy-DumpToLinux -StagingWin $staging -DestLinux $destLinux

Write-Host "=== ALL DONE $(Get-Date -Format o) ==="
