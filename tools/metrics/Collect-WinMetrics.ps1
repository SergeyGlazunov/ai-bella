# Collect-WinMetrics.ps1 — Windows host metrics for night 1C exports (every 5 min via Task Scheduler)
# Avoids hanging Get-Counter calls; uses CIM + short-timeout counters.

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

function Get-CounterSafe {
    param([string]$Path, [int]$TimeoutSec = 8)
    $job = Start-Job -ScriptBlock {
        param($p)
        (Get-Counter $p -ErrorAction Stop).CounterSamples.CookedValue
    } -ArgumentList $Path
    if (Wait-Job $job -Timeout $TimeoutSec) {
        try { return [math]::Round((Receive-Job $job), 2) } catch { return 0 }
    }
    Stop-Job $job -ErrorAction SilentlyContinue
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    return 0
}

$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# Prefer CIM LoadPercentage (instant); fallback to timed Get-Counter
$cpuCim = Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue |
    Measure-Object -Property LoadPercentage -Average
$cpu = if ($null -ne $cpuCim.Average) {
    [math]::Round($cpuCim.Average, 1)
} else {
    Get-CounterSafe '\Processor(_Total)\% Processor Time' 8
}

$os = Get-CimInstance Win32_OperatingSystem
$freeMb = [math]::Round($os.FreePhysicalMemory / 1024, 0)
$totalMb = [math]::Round($os.TotalVisibleMemorySize / 1024, 0)

$vmmem = Get-Process -Name "vmmem*" -ErrorAction SilentlyContinue |
    Measure-Object -Property WorkingSet64 -Sum
$vmmemMb = if ($vmmem.Sum) { [math]::Round($vmmem.Sum / 1MB, 0) } else { 0 }

$diskQ = Get-CounterSafe '\PhysicalDisk(_Total)\Current Disk Queue Length' 8

"$ts,$cpu,$freeMb,$totalMb,$vmmemMb,$diskQ" | Out-File $OutFile -Append -Encoding utf8
