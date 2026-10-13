<#
.SYNOPSIS
  Register daily UBD git→ai2 sync task (same account pattern as KB/klon_opl).

.DESCRIPTION
  Registers as interactive Limited (like AI-contour KB nightly), not Highest.
  If an old Highest/admin-owned task blocks registration, run this script once
  from an elevated PowerShell to delete+recreate.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "UBD Config Sync",
    [string]$At = "02:00",
    [string]$UserId = $env:USERNAME
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

$principal = New-ScheduledTaskPrincipal `
    -UserId $UserId `
    -LogonType Interactive `
    -RunLevel Limited

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host "Registered task '$TaskName' daily at $At (User=$UserId, Interactive/Limited)"
Get-ScheduledTask -TaskName $TaskName | Format-List TaskName, State, @{
    N = 'User'
    E = { $_.Principal.UserId }
}, @{
    N = 'RunLevel'
    E = { $_.Principal.RunLevel }
}, @{
    N = 'Actions'
    E = { $_.Actions.Execute + ' ' + $_.Actions.Arguments }
}
Get-ScheduledTaskInfo -TaskName $TaskName | Format-List LastRunTime, LastTaskResult, NextRunTime
