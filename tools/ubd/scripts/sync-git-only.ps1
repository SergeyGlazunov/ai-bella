# Git-only refresh of C:\MCP\ubd_config from GitLab.
# Contour indexes from /srv/ai-contour/sources/ubd with origin=/mnt/c/MCP/ubd_config.
# Full UBD Config Sync (indexing) stays Disabled; this task only updates the mirror.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path (Join-Path $here "lib.ps1")) {
    . (Join-Path $here "lib.ps1")
}
else {
    throw "lib.ps1 not found next to sync-git-only.ps1"
}

$logDir = Join-Path $script:UbdIndex "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("git-sync-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
function Log([string]$m) {
    $line = "{0:u} {1}" -f (Get-Date), $m
    Add-Content $log $line -Encoding UTF8
    Write-Host $line
}

Log "start git-only sync"

$env:GIT_TERMINAL_PROMPT = "0"
$env:GCM_INTERACTIVE = "never"
$env:GCM_MODAL_PROMPT = "false"

$credFile = Join-Path $script:UbdIndex ".git-credentials"
$credFileGit = ($credFile -replace '\\', '/')
function Invoke-GitQuiet {
    param([Parameter(Mandatory = $true)][string[]]$GitArgs)
    $helperArgs = @("-c", "credential.helper=", "-c", "credential.helper=store --file=$credFileGit")
    if (Test-Path $credFile) {
        & git -C $script:UbdConfig @helperArgs @GitArgs
    }
    else {
        & git -C $script:UbdConfig -c credential.helper= @GitArgs
    }
}

# Indexer artifacts must not block FF pull / dirty checks downstream.
$exclude = Join-Path $script:UbdConfig ".git\info\exclude"
if (Test-Path (Split-Path -Parent $exclude)) {
    $marker = "src/.code-index/"
    $existing = @()
    if (Test-Path $exclude) {
        $existing = Get-Content $exclude -ErrorAction SilentlyContinue
    }
    if ($existing -notcontains $marker) {
        Add-Content $exclude $marker -Encoding UTF8
        Log "added $marker to .git/info/exclude"
    }
}

$before = $null
try {
    $before = (git -C $script:UbdConfig rev-parse HEAD 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($before)) { throw "rev-parse before failed" }
}
catch {
    Log "ERROR git rev-parse HEAD (before) failed, see $log"
    exit 1
}
Log "HEAD before $before"

Invoke-GitQuiet @("fetch", "--prune", "origin")
if ($LASTEXITCODE -ne 0) {
    Log "ERROR git fetch failed, see $log"
    exit 1
}
Invoke-GitQuiet @("pull", "--ff-only")
if ($LASTEXITCODE -ne 0) {
    Log "ERROR git pull --ff-only failed, see $log"
    exit 1
}

$after = $null
try {
    $after = (git -C $script:UbdConfig rev-parse HEAD 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($after)) { throw "rev-parse after failed" }
}
catch {
    Log "ERROR git rev-parse HEAD (after) failed, see $log"
    exit 1
}
Log "HEAD after  $after"
if ($before -eq $after) {
    Log "unchanged"
}
else {
    Log "updated $before -> $after"
}
Log "done"
exit 0
