#Requires -Version 5.1
# Strengthen protection only on disposable GitHub-hosted Windows runners.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. "$PSScriptRoot/test-windows-defender.ps1"

function Enable-WindowsDefenderEventLog {
  $log = New-Object System.Diagnostics.Eventing.Reader.EventLogConfiguration 'Microsoft-Windows-Windows Defender/Operational'
  try {
    $log.IsEnabled = $true
    $log.SaveChanges()
  } finally { $log.Dispose() }
}

function Initialize-GitHubHostedDefender {
  if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or $env:OS -ne 'Windows_NT') {
    throw 'Protection setup is restricted to disposable GitHub-hosted Windows runners.'
  }
  # Hosted build images disable protection and exclude build drives by default.
  # Reverse those settings before any release executables are downloaded.
  Start-Service -Name WinDefend -ErrorAction Stop
  Set-MpPreference -DisableRealtimeMonitoring $false -DisableBehaviorMonitoring $false `
    -DisableIOAVProtection $false -DisableScriptScanning $false -DisableBlockAtFirstSeen $false `
    -MAPSReporting Advanced -SubmitSamplesConsent SendSafeSamples -ErrorAction Stop
  $preferences = Get-MpPreference -ErrorAction Stop
  foreach ($name in @('ExclusionPath', 'ExclusionExtension', 'ExclusionProcess', 'ExclusionIpAddress')) {
    $values = @($preferences.$name | Where-Object { $_ })
    if ($values.Count -gt 0) {
      $parameters = @{ ErrorAction = 'Stop' }
      $parameters[$name] = $values
      Remove-MpPreference @parameters
    }
  }
  Enable-WindowsDefenderEventLog
  Update-MpSignature -ErrorAction Stop
  Assert-DefenderCoverage | Format-List AMRunningMode, AMEngineVersion, AMProductVersion, AntivirusSignatureVersion, RealTimeProtectionEnabled
}

if ($MyInvocation.InvocationName -ne '.') { Initialize-GitHubHostedDefender }
