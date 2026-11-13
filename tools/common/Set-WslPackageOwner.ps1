<#
.SYNOPSIS
  DEPRECATED. Prefer Publish-ReadyPackageViaDocker (tools/common/Docker-Index.ps1).

.DESCRIPTION
  Legacy helper for packages dropped via \\wsl$\... Contour claim_package needs
  ai-contour ownership to rename into accepted/.

  On this stand \\wsl$ is broken (VHD sharing) — KB / klon_opl exporters already
  deliver via Docker API + chown inside the container. This script remains only
  for manual/legacy paths: chown failures become warnings and do NOT fail the
  export (a successful 1C dump must not be discarded because WSL hung).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$WinPath,
    [string]$Owner = "ai-contour:ai-contour",
    [string]$InboxPrefix = "/srv/ai-contour/inbox/"
)

$ErrorActionPreference = "Continue"
$full = [IO.Path]::GetFullPath($WinPath)

Write-Warning "Set-WslPackageOwner is deprecated; use Publish-ReadyPackageViaDocker for inbox delivery."

if ($full -notmatch '(?i)^\\\\wsl\$\\([^\\]+)\\(.+)$') {
    Write-Verbose "Set-WslPackageOwner: not a \\wsl`$ path, skip ($full)"
    return
}

$distro = $Matches[1]
$linux = "/" + (($Matches[2] -replace "\\", "/").TrimStart("/"))
if (-not $linux.StartsWith($InboxPrefix)) {
    Write-Warning "Set-WslPackageOwner refuses path outside ${InboxPrefix}: $linux"
    return
}
if (-not (Test-Path -LiteralPath $full)) {
    Write-Warning "Set-WslPackageOwner: path missing: $full"
    return
}

# -u root: only root can chown Windows-created root:root trees.
& wsl.exe -d $distro -u root -- chown -R $Owner -- $linux
if ($LASTEXITCODE -ne 0) {
    Write-Warning "chown -R $Owner failed for $linux (wsl exit $LASTEXITCODE); package left as-is"
    return
}
Write-Host "package owner set to $Owner : $linux"
