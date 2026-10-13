<#
.SYNOPSIS
  Register daily UBD git→ai2 sync task (same account pattern as KB/klon_opl).

.DESCRIPTION
  Does not require the old "WSL Docker Engine" task. Uses DOCKER_HOST from
  C:\MCP\ubd_index\deployment.config.psd1 via sync-and-index.ps1 → lib.ps1.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "UBD Config Sync",
    [string]$At = "02:00"
)

$ErrorActionPreference = "Stop"
$sync = "C:\MCP\ubd_index\scripts\sync-and-index.ps1"
if (-not (Test-Path -LiteralPath $sync)) {
    throw "Missing $sync"
}

$action = New-ScheduledTaskAction `
    -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$sync`""

$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Highest `
    -Force | Out-Null

Write-Host "Registered task '$TaskName' daily at $At"
Get-ScheduledTask -TaskName $TaskName | Format-List TaskName, State, @{N='Actions';E={$_.Actions.Execute + ' ' + $_.Actions.Arguments}}
Get-ScheduledTaskInfo -TaskName $TaskName | Format-List LastRunTime, LastTaskResult, NextRunTime
