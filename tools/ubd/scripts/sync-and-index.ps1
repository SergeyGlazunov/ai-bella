# Nightly git pull + apply_changes.py. Run under the account that owns Docker/WSL.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path (Join-Path $here "lib.ps1")) {
    . (Join-Path $here "lib.ps1")
}
else {
    throw "lib.ps1 not found next to sync-and-index.ps1. Re-run 05-schedule.ps1 from the transfer package."
}

$stateMod = Join-Path $here "sync-state.ps1"
if (-not (Test-Path $stateMod)) {
    throw "sync-state.ps1 not found next to sync-and-index.ps1. Re-run 05-schedule.ps1 from the transfer package."
}
. $stateMod

$logDir = Join-Path $script:UbdIndex "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("sync-{0:yyyyMMdd-HHmmss}.log" -f (Get-Date))
function Log([string]$m) {
    $line = "{0:u} {1}" -f (Get-Date), $m
    Add-Content $log $line -Encoding UTF8
    Write-Host $line
}

Log "start"

if (-not (Enter-UbdSyncLock)) {
    Log "ERROR sync already running (sync.lock held)"
    Write-UbdSyncFailed -Phase "already_running" -Head "" -LogPath $log
    exit 1
}
try {
    Log "preflight Quick"
    $pf = Join-Path $here "preflight.ps1"
    if (-not (Test-Path $pf)) {
        Write-UbdSyncFailed -Phase "preflight" -Head "" -LogPath $log
        throw "preflight.ps1 missing next to sync-and-index.ps1"
    }
    $ErrorActionPreference = "Continue"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $pf -Mode Quick
    $pfCode = $LASTEXITCODE
    $ErrorActionPreference = "Stop"
    if ($pfCode -ne 0) {
        Log "ERROR preflight Quick failed, exit $pfCode"
        Write-UbdSyncFailed -Phase "preflight" -Head "" -LogPath $log
        exit $pfCode
    }
    Log "preflight Quick ok"

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

    $before = $null
    try {
        $before = (git -C $script:UbdConfig rev-parse HEAD 2>$null).Trim()
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($before)) { throw "rev-parse before failed" }
    }
    catch {
        Write-UbdSyncFailed -Phase "git" -Head "" -LogPath $log
        throw "git rev-parse HEAD (before) failed, see $log"
    }
    Log "HEAD before $before"
    Invoke-GitQuiet @("fetch", "--prune", "origin")
    if ($LASTEXITCODE -ne 0) {
        Write-UbdSyncFailed -Phase "git" -Head $before -LogPath $log
        throw "git fetch failed, see $log"
    }
    Invoke-GitQuiet @("pull", "--ff-only")
    if ($LASTEXITCODE -ne 0) {
        Write-UbdSyncFailed -Phase "git" -Head $before -LogPath $log
        throw "git pull --ff-only failed, see $log"
    }
    $after = $null
    try {
        $after = (git -C $script:UbdConfig rev-parse HEAD 2>$null).Trim()
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($after)) { throw "rev-parse after failed" }
    }
    catch {
        Write-UbdSyncFailed -Phase "git" -Head $before -LogPath $log
        throw "git rev-parse HEAD (after) failed, see $log"
    }
    Log "HEAD after  $after"

    $needCi = Test-UbdSyncLayerNeeded -Layer code_index -Head $after
    $needGraph = Test-UbdSyncLayerNeeded -Layer graph -Head $after
    Log "need_ci=$needCi need_graph=$needGraph last_ok_ci=$((Get-UbdSyncState).last_ok_code_index) last_ok_graph=$((Get-UbdSyncState).last_ok_graph)"

    if (-not $needCi -and -not $needGraph) {
        Log "indexes already at HEAD"
        Clear-UbdSyncFailed
        exit 0
    }

    if ($before -ne $after) {
        $names = @(git -C $script:UbdConfig diff --name-only $before $after)
        $maxShow = 20
        Log "changed files: $($names.Count)"
        $names | Select-Object -First $maxShow | ForEach-Object { Log "  $_" }
        if ($names.Count -gt $maxShow) { Log "  … and $($names.Count - $maxShow) more" }
    }
    else {
        Log "git HEAD unchanged; catch-up only"
    }

    $env:DOCKER_HOST = "tcp://127.0.0.1:$($script:DockerApiPort)"
    $bslContainer = $script:UbdBslContainer
    $srcLinux = $script:UbdSrcLinux

    function Copy-UbdGitSrcToAi2 {
        $srcWin = Find-ConfigRoot $script:UbdConfig
        if (-not $srcWin) { throw "Configuration.xml not found under $($script:UbdConfig)" }
        if (-not (Test-Path -LiteralPath (Join-Path $srcWin "Configuration.xml"))) {
            throw "Configuration.xml missing in $srcWin"
        }
        # Linux Docker Engine (DOCKER_HOST tcp) rejects Windows bind mounts
        # (-v C:\...:/src). Same transport as tools/common/Docker-Index.ps1:
        # named volume + docker cp, then alpine copy into /srv/ai2/ubd/src.
        Log "copy $srcWin -> $srcLinux (volume+cp)"
        $vol = "ubd-git-stage-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
        $oldEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $null = & docker.exe volume create $vol 2>&1
            if ($LASTEXITCODE -ne 0) { throw "docker volume create failed" }
            try {
                $cid = (& docker.exe create -v "${vol}:/data" alpine 2>&1 | Out-String).Trim()
                if (-not $cid -or $LASTEXITCODE -ne 0) { throw "docker create failed" }
                try {
                    & docker.exe cp "${srcWin}/." "${cid}:/data/" 2>&1 | ForEach-Object { Log "  $_" }
                    if ($LASTEXITCODE -ne 0) { throw "docker cp into volume failed" }
                }
                finally {
                    $null = & docker.exe rm $cid 2>&1
                }
                & docker.exe run --rm `
                    -v "${vol}:/src:ro" `
                    -v "${srcLinux}:/dest" `
                    alpine sh -c "set -e; mkdir -p /dest; find /dest -mindepth 1 -maxdepth 1 -exec rm -rf {} +; cp -a /src/. /dest/; test -f /dest/Configuration.xml; echo COPY_OK; find /dest -type f | wc -l" 2>&1 |
                    ForEach-Object { Log "  $_" }
                if ($LASTEXITCODE -ne 0) { throw "copy to $srcLinux failed" }
            }
            finally {
                $null = & docker.exe volume rm $vol 2>&1
            }
        }
        finally {
            $ErrorActionPreference = $oldEap
        }
    }

    # Lock lives inside ai2-bsl volume (/repos/SRC/.code-index or CODE_INDEX_HOME).
    function Clear-StaleCodeIndexLock {
        $oldEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $lockInfo = docker exec $bslContainer sh -c @'
set -e
for p in /repos/SRC/.code-index/index.lock /data/code-index-home/index.lock; do
  if [ -f "$p" ]; then echo "LOCK:$p"; cat "$p"; exit 0; fi
done
echo NO_LOCK
'@ 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $lockInfo -or ($lockInfo | Out-String) -match 'NO_LOCK') {
                return $true
            }
            $text = ($lockInfo | Out-String)
            $pidInLock = $null
            if ($text -match '(?m)^(\d{1,9})\s*$') { $pidInLock = [int]$Matches[1] }
            elseif ($text -match '(?m)(\d{2,9})') { $pidInLock = [int]$Matches[1] }
            if ($null -eq $pidInLock) {
                Log "WARN removing unreadable code-index lock in $bslContainer"
                docker exec $bslContainer sh -c 'rm -f /repos/SRC/.code-index/index.lock /data/code-index-home/index.lock' 2>$null | Out-Null
                return $true
            }
            $cmdline = docker exec $bslContainer sh -c "tr '\0' ' ' </proc/$pidInLock/cmdline 2>/dev/null" 2>$null
            if ($LASTEXITCODE -eq 0 -and $cmdline -and ("$cmdline" -match 'bsl-indexer') -and ("$cmdline" -match 'index')) {
                Log "WARN code-index lock held by live indexer PID $pidInLock"
                return $false
            }
            Log "WARN removing stale code-index lock pid=$pidInLock"
            docker exec $bslContainer sh -c 'rm -f /repos/SRC/.code-index/index.lock /data/code-index-home/index.lock' 2>$null | Out-Null
            return $true
        }
        finally {
            $ErrorActionPreference = $oldEap
        }
    }

    # Always refresh ai2 tree when any layer needs work (MCP/BSL read /srv/ai2/ubd/src).
    if ($needCi -or $needGraph) {
        Copy-UbdGitSrcToAi2
    }

    if ($needCi) {
        $ciStatus = ""
        try { $ciStatus = (docker inspect $bslContainer --format "{{.State.Status}}" 2>$null).Trim() } catch { $ciStatus = "" }
        if ($ciStatus -ne "running") {
            Write-UbdSyncFailed -Phase "code_index" -Head $after -LogPath $log
            throw "code-index required but $bslContainer not running (status=$ciStatus)"
        }

        if (-not (Clear-StaleCodeIndexLock)) {
            Write-UbdSyncFailed -Phase "code_index" -Head $after -LogPath $log
            throw "code-index lock busy in $bslContainer"
        }

        Log "code-index incremental index /repos/SRC via $bslContainer"
        $ciLog = Join-Path $logDir "code-index-last.txt"
        $oldEapCi = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            docker exec -e CODE_INDEX_HOME=/data/code-index-home $bslContainer bsl-indexer index --path /repos/SRC 2>&1 |
                Tee-Object -FilePath $ciLog | Out-Null
            if ($LASTEXITCODE -ne 0) {
                Write-UbdSyncFailed -Phase "code_index" -Head $after -LogPath $ciLog
                throw "code-index index failed, exit $LASTEXITCODE"
            }
            docker exec -e CODE_INDEX_HOME=/data/code-index-home $bslContainer bsl-indexer clean --path /repos/SRC 2>&1 |
                Tee-Object -FilePath $ciLog -Append | Out-Null
            if ($LASTEXITCODE -ne 0) {
                Write-UbdSyncFailed -Phase "code_index" -Head $after -LogPath $ciLog
                throw "code-index clean failed, exit $LASTEXITCODE"
            }
            Set-UbdSyncLayerOk -Layer code_index -Commit $after
            Log "code-index delta ok; last_ok_code_index=$after"
        }
        finally {
            $ErrorActionPreference = $oldEapCi
        }
    }
    else {
        Log "skip code-index: already at HEAD"
    }

    if (-not $needGraph) {
        Log "skip graph: already at HEAD"
        Clear-UbdSyncFailed
        exit 0
    }

    $idxStatus = ""
    try { $idxStatus = (docker inspect $script:UbdIndexerRefContainer --format "{{.State.Status}}" 2>$null).Trim() } catch { $idxStatus = "" }
    if ($idxStatus -eq "running") {
        Log "SKIP apply: $($script:UbdIndexerRefContainer) still running (first index). Git already updated."
        exit 0
    }

    # Long apply/FORCE runs: docker run in a background job; parent logs heartbeat every ~180s.
    # No compose plugin / wsl.exe — only docker.exe + DOCKER_HOST (same pattern as KB nightly).
    function Invoke-IndexerDockerRunWithHeartbeat {
        param(
            [Parameter(Mandatory = $true)]
            [ValidateSet("apply_changes", "full_reindex")]
            [string]$Mode,
            [Parameter(Mandatory = $true)][string]$TeeFile,
            [Parameter(Mandatory = $true)][string]$PhaseLabel,
            [int]$HeartbeatSeconds = 180
        )
        $dockerArgs = New-UbdIndexerDockerRunArgs -Mode $Mode
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $job = Start-Job -ArgumentList $dockerArgs, $TeeFile, $script:DockerApiPort -ScriptBlock {
            param($DockerArgs, $TeeFile, $DockerApiPort)
            $env:DOCKER_HOST = "tcp://127.0.0.1:$DockerApiPort"
            $ErrorActionPreference = "Continue"
            & docker.exe @DockerArgs 2>&1 | Tee-Object -FilePath $TeeFile | Out-Null
            return @{ Code = [int]$LASTEXITCODE; Via = "docker-run" }
        }
        try {
            $nextBeat = $HeartbeatSeconds
            while ($job.State -eq "Running") {
                Start-Sleep -Seconds 5
                if ($job.State -ne "Running") { break }
                if ($sw.Elapsed.TotalSeconds -ge $nextBeat) {
                    Log ("heartbeat {0}: still running, elapsed {1:n0}s" -f $PhaseLabel, $sw.Elapsed.TotalSeconds)
                    $nextBeat += $HeartbeatSeconds
                }
            }
            $jobErrs = @()
            $raw = Receive-Job $job -ErrorAction SilentlyContinue -ErrorVariable jobErrs
            foreach ($e in @($jobErrs)) {
                Log ("indexer job error ({0}): {1}" -f $PhaseLabel, $e)
            }
            foreach ($cj in @($job.ChildJobs)) {
                foreach ($e in @($cj.Error)) {
                    Log ("indexer job child error ({0}): {1}" -f $PhaseLabel, $e)
                }
            }
            $via = "docker-run"
            $code = 1
            if ($null -eq $raw) {
                Log ("indexer job ({0}): empty Receive-Job result" -f $PhaseLabel)
            }
            elseif ($null -ne ($raw.Code)) {
                $via = [string]$raw.Via
                $code = [int]$raw.Code
            }
            elseif ($raw -is [array]) {
                $summary = ($raw | ForEach-Object { "$_" } | Select-Object -First 5) -join " | "
                if ($summary) { Log ("indexer job ({0}) non-exit output: {1}" -f $PhaseLabel, $summary) }
                $last = $raw[-1]
                if ($null -ne ($last.Code)) {
                    $via = [string]$last.Via
                    $code = [int]$last.Code
                }
                else {
                    try { $code = [int]$last } catch { $code = 1 }
                }
            }
            else {
                Log ("indexer job ({0}) unexpected result type {1}: {2}" -f $PhaseLabel, $raw.GetType().FullName, $raw)
                try { $code = [int]$raw } catch { $code = 1 }
            }
            Log ("indexer via {0}" -f $via)
            return [int]$code
        }
        finally {
            Remove-Job $job -Force -ErrorAction SilentlyContinue
        }
    }

    $outFile = Join-Path $logDir "apply-last.txt"
    Log "apply_changes.py (docker run)"
    Remove-Item -Force $outFile -ErrorAction SilentlyContinue
    $env:DOCKER_HOST = "tcp://127.0.0.1:$($script:DockerApiPort)"
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $applyFailed = $false
    $code = 0
    try {
        $code = Invoke-IndexerDockerRunWithHeartbeat -Mode "apply_changes" -TeeFile $outFile -PhaseLabel "apply_changes"
        if ($code -ne 0) { $applyFailed = $true }
    }
    catch {
        Log "apply_changes exception $($_.Exception.Message)"
        $applyFailed = $true
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    $applyText = ""
    if (Test-Path $outFile) { $applyText = Get-Content $outFile -Raw -Encoding UTF8 }

    $needFull = $false
    if ($code -eq 3) { $needFull = $true }
    $applyForMatch = $applyText -replace 'OPENROUTER_API_KEY.*?blank string\.', ''
    if ($applyForMatch -match 'METADATA_MAX_PARTIAL|cannot apply|partial update is not|force reindex|no saved state') {
        $needFull = $true
    }

    if ($applyFailed -and -not $needFull) {
        Write-UbdSyncFailed -Phase "apply" -Head $after -LogPath $outFile
        throw "apply_changes failed, see $outFile"
    }

    if (-not $needFull) {
        Set-UbdSyncLayerOk -Layer graph -Commit $after
        Clear-UbdSyncFailed
        Log "partial apply ok; last_ok_graph=$after"
        exit 0
    }

    Log "full reindex METADATA_FORCE_REINDEX=true (docker run)"
    $ErrorActionPreference = "Continue"
    $fullLog = Join-Path $logDir "full-reindex-last.txt"
    Remove-Item -Force $fullLog -ErrorAction SilentlyContinue
    $code = Invoke-IndexerDockerRunWithHeartbeat -Mode "full_reindex" -TeeFile $fullLog -PhaseLabel "full_reindex"
    if ($code -ne 0) {
        Write-UbdSyncFailed -Phase "force" -Head $after -LogPath $fullLog
        throw "full reindex failed, see logs"
    }
    $s = Get-UbdSyncState
    $s.last_force_commit = $after
    Save-UbdSyncState -State $s
    Set-UbdSyncLayerOk -Layer graph -Commit $after
    Clear-UbdSyncFailed
    Log "full reindex ok; last_ok_graph=$after"
    exit 0
}
finally {
    Exit-UbdSyncLock
}
