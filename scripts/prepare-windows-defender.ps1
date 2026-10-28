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

function Update-GitHubHostedDefenderSignature {
  # Defender can restart while protection is enabled on the hosted image.
  # Retry only RPC failure, service-unavailable, or another-install-in-progress
  # HRESULTs during startup; persistent errors and policy failures still fail.
  for ($attempt = 1; $attempt -le 3; $attempt++) {
    try {
      Update-MpSignature -ErrorAction Stop
      return
    } catch {
      if ($_.FullyQualifiedErrorId -notmatch '^HRESULT 0x(?:800706be|800106ba|80070652)(?:,|$)' -or $attempt -eq 3) {
        throw
      }
      Write-Warning "Defender signature update service is not ready; retrying ($attempt/3)."
      # 0x800706be is a wedged RPC server inside an already-running WinDefend.
      # Hosted images refuse to stop that service, so Restart-Service throws
      # CouldNotStopService and must not fail the job. 0x800106ba means the
      # service is still starting.
      if ($_.FullyQualifiedErrorId -match '^HRESULT 0x800706be(?:,|$)') {
        try {
          Restart-Service -Name WinDefend -Force -ErrorAction Stop
        } catch {
          $stopped = $_.FullyQualifiedErrorId -match 'CouldNotStopService' -or
            $_.Exception.Message -match 'cannot be stopped'
          if (-not $stopped) { throw }
          Write-Warning "WinDefend cannot be stopped on this hosted image; retrying the signature update."
        }
      } elseif ($_.FullyQualifiedErrorId -match '^HRESULT 0x800106ba(?:,|$)') {
        Start-Service -Name WinDefend -ErrorAction Stop
      }
      # 0x80070652 means Windows Update already owns the installer. Leave the
      # running service alone and retry after the competing update completes.
      Start-Sleep -Seconds (5 * $attempt)
    }
  }
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
  Update-GitHubHostedDefenderSignature
  Assert-DefenderCoverage | Format-List AMRunningMode, AMEngineVersion, AMProductVersion, AntivirusSignatureVersion, RealTimeProtectionEnabled
}

if ($MyInvocation.InvocationName -ne '.') { Initialize-GitHubHostedDefender }
