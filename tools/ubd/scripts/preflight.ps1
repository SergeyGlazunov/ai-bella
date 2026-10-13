param(
    [ValidateSet("Quick", "Full")][string]$Mode = "Quick",
    [switch]$RepairDockerTask,
    [switch]$ForceRepair,
    [switch]$SelfTest
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here "lib.ps1")
$ss = Join-Path $here "sync-state.ps1"
if (Test-Path $ss) { . $ss }

$script:PfResults = New-Object System.Collections.Generic.List[object]

function Add-PfResult([string]$Id, [ValidateSet("PASS", "FAIL", "WARN")][string]$Status, [string]$Message) {
    $script:PfResults.Add([pscustomobject]@{ id = $Id; status = $Status; message = $Message })
    $color = switch ($Status) { "PASS" { "Green" } "FAIL" { "Red" } default { "Yellow" } }
    Write-Host "[$Status] $Id $Message" -ForegroundColor $color
}

function Get-PfExitCode {
    if ($script:PfResults | Where-Object { $_.status -eq "FAIL" }) { return 1 }
    return 0
}

function Write-PfReport {
    $passCount = @($script:PfResults | Where-Object { $_.status -eq "PASS" }).Count
    $warnCount = @($script:PfResults | Where-Object { $_.status -eq "WARN" }).Count
    $failCount = @($script:PfResults | Where-Object { $_.status -eq "FAIL" }).Count
    Write-Host ""
    Write-Host "=== Preflight $Mode ===" -ForegroundColor Cyan
    Write-Host "Summary: $passCount PASS, $warnCount WARN, $failCount FAIL"
    if ($failCount -gt 0) {
        Write-Host "Result: FAIL" -ForegroundColor Red
    }
    elseif ($warnCount -gt 0) {
        Write-Host "Result: PASS (with warnings)" -ForegroundColor Yellow
    }
    else {
        Write-Host "Result: PASS" -ForegroundColor Green
    }
}

function Save-PfJson {
    $path = Join-Path $script:UbdIndex "logs\preflight-last.json"
    $dir = Split-Path -Parent $path
    if (-not [string]::IsNullOrWhiteSpace($dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $rows = foreach ($r in $script:PfResults) {
        @{ id = $r.id; status = $r.status; message = $r.message }
    }
    $payload = @{
        utc     = (Get-Date).ToUniversalTime().ToString("o")
        mode    = [string]$Mode
        results = @($rows)
    }
    $json = $payload | ConvertTo-Json -Depth 5
    Set-Content -LiteralPath $path -Value $json -Encoding UTF8
}

if ($SelfTest) {
    Add-PfResult "T1" "PASS" "ok"
    if ((Get-PfExitCode) -ne 0) { throw "PASS-only should be 0" }
    $script:PfResults.Clear()
    Add-PfResult "T2" "WARN" "w"
    if ((Get-PfExitCode) -ne 0) { throw "WARN-only should be 0" }
    $script:PfResults.Clear()
    Add-PfResult "T3" "FAIL" "f"
    if ((Get-PfExitCode) -ne 1) { throw "FAIL should be 1" }
    Write-Host "SelfTest OK"
    exit 0
}

if ($RepairDockerTask -and $Mode -ne "Full") {
    throw "-RepairDockerTask requires -Mode Full"
}

Add-PfResult "Q1" "PASS" "deployment.config.psd1 loaded"

$env:DOCKER_HOST = "tcp://127.0.0.1:$($script:DockerApiPort)"
$old = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$ver = docker version --format "{{.Server.Version}}" 2>$null
$ErrorActionPreference = $old
if ([string]::IsNullOrWhiteSpace($ver)) {
    Add-PfResult "Q2" "FAIL" "Docker Server not reachable at DOCKER_HOST=$env:DOCKER_HOST"
}
else {
    Add-PfResult "Q2" "PASS" "Docker Server $ver"
}

# Nightly graph path: docker run against DOCKER_HOST (no Windows compose plugin / no wsl.exe).
$idxRun = Test-UbdIndexerDockerRunAvailable
if ($idxRun.Ok) {
    Add-PfResult "Q3" "PASS" "$($idxRun.Message); image=$($idxRun.Image) net=$($idxRun.Network)"
}
else {
    Add-PfResult "Q3" "FAIL" "indexer docker-run unavailable: $($idxRun.Message)"
}

if (Test-Path (Join-Path $script:UbdConfig ".git")) {
    Add-PfResult "Q4" "PASS" "git clone at $($script:UbdConfig)"
}
else {
    Add-PfResult "Q4" "FAIL" "missing $($script:UbdConfig)\.git"
}

foreach ($pair in @(@{ id = "Q5a"; name = $script:DockerTaskName }, @{ id = "Q5b"; name = $script:SyncTaskName })) {
    $t = Get-ScheduledTask -TaskName $pair.name -ErrorAction SilentlyContinue
    if ($t) { Add-PfResult $pair.id "PASS" "task $($pair.name) exists" }
    else {
        # WARN (not FAIL): Docker may already be up via portproxy; sync can be re-registered
        # under the interactive account (same pattern as AI-contour KB/klon_opl tasks).
        Add-PfResult $pair.id "WARN" "task $($pair.name) missing; re-register via 05-schedule.ps1 or tools/ubd/Register-UbdNightly.ps1"
    }
}

$ErrorActionPreference = "Continue"
$ci = (docker inspect $script:UbdBslContainer --format "{{.State.Status}}" 2>$null)
$neo = (docker inspect $script:UbdNeo4jContainer --format "{{.State.Status}}" 2>$null)
$ErrorActionPreference = $old
if ($ci -ne "running") { Add-PfResult "Q6" "WARN" "$($script:UbdBslContainer) status=$ci" }
else { Add-PfResult "Q6" "PASS" "$($script:UbdBslContainer) running" }
if ($neo -ne "running") { Add-PfResult "Q6n" "WARN" "$($script:UbdNeo4jContainer) status=$neo" }
else { Add-PfResult "Q6n" "PASS" "$($script:UbdNeo4jContainer) running" }

function Test-UbdLocalPassword([string]$User, [string]$Password) {
    Add-Type -AssemblyName System.DirectoryServices.AccountManagement
    $ctx = New-Object System.DirectoryServices.AccountManagement.PrincipalContext('Machine')
    return $ctx.ValidateCredentials($User, $Password)
}

function Repair-UbdDockerTask {
    $f1 = $script:PfResults | Where-Object { $_.id -eq "F1" } | Select-Object -Last 1
    $f2 = $script:PfResults | Where-Object { $_.id -eq "F2" } | Select-Object -Last 1
    if (-not $f1 -or $f1.status -ne "FAIL") {
        Add-PfResult "R0" "WARN" "repair skipped: F1 not FAIL"
        return
    }
    if ((-not $f2 -or $f2.status -ne "PASS") -and -not $ForceRepair) {
        Add-PfResult "R0" "FAIL" "repair blocked: F2 not PASS (use -ForceRepair to override)"
        return
    }
    $pwFile = Join-Path $script:McpRoot "secrets\ai-docker.txt"
    if (-not (Test-Path $pwFile)) {
        Add-PfResult "R0" "FAIL" "repair blocked: missing $pwFile"
        return
    }
    $pw = (Get-Content $pwFile -Raw -Encoding UTF8).Trim()
    if (-not (Test-UbdLocalPassword -User $script:DockerUser -Password $pw)) {
        Add-PfResult "R0" "FAIL" "repair blocked: ValidateCredentials failed for $($script:DockerUser)"
        return
    }
    $exist = Get-ScheduledTask -TaskName $script:DockerTaskName -ErrorAction Stop
    $action = $exist.Actions | Select-Object -First 1
    $start = $script:DockerStartScript
    $startLeaf = Split-Path -Leaf $start
    $reuse = $false
    if ($action -and $action.Execute -and $action.Arguments) {
        if ($action.Arguments -like "*$start*" -or $action.Arguments -like "*$startLeaf*") {
            $reuse = $true
        }
    }
    if ($reuse) {
        $newAction = New-ScheduledTaskAction -Execute $action.Execute -Argument $action.Arguments
    }
    else {
        $newAction = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$start`""
    }
    $settings = $exist.Settings
    $triggers = @($exist.Triggers)
    Register-ScheduledTask -TaskName $script:DockerTaskName `
        -Action $newAction -Trigger $triggers -Settings $settings `
        -User $script:DockerUser -Password $pw `
        -RunLevel Highest -Force -ErrorAction Stop | Out-Null
    Add-PfResult "R0" "PASS" "Docker task re-registered as $($script:DockerUser)"
}

if ($Mode -eq "Full") {
    $dt = Get-ScheduledTask -TaskName $script:DockerTaskName -ErrorAction SilentlyContinue
    $uid = if ($dt) { [string]$dt.Principal.UserId } else { "" }
    $matchesUser = $uid -like "*$($script:DockerUser)*"
    if (-not $dt) {
        Add-PfResult "F1" "FAIL" "Docker task missing"
    }
    elseif ($matchesUser) {
        Add-PfResult "F1" "PASS" "Docker task owner $uid"
    }
    else {
        Add-PfResult "F1" "FAIL" "Docker task owner '$uid' != DockerUser '$($script:DockerUser)'"
    }

    $asDockerUser = ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -like "*\$($script:DockerUser)"
    $ErrorActionPreference = "Continue"
    # wsl.exe emits UTF-16; strip NULs so -match works under default console encoding
    $wslList = ((wsl -l -v 2>&1 | Out-String) -replace "`0", "")
    $ErrorActionPreference = $old
    $hasUbuntu = $wslList -match "Ubuntu"
    if ($asDockerUser -and $hasUbuntu) {
        Add-PfResult "F2" "PASS" "WSL Ubuntu visible under $($script:DockerUser)"
    }
    elseif ($hasUbuntu) {
        Add-PfResult "F2" "WARN" "Ubuntu visible in current session; confirm same distro under $($script:DockerUser) before repair (manual: runas /user:$($script:DockerUser) `"wsl -l -v`")"
    }
    else {
        Add-PfResult "F2" "WARN" "Ubuntu not listed in current session; do not repair without -ForceRepair until verified under $($script:DockerUser)"
    }

    $pwFile = Join-Path $script:McpRoot "secrets\ai-docker.txt"
    if (Test-Path $pwFile) {
        Add-PfResult "F3" "PASS" "password file present (not displayed)"
    }
    else {
        Add-PfResult "F3" "FAIL" "missing $pwFile"
    }

    $pp = netsh interface portproxy show v4tov4 2>$null | Out-String
    if ($pp -match [regex]::Escape([string]$script:DockerApiPort)) {
        Add-PfResult "F4a" "PASS" "portproxy mentions $($script:DockerApiPort)"
    }
    else {
        Add-PfResult "F4a" "FAIL" "portproxy missing DockerApiPort"
    }
    $drive = (Split-Path -Qualifier $script:McpRoot).TrimEnd(":")
    $freeGb = [math]::Round((Get-PSDrive $drive).Free / 1GB, 1)
    if ($freeGb -ge 25) {
        Add-PfResult "F4b" "PASS" "FreeGB=$freeGb"
    }
    else {
        Add-PfResult "F4b" "WARN" "FreeGB=$freeGb (<25 advisory)"
    }

    if (Get-Command Test-UbdProcessAlive -ErrorAction SilentlyContinue) {
        $lockPath = Join-Path $script:UbdIndex "state\sync.lock"
        if (-not (Test-Path $lockPath)) {
            Add-PfResult "F5" "PASS" "no sync.lock"
        }
        else {
            try {
                $info = Get-Content $lockPath -Raw -Encoding UTF8 | ConvertFrom-Json
                $pidLock = [int]$info.pid
                if (Test-UbdProcessAlive -ProcessId $pidLock) {
                    Add-PfResult "F5" "WARN" "sync.lock held by live pid $pidLock"
                }
                else {
                    Add-PfResult "F5" "WARN" "stale sync.lock pid $pidLock (dead)"
                }
            }
            catch {
                Add-PfResult "F5" "WARN" "unreadable sync.lock"
            }
        }
    }
    else {
        Add-PfResult "F5" "WARN" "sync-state not loaded; skip sync.lock"
    }

    if ($RepairDockerTask) {
        Repair-UbdDockerTask
    }
}

Write-PfReport
Save-PfJson
exit (Get-PfExitCode)
