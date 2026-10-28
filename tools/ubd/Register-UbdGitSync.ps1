<#
.SYNOPSIS
  Register daily git-only refresh of C:\MCP\ubd_config (no indexing).

.DESCRIPTION
  Contour (ai-sync.timer ~01:30) fetches UBD from /mnt/c/MCP/ubd_config.
  After cutover, full "UBD Config Sync" stays Disabled. This task only pulls
  GitLab into the Windows mirror so contour sees fresh commits.

  Default At=01:00 so the mirror is ready before ai-sync.timer.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "UBD Config Git Sync",
    [string]$At = "01:00",
    [string]$UserId = $env:USERNAME,
    [string]$ScriptPath = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ScriptPath)) {
    $candidates = @(
        "C:\MCP\ubd_index\scripts\sync-git-only.ps1",
        (Join-Path $PSScriptRoot "scripts\sync-git-only.ps1")
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) {
            $ScriptPath = $c
            break
        }
    }
}
if (-not (Test-Path -LiteralPath $ScriptPath)) {
    throw "Missing sync-git-only.ps1 (checked C:\MCP\ubd_index\scripts and tools/ubd/scripts)"
}

$action = New-ScheduledTaskAction `
    -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""

$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

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
Write-Host "Script: $ScriptPath"
Write-Host "Keep 'UBD Config Sync' Disabled (indexing owned by contour)."
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
