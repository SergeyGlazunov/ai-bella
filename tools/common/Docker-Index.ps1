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

function Publish-ReadyPackageViaDocker {
    <#
    .SYNOPSIS
      Copy a Windows ready-<id> package into the Linux Contour inbox via Docker API.

    .DESCRIPTION
      Avoids \\wsl$ (broken on this stand). Creates a temp volume, docker cp the
      package tree, then alpine copies into LinuxInbox as .incoming-* → ready-*
      with chown for ai-contour claim_package.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$ReadyPathWin,
        [Parameter(Mandatory = $true)][string]$LinuxInbox,
        [string]$Owner = "999:988",
        [string]$InboxPrefix = "/srv/ai-contour/inbox/",
        # Test seam: ({ string[] DockerArgs }) -> @{ ExitCode = int; StdOut = string }
        [scriptblock]$DockerCall = $null
    )

    $ready = [IO.Path]::GetFullPath($ReadyPathWin)
    $pkgName = Split-Path -Leaf $ready
    if ($pkgName -notmatch '^ready-') {
        throw "ReadyPathWin must be a ready-* directory: $ready"
    }
    $readyMarker = Join-Path $ready 'READY'
    if (-not (Test-Path -LiteralPath $readyMarker -PathType Leaf)) {
        throw "READY missing: $ready"
    }
    $cfgXml = Join-Path $ready 'src\Configuration.xml'
    if (-not (Test-Path -LiteralPath $cfgXml -PathType Leaf)) {
        throw "src/Configuration.xml missing: $ready"
    }

    $inbox = $LinuxInbox.TrimEnd('/')
    $prefix = $InboxPrefix.TrimEnd('/')
    if (-not ($inbox.StartsWith($prefix + '/') -or $inbox -eq $prefix)) {
        throw "LinuxInbox must be under ${prefix}/ : $LinuxInbox"
    }
    # Mount parent inbox; package lands as /inbox/<ready-*>
    $inboxParent = $inbox

    function Invoke-PublishDocker {
        param([Parameter(Mandatory = $true)][string[]]$DockerArgs)
        if ($null -ne $DockerCall) {
            $res = & $DockerCall $DockerArgs
            if ($res -is [hashtable] -or $res -is [System.Collections.Specialized.OrderedDictionary]) {
                return [pscustomobject]@{
                    ExitCode = [int]$res['ExitCode']
                    StdOut   = [string]$res['StdOut']
                }
            }
            throw "DockerCall must return @{ ExitCode; StdOut }"
        }
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $output = & docker @DockerArgs 2>&1
            $code = [int]$LASTEXITCODE
            $text = ($output | ForEach-Object { "$_" }) -join "`n"
            return [pscustomobject]@{ ExitCode = $code; StdOut = $text }
        }
        finally {
            $ErrorActionPreference = $prev
        }
    }

    Write-Host "=== docker-publish $pkgName -> $inboxParent ==="
    $vol = "contour-pkg-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
    $created = Invoke-PublishDocker -DockerArgs @("volume", "create", $vol)
    if ($created.ExitCode -ne 0) {
        throw "docker volume create failed: $($created.StdOut)"
    }

    try {
        $cidRes = Invoke-PublishDocker -DockerArgs @("create", "-v", "${vol}:/data", "alpine")
        if ($cidRes.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($cidRes.StdOut)) {
            throw "docker create failed: $($cidRes.StdOut)"
        }
        $cid = ($cidRes.StdOut -split "[\r\n]+" | Where-Object { $_ -match '^[a-f0-9]{12,}$' } | Select-Object -First 1)
        if (-not $cid) {
            $cid = ($cidRes.StdOut.Trim() -split "\s+")[-1]
        }
        try {
            $cp = Invoke-PublishDocker -DockerArgs @("cp", "$ready/.", "${cid}:/data/")
            if ($cp.ExitCode -ne 0) {
                throw "docker cp into volume failed: $($cp.StdOut)"
            }
        }
        finally {
            $null = Invoke-PublishDocker -DockerArgs @("rm", $cid)
        }

        # Bind-mount the Contour inbox directory; stage as .incoming then rename.
        $script = @(
            'set -e'
            "NAME=$pkgName"
            "OWNER=$Owner"
            'rm -rf "/inbox/.incoming-$NAME" "/inbox/$NAME"'
            'mkdir -p "/inbox/.incoming-$NAME"'
            'cp -a /src/. "/inbox/.incoming-$NAME/"'
            'test -f "/inbox/.incoming-$NAME/READY"'
            'test -f "/inbox/.incoming-$NAME/src/Configuration.xml"'
            'test -f "/inbox/.incoming-$NAME/manifest.json"'
            "chown -R $Owner `"/inbox/.incoming-`$NAME`""
            'mv "/inbox/.incoming-$NAME" "/inbox/$NAME"'
            'echo DELIVER_OK'
            'find "/inbox/$NAME" -type f | wc -l'
        ) -join "`n"

        $run = Invoke-PublishDocker -DockerArgs @(
            "run", "--rm",
            "-v", "${vol}:/src:ro",
            "-v", "${inboxParent}:/inbox",
            "alpine", "sh", "-c", $script
        )
        if ($run.ExitCode -ne 0 -or ($run.StdOut -notmatch 'DELIVER_OK')) {
            throw "docker deliver to $inboxParent failed exit=$($run.ExitCode): $($run.StdOut)"
        }
        Write-Host $run.StdOut.Trim()
        Write-Host "published to Linux inbox: $inboxParent/$pkgName"
    }
    finally {
        $null = Invoke-PublishDocker -DockerArgs @("volume", "rm", $vol)
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
