#Requires -Version 5.1
# Classify disposable-runner setup only. Product tests and artifact scans must fail.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-RetryableDefenderSetup {
  param([string]$Message, [string]$ErrorId)
  # These conditions describe unavailable scanner infrastructure. Unknown errors
  # (including policy/exclusion failures) are not eligible for automatic retry.
  return $Message -match '^Defender coverage unavailable: (AMServiceEnabled|AntivirusEnabled|RealTimeProtectionEnabled|BehaviorMonitorEnabled|IoavProtectionEnabled|OnAccessProtectionEnabled) is not enabled\.$' -or
    $ErrorId -match '^HRESULT 0x(?:800706be|800106ba|80070652)(?:,|$)'
}

function Invoke-RunnerDefenderSetup {
  try {
    # Setup already narrowly excludes the hosted provjobd false positive before
    # enabling the scanner; never exempt release binaries or weaken coverage.
    & "$PSScriptRoot/../../scripts/prepare-windows-defender.ps1"
    'retryable=false' >> $env:GITHUB_OUTPUT
  } catch {
    $retryable = Test-RetryableDefenderSetup -Message $_.Exception.Message -ErrorId $_.FullyQualifiedErrorId
    "retryable=$($retryable.ToString().ToLowerInvariant())" >> $env:GITHUB_OUTPUT
    if ($retryable) {
      Write-Warning 'Disposable Windows runner could not initialize Defender; one fresh runner may retry.'
    }
    throw
  }
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-RunnerDefenderSetup }
