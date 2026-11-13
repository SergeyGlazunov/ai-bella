# Collect-WinMetrics.ps1 — Windows host metrics for night 1C exports (every 5 min via Task Scheduler)
# Default output: C:\Scripts\win_host.csv (override with -OutFile or METRICS_WIN_CSV env)

param(
    [string]$OutFile = $(if ($env:METRICS_WIN_CSV) { $env:METRICS_WIN_CSV } else { "C:\Scripts\win_host.csv" })
)

$ErrorActionPreference = "Continue"
$dir = Split-Path -Parent $OutFile
if ($dir -and -not (Test-Path $dir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
}

if (-not (Test-Path $OutFile)) {
    "timestamp,win_cpu_perc,win_ram_free_mb,win_ram_total_mb,vmmem_ram_mb,disk_queue" |
        Out-File $OutFile -Encoding utf8
}

$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

$cpuSample = Get-Counter '\Processor(_Total)\% Processor Time' -ErrorAction SilentlyContinue
$cpu = if ($cpuSample) {
    [math]::Round($cpuSample.CounterSamples.CookedValue, 1)
} else { 0 }

$os = Get-CimInstance Win32_OperatingSystem
$freeMb = [math]::Round($os.FreePhysicalMemory / 1024, 0)
$totalMb = [math]::Round($os.TotalVisibleMemorySize / 1024, 0)

$vmmem = Get-Process -Name "vmmem*" -ErrorAction SilentlyContinue |
    Measure-Object -Property WorkingSet64 -Sum
$vmmemMb = if ($vmmem.Sum) { [math]::Round($vmmem.Sum / 1MB, 0) } else { 0 }

$diskSample = Get-Counter '\PhysicalDisk(_Total)\Current Disk Queue Length' -ErrorAction SilentlyContinue
$diskQ = if ($diskSample) {
    [math]::Round($diskSample.CounterSamples.CookedValue, 2)
} else { 0 }

"$ts,$cpu,$freeMb,$totalMb,$vmmemMb,$diskQ" | Out-File $OutFile -Append -Encoding utf8
