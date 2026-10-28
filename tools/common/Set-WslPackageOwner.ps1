<#
.SYNOPSIS
  After publishing via \\wsl$\..., make the package claimable by ai-contour.

.DESCRIPTION
  Contour claim_package uses Path.replace (cross-directory rename). Linux
  requires write permission on the directory being moved (to update "..").
  Drops created through \\wsl$ are typically root:root mode 755 — ai-contour
  can write the inbox parent but cannot rename the package dir into accepted/.

  No-op when -WinPath is not under \\wsl$\. Refuses paths outside
  /srv/ai-contour/inbox/ (or the Linux path derived from that UNC).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$WinPath,
    [string]$Owner = "ai-contour:ai-contour",
    [string]$InboxPrefix = "/srv/ai-contour/inbox/"
)

$ErrorActionPreference = "Stop"
$full = [IO.Path]::GetFullPath($WinPath)

if ($full -notmatch '(?i)^\\\\wsl\$\\([^\\]+)\\(.+)$') {
    Write-Verbose "Set-WslPackageOwner: not a \\\\wsl\$ path, skip ($full)"
    return
}

$distro = $Matches[1]
$linux = "/" + (($Matches[2] -replace "\\", "/").TrimStart("/"))
if (-not $linux.StartsWith($InboxPrefix)) {
    throw "Set-WslPackageOwner refuses path outside ${InboxPrefix}: $linux"
}
if (-not (Test-Path -LiteralPath $full)) {
    throw "Set-WslPackageOwner: path missing: $full"
}

# -u root: only root can chown Windows-created root:root trees.
& wsl.exe -d $distro -u root -- chown -R $Owner -- $linux
if ($LASTEXITCODE -ne 0) {
    throw "chown -R $Owner failed for $linux (wsl exit $LASTEXITCODE)"
}
Write-Host "package owner set to $Owner : $linux"
