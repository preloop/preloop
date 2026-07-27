#Requires -Version 5.1
<#
.SYNOPSIS
  Install the Preloop CLI on Windows.

.DESCRIPTION
  Downloads the matching preloop-windows-* binary from GitHub Releases into
  %LOCALAPPDATA%\Preloop\bin, unblocks Mark of the Web, and adds the directory
  to the user PATH.

  Recommended install (PowerShell):

    irm https://preloop.ai/install/cli.ps1 | iex

  Or pin a version:

    $env:PRELOOP_VERSION = '1.2.3'
    irm https://preloop.ai/install/cli.ps1 | iex

  Environment overrides:
    PRELOOP_REPO      GitHub repo (default: preloop/preloop)
    PRELOOP_VERSION   Semver without leading v (default: latest)
    INSTALL_DIR       Install directory (default: %LOCALAPPDATA%\Preloop\bin)
    PRELOOP_URL       Control plane URL for subsequent login
    PRELOOP_CONFIRM   If 1/true, skip interactive prompts

.NOTES
  Prefer this script over the bash installer under Git Bash. 32-bit Git Bash
  reports architecture as i686 and previously failed; PowerShell reads the
  real PROCESSOR_ARCHITECTURE.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-PreloopConfirm {
  $value = [string]$env:PRELOOP_CONFIRM
  if ([string]::IsNullOrWhiteSpace($value)) { return $false }
  switch ($value.Trim().ToLowerInvariant()) {
    { $_ -in @('1', 'y', 'yes', 'true', 'on') } { return $true }
    default { return $false }
  }
}

function Get-PreloopArch {
  $arch = $env:PROCESSOR_ARCHITECTURE
  $wow64 = $env:PROCESSOR_ARCHITEW6432
  if (-not [string]::IsNullOrWhiteSpace($wow64)) {
    $arch = $wow64
  }
  switch -Regex ($arch) {
    '^(AMD64|X86_64)$' { return 'amd64' }
    '^(ARM64|AARCH64)$' { return 'arm64' }
    default {
      throw @"
Unsupported Windows architecture: $arch

Preloop ships 64-bit Windows builds only (amd64 / arm64).
Download from https://github.com/preloop/preloop/releases if you need a manual install.
"@
    }
  }
}

function Get-LatestPreloopVersion {
  param([Parameter(Mandatory = $true)][string]$Repo)
  $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest"
  $tag = [string]$release.tag_name
  if ([string]::IsNullOrWhiteSpace($tag)) {
    throw "Could not determine the latest Preloop release from GitHub."
  }
  return $tag.TrimStart('v')
}

function Ensure-UserPathEntry {
  param([Parameter(Mandatory = $true)][string]$Directory)
  $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
  if ([string]::IsNullOrWhiteSpace($userPath)) {
    $userPath = ''
  }
  $parts = @($userPath -split ';' | Where-Object { $_ -ne '' })
  if ($parts -contains $Directory) {
    return $false
  }
  $newPath = if ($userPath.Trim() -eq '') { $Directory } else { "$Directory;$userPath" }
  [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
  $env:Path = "$Directory;$env:Path"
  return $true
}

$repo = if ([string]::IsNullOrWhiteSpace($env:PRELOOP_REPO)) { 'preloop/preloop' } else { $env:PRELOOP_REPO.Trim() }
$version = if ([string]::IsNullOrWhiteSpace($env:PRELOOP_VERSION)) { Get-LatestPreloopVersion -Repo $repo } else { $env:PRELOOP_VERSION.Trim().TrimStart('v') }
$arch = Get-PreloopArch
$installDir = if ([string]::IsNullOrWhiteSpace($env:INSTALL_DIR)) {
  Join-Path $env:LOCALAPPDATA 'Preloop\bin'
} else {
  $env:INSTALL_DIR.Trim()
}

$asset = "preloop-windows-$arch.exe"
$tag = "v$version"
$url = "https://github.com/$repo/releases/download/$tag/$asset"
$target = Join-Path $installDir 'preloop.exe'

Write-Host "Installing Preloop CLI $version ($arch)..."
Write-Host "  source: $url"
Write-Host "  target: $target"

New-Item -ItemType Directory -Force -Path $installDir | Out-Null
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("preloop-install-" + [guid]::NewGuid().ToString('N') + '.exe')
try {
  Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
  # Remove Mark of the Web so SmartScreen/Defender treat a verified download
  # less harshly after the user explicitly installed it.
  Unblock-File -Path $tmp -ErrorAction SilentlyContinue
  Move-Item -Force -Path $tmp -Destination $target
  Unblock-File -Path $target -ErrorAction SilentlyContinue
} finally {
  if (Test-Path -LiteralPath $tmp) {
    Remove-Item -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
  }
}

$pathAdded = Ensure-UserPathEntry -Directory $installDir
Write-Host "Installed preloop $version to $target"
if ($pathAdded) {
  Write-Host "Added $installDir to your user PATH. Open a new terminal for it to take effect."
} else {
  Write-Host "$installDir is already on your user PATH."
}

# Seed client_id for adoption telemetry (random UUID, no user data).
$preloopDir = Join-Path $env:USERPROFILE '.preloop'
$clientIdFile = Join-Path $preloopDir 'client_id'
if (-not (Test-Path -LiteralPath $clientIdFile)) {
  New-Item -ItemType Directory -Force -Path $preloopDir | Out-Null
  [guid]::NewGuid().ToString('D') | Set-Content -LiteralPath $clientIdFile -Encoding ascii
}

Write-Host ""
Write-Host "If Microsoft Defender quarantines preloop.exe, restore it from Protection history,"
Write-Host "verify the SHA256 against the GitHub release, then add an exclusion for:"
Write-Host "  $installDir"
Write-Host "Details: https://github.com/preloop/preloop/blob/main/docs/windows-cli.md"

& $target version

$cloudUrl = 'https://preloop.ai'
$targetUrl = if ([string]::IsNullOrWhiteSpace($env:PRELOOP_URL)) { $cloudUrl } else { $env:PRELOOP_URL.Trim() }
$env:PRELOOP_URL = $targetUrl

if (Test-PreloopConfirm) {
  Write-Host "Preloop instance: $targetUrl (PRELOOP_CONFIRM)"
  Write-Host "Skipped interactive login. Run: preloop login --url $targetUrl"
  exit 0
}

Write-Host ""
Write-Host "The CLI connects your agents to a Preloop control plane."
Write-Host "  Preloop Cloud: $cloudUrl (default)"
Write-Host "  Self-hosted:   e.g. http://localhost:8000"
$custom = Read-Host "Preloop instance URL [$targetUrl]"
if (-not [string]::IsNullOrWhiteSpace($custom)) {
  $targetUrl = $custom.Trim()
  $env:PRELOOP_URL = $targetUrl
}
Write-Host "Preloop instance: $targetUrl"

$auth = Read-Host "Sign in (or sign up) now? [Y/s/n]"
if ([string]::IsNullOrWhiteSpace($auth)) { $auth = 'y' }
switch -Regex ($auth.Trim().ToLowerInvariant()) {
  '^(y|yes|l|login)$' {
    & $target login
  }
  '^(s|signup|register|r)$' {
    & $target signup
  }
  default {
    Write-Host "Skipped authentication. When ready, run: preloop login --url $targetUrl"
  }
}

Write-Host ""
Write-Host "You're done when:"
Write-Host "  [ ] You are authenticated (preloop auth status)"
Write-Host "  [ ] Your agents are discovered and onboarded (preloop agents discover)"
Write-Host "  [ ] You have tested an approval path, if your policies require one"
