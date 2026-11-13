# Register-WinMetricsTask.ps1 — schedule Collect-WinMetrics.ps1 every 5 minutes (run as Administrator)
param(
    [string]$ScriptPath = (Join-Path $PSScriptRoot "Collect-WinMetrics.ps1"),
    [string]$OutFile = "C:\Scripts\win_host.csv",
    [string]$TaskName = "AI_Contour_Metrics",
    [int]$Days = 14
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $ScriptPath)) {
    throw "Collector not found: $ScriptPath"
}

$outDir = Split-Path -Parent $OutFile
if (-not (Test-Path $outDir)) {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
}

$arg = "-NoProfile -WindowStyle Hidden -File `"$ScriptPath`" -OutFile `"$OutFile`""
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days $Days)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -User "SYSTEM" -Force | Out-Null

Write-Host "Registered task '$TaskName' -> $ScriptPath (CSV: $OutFile) for $Days days."
