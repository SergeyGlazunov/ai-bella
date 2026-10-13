# Common helpers for UBD Git -> Neo4j -> MCP on a target Windows machine.
$ErrorActionPreference = "Stop"
$packageRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$configPath = Join-Path $packageRoot "deployment.config.psd1"
if (-not (Test-Path $configPath)) {
    throw "Missing $configPath. Copy deployment.config.example.psd1 to deployment.config.psd1 and fill in the target-machine values."
}
$config = Import-PowerShellDataFile $configPath
$required = @("McpRoot", "DockerUser", "DockerUserProfile", "DockerApiPort", "DockerTaskName",
    "DockerStartScript", "GraphPort", "ConfigGitUrl", "GraphStackGitUrl", "WslMemoryGB",
    "SyncTaskName", "SyncAt")
foreach ($name in $required) {
    if (-not $config.ContainsKey($name) -or [string]::IsNullOrWhiteSpace([string]$config[$name])) {
        throw "deployment.config.psd1: required value '$name' is empty."
    }
}
if ($config.ConfigGitUrl -match "example\.local" -or $config.DockerUser -eq "docker-service-user") {
    throw "deployment.config.psd1 still contains example values."
}

$script:McpRoot = $config.McpRoot.TrimEnd("\")
$script:UbdIndex = Join-Path $script:McpRoot "ubd_index"
$script:UbdConfig = Join-Path $script:McpRoot "ubd_config"
$script:McpSuite = Join-Path $script:McpRoot "mcp_for_1c"
$script:GitLabUrl = $config.ConfigGitUrl
$script:McpFor1cUrl = $config.GraphStackGitUrl
$script:GitHost = ([Uri]$script:GitLabUrl).Host
$script:LanIp = [string]$config.LanIp
$script:McpPort = [int]$config.GraphPort
$script:DockerApiPort = [int]$config.DockerApiPort
$script:DockerUser = $config.DockerUser
$script:DockerUserProfile = $config.DockerUserProfile
$script:DockerTaskName = $config.DockerTaskName
$script:DockerStartScript = $config.DockerStartScript
$script:WebUiNetwork = [string]$config.WebUiNetwork
$script:WslMemoryGB = [int]$config.WslMemoryGB
$script:SyncTaskName = $config.SyncTaskName
$script:SyncAt = $config.SyncAt
$env:DOCKER_HOST = "tcp://127.0.0.1:$($script:DockerApiPort)"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "=== $Message ===" -ForegroundColor Cyan
}

function Get-WslConnectIp {
    $raw = netsh interface portproxy show v4tov4 2>$null | Out-String
    $line = ($raw -split "`r?`n") |
        Where-Object { $_ -match "\s$([regex]::Escape([string]$script:DockerApiPort))\s" } |
        Select-Object -First 1
    if (-not $line) { return $null }
    $parts = $line -split '\s+' | Where-Object { $_ }
    # columns: listen-addr listen-port connect-addr connect-port
    if ($parts.Count -ge 4) { return $parts[2] }
    return $null
}

function ConvertTo-UbdWslPath([string]$WinPath) {
    if ($WinPath -match '^([A-Za-z]):\\(.*)$') {
        return '/mnt/' + $Matches[1].ToLowerInvariant() + '/' + ($Matches[2] -replace '\\', '/')
    }
    throw "Cannot convert path for WSL: $WinPath"
}

# wsl.exe under Task Scheduler often emits UTF-16; NULs break -match / string ops.
function ConvertFrom-UbdWslOutput {
    param($InputObject)
    return (($InputObject | Out-String) -replace "`0", "").Trim()
}

# Nightly graph/code: ai2 is the sole runtime (chat MCP reads /srv/ai2/...).
# Prefer plain `docker run` (DOCKER_HOST TCP). No Windows compose plugin / wsl.exe.
$script:UbdIndexerRefContainer = if ($config.ContainsKey("Ai2IndexerContainer") -and $config.Ai2IndexerContainer) {
    [string]$config.Ai2IndexerContainer
} else { "ai2-metadata-indexer" }
$script:UbdNeo4jContainer = if ($config.ContainsKey("Ai2Neo4jContainer") -and $config.Ai2Neo4jContainer) {
    [string]$config.Ai2Neo4jContainer
} else { "ai2-neo4j" }
$script:UbdBslContainer = if ($config.ContainsKey("Ai2BslContainer") -and $config.Ai2BslContainer) {
    [string]$config.Ai2BslContainer
} else { "ai2-bsl-indexer" }
$script:UbdSrcLinux = if ($config.ContainsKey("Ai2SrcLinux") -and $config.Ai2SrcLinux) {
    [string]$config.Ai2SrcLinux
} else { "/srv/ai2/ubd/src" }

function Test-UbdIndexerDockerRunAvailable {
    param(
        [string]$ReferenceContainer = $script:UbdIndexerRefContainer,
        [string]$Neo4jContainer = $script:UbdNeo4jContainer
    )
    $env:DOCKER_HOST = "tcp://127.0.0.1:$($script:DockerApiPort)"
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $null = docker inspect $ReferenceContainer 2>$null
        if ($LASTEXITCODE -ne 0) {
            return [pscustomobject]@{
                Ok      = $false
                Via     = $null
                Image   = $null
                Network = $null
                Message = "reference container missing: $ReferenceContainer"
            }
        }
        $ref = (docker inspect $ReferenceContainer | ConvertFrom-Json)[0]
        $image = [string]$ref.Config.Image
        $network = $null
        if ($ref.NetworkSettings.Networks) {
            $network = @($ref.NetworkSettings.Networks.PSObject.Properties.Name)[0]
        }
        if (-not $network) { $network = [string]$ref.HostConfig.NetworkMode }
        if ([string]::IsNullOrWhiteSpace($network) -or $network -eq "default") {
            return [pscustomobject]@{
                Ok      = $false
                Via     = "docker-run"
                Image   = $image
                Network = $network
                Message = "no usable network on $ReferenceContainer"
            }
        }
        $null = docker image inspect $image 2>$null
        if ($LASTEXITCODE -ne 0) {
            return [pscustomobject]@{
                Ok      = $false
                Via     = "docker-run"
                Image   = $image
                Network = $network
                Message = "image missing: $image"
            }
        }
        $neo = (docker inspect --format "{{.State.Status}}" $Neo4jContainer 2>$null)
        if ($neo -ne "running") {
            return [pscustomobject]@{
                Ok      = $false
                Via     = "docker-run"
                Image   = $image
                Network = $network
                Message = "neo4j not running (status=$neo)"
            }
        }
        return [pscustomobject]@{
            Ok      = $true
            Via     = "docker-run"
            Image   = $image
            Network = $network
            Message = "indexer docker-run ready"
        }
    }
    finally {
        $ErrorActionPreference = $old
    }
}

function New-UbdIndexerDockerRunArgs {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("apply_changes", "full_reindex")]
        [string]$Mode,
        [string]$ReferenceContainer = $script:UbdIndexerRefContainer,
        [string]$RunName = ""
    )
    $env:DOCKER_HOST = "tcp://127.0.0.1:$($script:DockerApiPort)"
    $avail = Test-UbdIndexerDockerRunAvailable -ReferenceContainer $ReferenceContainer
    if (-not $avail.Ok) {
        throw "indexer docker-run not available: $($avail.Message)"
    }
    $ref = (docker inspect $ReferenceContainer | ConvertFrom-Json)[0]
    $image = [string]$ref.Config.Image
    $network = [string]$avail.Network
    if (-not $RunName) {
        $RunName = "ubd-metadata-indexer-$Mode-$(Get-Date -Format 'yyyyMMddHHmmss')"
    }

    # FULL clear_metadata_layer batches can exceed the indexer default HTTP
    # timeout (60s) on a large УБД graph → TimeoutError. apply_changes already
    # honors NEO4J_TIMEOUT_SEC; mcp_metadata_indexer does not, so FULL uses a
    # bootstrap that patches Neo4j() + DELETE_BATCH_DEFAULT.
    $neoTimeout = if ($config.ContainsKey("Neo4jTimeoutSec") -and $config.Neo4jTimeoutSec) {
        [string]$config.Neo4jTimeoutSec
    }
    elseif ($Mode -eq "full_reindex") { "1800" }
    else { "600" }
    $deleteBatch = if ($config.ContainsKey("Neo4jDeleteBatch") -and $config.Neo4jDeleteBatch) {
        [string]$config.Neo4jDeleteBatch
    }
    else { "2000" }

    $dockerArgs = @(
        "run", "--rm",
        "--name", $RunName,
        "--network", $network,
        "-w", "/app",
        "-e", "PYTHONUNBUFFERED=1",
        "-e", "NEO4J_TIMEOUT_SEC=$neoTimeout",
        "-e", "NEO4J_DELETE_BATCH=$deleteBatch"
    )
    if ($Mode -eq "full_reindex") {
        $dockerArgs += "-e", "METADATA_FORCE_REINDEX=true"
        $dockerArgs += "-v", "/srv/ai2/tools/ubd/neo4j_timeout_bootstrap.py:/app/neo4j_timeout_bootstrap.py:ro"
    }
    else {
        $dockerArgs += "-e", "METADATA_FORCE_REINDEX=false"
    }

    foreach ($bind in @($ref.HostConfig.Binds)) {
        if ($bind) { $dockerArgs += "-v", $bind }
    }

    $skipPrefix = @(
        "PATH=", "LANG=", "GPG_KEY=", "PYTHON_VERSION=", "PYTHON_SHA256=",
        "METADATA_FORCE_REINDEX=", "PYTHONUNBUFFERED=",
        "NEO4J_TIMEOUT_SEC=", "NEO4J_DELETE_BATCH="
    )
    foreach ($e in @($ref.Config.Env)) {
        $skip = $false
        foreach ($p in $skipPrefix) {
            if ($e.StartsWith($p)) { $skip = $true; break }
        }
        if ($skip) { continue }
        $dockerArgs += "-e", $e
    }

    if ($Mode -eq "apply_changes") {
        $dockerArgs += "--entrypoint", "python", $image, "/app/apply_changes.py"
    }
    else {
        $dockerArgs += $image, "python", "/app/neo4j_timeout_bootstrap.py"
    }
    return , $dockerArgs
}

# Windows docker.exe on this stand often has no compose plugin. Prefer WSL compose.
# Kept for one-shot setup scripts (03-build-and-index); nightly sync uses docker-run helpers above.
# Do not trust $LASTEXITCODE after `wsl.exe` with redirected streams — parse an explicit marker.
# Retries: at 02:00 WSL/dockerd may still be warming up even when DOCKER_HOST already answers.
function Test-UbdComposeAvailable {
    param(
        [int]$Retries = 6,
        [int]$DelaySec = 10
    )
    if (Get-Command docker-compose -ErrorAction SilentlyContinue) {
        return [pscustomobject]@{ Ok = $true; Via = "docker-compose" }
    }
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $max = [Math]::Max(1, $Retries)
        for ($attempt = 1; $attempt -le $max; $attempt++) {
            # Single-quoted so PowerShell does not expand $?; marker is from bash.
            $marker = & wsl.exe -d Ubuntu -u root -- bash -lc 'docker compose version >/dev/null 2>&1; echo __COMPOSE_EXIT__:$?' 2>&1
            $joined = ConvertFrom-UbdWslOutput $marker
            if ($joined -match '__COMPOSE_EXIT__:0') {
                return [pscustomobject]@{ Ok = $true; Via = "wsl" }
            }
            $null = & cmd.exe /c "docker compose version >nul 2>nul"
            if ($LASTEXITCODE -eq 0) {
                return [pscustomobject]@{ Ok = $true; Via = "docker.exe compose" }
            }
            if ($attempt -lt $max) {
                Start-Sleep -Seconds $DelaySec
            }
        }
    }
    finally {
        $ErrorActionPreference = $old
    }
    return [pscustomobject]@{ Ok = $false; Via = $null }
}

function Invoke-Compose {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$ComposeArgs
    )
    if (-not (Test-Path $script:McpSuite)) {
        throw "No stack at $($script:McpSuite). Run 02-clone.ps1 first."
    }
    $env:DOCKER_HOST = "tcp://127.0.0.1:$($script:DockerApiPort)"
    $avail = Test-UbdComposeAvailable
    if (-not $avail.Ok) {
        throw "docker compose not available (no docker-compose, no Windows plugin, no WSL compose)"
    }
    if ($avail.Via -eq "docker-compose") {
        Push-Location $script:McpSuite
        try {
            & docker-compose @ComposeArgs
            if ($LASTEXITCODE -ne 0) { throw "docker-compose failed: $($ComposeArgs -join ' ')" }
        }
        finally { Pop-Location }
        return
    }
    if ($avail.Via -eq "docker.exe compose") {
        Push-Location $script:McpSuite
        try {
            & docker.exe compose @ComposeArgs
            if ($LASTEXITCODE -ne 0) { throw "docker compose failed: $($ComposeArgs -join ' ')" }
        }
        finally { Pop-Location }
        return
    }
    $linuxDir = ConvertTo-UbdWslPath $script:McpSuite
    $joined = ($ComposeArgs | ForEach-Object {
            $a = $_ -replace "'", "'\''"
            if ($a -match '[\s"]') { "'$a'" } else { $a }
        }) -join ' '
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $bashCmd = "cd '$linuxDir' && docker compose $joined; echo __COMPOSE_EXIT__:" + '$?'
        $out = & wsl.exe -d Ubuntu -u root -- bash -lc $bashCmd 2>&1
        $text = ConvertFrom-UbdWslOutput $out
        $printable = ($text -replace '__COMPOSE_EXIT__:\d+', '').TrimEnd()
        if ($printable) { Write-Host $printable }
        if ($text -notmatch '__COMPOSE_EXIT__:0') {
            throw "wsl docker compose failed: $($ComposeArgs -join ' ')"
        }
    }
    finally {
        $ErrorActionPreference = $old
    }
}

function Get-GitLabToken {
    $gitEnv = Join-Path $script:UbdIndex "git.env"
    if (-not (Test-Path $gitEnv)) {
        throw "Missing $gitEnv. Run 01-prepare.ps1 and paste the Deploy Token."
    }
    $line = Get-Content $gitEnv -Encoding ASCII | Where-Object { $_ -match '^\s*GITLAB_TOKEN=' } | Select-Object -First 1
    if (-not $line) { throw "GITLAB_TOKEN is empty in $gitEnv" }
    return ($line -replace '^\s*GITLAB_TOKEN=', '').Trim().Trim('"').Trim("'")
}

function Protect-SecretFile([string]$Path) {
    if (-not (Test-Path $Path)) { return }
    icacls $Path /inheritance:r | Out-Null
    $admins = (Get-LocalGroup -SID S-1-5-32-544).Name
    icacls $Path /grant:r "${admins}:(R,W)" | Out-Null
    if (Get-LocalUser -Name $script:DockerUser -ErrorAction SilentlyContinue) {
        icacls $Path /grant:r "$($script:DockerUser):(R)" | Out-Null
    }
}

function Convert-ToWslPath([string]$WindowsPath) {
    $full = [IO.Path]::GetFullPath($WindowsPath)
    if ($full -notmatch '^([A-Za-z]):\\(.*)$') {
        throw "Cannot convert path to WSL form: $full"
    }
    return "/mnt/$($Matches[1].ToLower())/" + ($Matches[2] -replace '\\', '/')
}

function Find-ConfigRoot([string]$RepoRoot) {
    $candidates = @(
        (Join-Path $RepoRoot "SRC"),
        (Join-Path $RepoRoot "src\cf"),
        (Join-Path $RepoRoot "src"),
        $RepoRoot
    )
    foreach ($dir in $candidates) {
        if (Test-Path (Join-Path $dir "Configuration.xml")) {
            return (Resolve-Path $dir).Path
        }
    }
    $found = Get-ChildItem $RepoRoot -Filter "Configuration.xml" -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($found) { return $found.Directory.FullName }
    return $null
}
