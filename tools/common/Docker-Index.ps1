# Shared docker helpers for nightly graph/code index (ai2).
# Dot-source from tools/kb, tools/klon_opl, tools/ubd wrappers.

function Invoke-DockerNative {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$DockerArgs,
        [string]$TeeFile = "",
        [switch]$AppendTee
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        # Capture exit BEFORE Tee-Object (pipeline resets LASTEXITCODE).
        $output = & docker @DockerArgs 2>&1
        $code = [int]$LASTEXITCODE
        if ($TeeFile) {
            if ($AppendTee) {
                $output | Tee-Object -FilePath $TeeFile -Append | Out-Null
            }
            else {
                $output | Tee-Object -FilePath $TeeFile | Out-Null
            }
        }
        return $code
    }
    finally {
        $ErrorActionPreference = $prev
    }
}

function Copy-WinTreeToLinuxPath {
    param(
        [Parameter(Mandatory = $true)][string]$StagingWin,
        [Parameter(Mandatory = $true)][string]$DestLinux,
        [string]$VolumePrefix = "ai2-stage"
    )
    if (-not (Test-Path -LiteralPath (Join-Path $StagingWin "Configuration.xml"))) {
        throw "Staging missing Configuration.xml: $StagingWin"
    }

    Write-Host "=== copy $StagingWin -> $DestLinux ==="
    $vol = "$VolumePrefix-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
    $null = Invoke-DockerNative -DockerArgs @("volume", "create", $vol)
    try {
        $cid = (& docker create -v "${vol}:/data" alpine 2>&1 | Out-String).Trim()
        if (-not $cid -or $LASTEXITCODE -ne 0) { throw "docker create failed" }
        try {
            $cpCode = Invoke-DockerNative -DockerArgs @("cp", "$StagingWin/.", "${cid}:/data/")
            if ($cpCode -ne 0) { throw "docker cp into volume failed" }
        }
        finally {
            $null = Invoke-DockerNative -DockerArgs @("rm", $cid)
        }

        $copyCode = Invoke-DockerNative -DockerArgs @(
            "run", "--rm",
            "-v", "${vol}:/src:ro",
            "-v", "${DestLinux}:/dest",
            "alpine", "sh", "-c",
            "set -e; mkdir -p /dest; find /dest -mindepth 1 -maxdepth 1 -exec rm -rf {} +; cp -a /src/. /dest/; test -f /dest/Configuration.xml; test -f /dest/ConfigDumpInfo.xml; echo COPY_OK; find /dest -type f | wc -l"
        )
        if ($copyCode -ne 0) { throw "copy into $DestLinux failed" }
    }
    finally {
        $null = Invoke-DockerNative -DockerArgs @("volume", "rm", $vol)
    }
}

function Invoke-Ai2BslCodeIndex {
    param(
        [Parameter(Mandatory = $true)][string]$Container,
        [string]$RepoPath = "/repos/SRC",
        [string]$LogDir = "",
        [string]$Stamp = ""
    )
    $null = Invoke-DockerNative -DockerArgs @("inspect", $Container)
    if ($LASTEXITCODE -ne 0) {
        throw "BSL container missing: $Container"
    }
    $status = (& docker inspect --format "{{.State.Status}}" $Container 2>$null)
    if ($status -ne "running") {
        $null = Invoke-DockerNative -DockerArgs @("start", $Container)
        Start-Sleep -Seconds 2
        $status = (& docker inspect --format "{{.State.Status}}" $Container 2>$null)
        if ($status -ne "running") {
            throw "BSL container not running: $Container (status=$status)"
        }
    }

    $log = $null
    if ($LogDir) {
        if (-not $Stamp) { $Stamp = Get-Date -Format "yyyyMMdd-HHmmss" }
        $log = Join-Path $LogDir "code-index-$Stamp.log"
    }

    # Drop stale lock if holder PID is dead / not a live bsl-indexer index.
    $lockScript = @'
for p in REPO/.code-index/index.lock /data/code-index-home/index.lock; do
  [ -f "$p" ] || continue
  pid=$(tr -cd '0-9' <"$p" | head -c 9)
  if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
    cmd=$(tr '\0' ' ' </proc/$pid/cmdline 2>/dev/null || true)
    case "$cmd" in
      *bsl-indexer*index*) echo LOCK_BUSY; exit 0 ;;
    esac
  fi
  rm -f "$p"
  echo LOCK_CLEARED
done
echo NO_LOCK
'@ -replace 'REPO', $RepoPath
    $lockOut = & docker exec $Container sh -c $lockScript 2>&1
    Write-Host "lock-check: $($lockOut | Out-String)".Trim()
    if ("$lockOut" -match 'LOCK_BUSY') {
        throw "code-index lock busy in $Container"
    }

    Write-Host "=== code-index ($Container) start $(Get-Date -Format o) ==="
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $indexArgs = @(
        "exec", "-e", "CODE_INDEX_HOME=/data/code-index-home",
        $Container, "bsl-indexer", "index", "--path", $RepoPath
    )
    $code = Invoke-DockerNative -DockerArgs $indexArgs -TeeFile $(if ($log) { $log } else { "" })
    if ($code -ne 0) {
        throw "code-index index failed exit=$code container=$Container"
    }

    $cleanArgs = @(
        "exec", "-e", "CODE_INDEX_HOME=/data/code-index-home",
        $Container, "bsl-indexer", "clean", "--path", $RepoPath
    )
    if ($log) {
        $code = Invoke-DockerNative -DockerArgs $cleanArgs -TeeFile $log -AppendTee
    }
    else {
        $code = Invoke-DockerNative -DockerArgs $cleanArgs
    }
    $sw.Stop()
    Write-Host "code-index exit=$code elapsed_sec=$([math]::Round($sw.Elapsed.TotalSeconds, 1))"
    if ($code -ne 0) {
        throw "code-index clean failed exit=$code container=$Container"
    }
}
