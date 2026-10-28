#Requires -Version 5.1
# These tests mock Defender, never change the host's protection settings.
$env:PRELOOP_DISABLE_TELEMETRY = 'true'

BeforeAll {
  $script:originalEnvironment = @{}
  foreach ($name in @('GITHUB_ACTIONS', 'RUNNER_ENVIRONMENT', 'OS', 'PROCESSOR_ARCHITECTURE')) {
    $script:originalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
  }
  . "$PSScriptRoot/../prepare-windows-defender.ps1"
}

AfterAll {
  foreach ($name in $script:originalEnvironment.Keys) {
    [Environment]::SetEnvironmentVariable($name, $script:originalEnvironment[$name], 'Process')
  }
}

Describe 'Windows release Defender validation' {
  BeforeEach {
    $env:PROCESSOR_ARCHITECTURE = 'AMD64'
    $script:status = [pscustomobject]@{
      AMServiceEnabled = $true; AntivirusEnabled = $true; RealTimeProtectionEnabled = $true
      BehaviorMonitorEnabled = $true; IoavProtectionEnabled = $true; OnAccessProtectionEnabled = $true
      AMRunningMode = 'Normal'; DefenderSignaturesOutOfDate = $false; AntivirusSignatureAge = 0
      AMEngineVersion = 'test-engine'; AMProductVersion = 'test-platform'; AntivirusSignatureVersion = 'test-signatures'
    }
    $script:preferences = [pscustomobject]@{
      DisableRealtimeMonitoring = $false; DisableBehaviorMonitoring = $false
      DisableIOAVProtection = $false; DisableScriptScanning = $false; DisableBlockAtFirstSeen = $false
      MAPSReporting = 2; SubmitSamplesConsent = 1
      ExclusionPath = @(); ExclusionExtension = @(); ExclusionProcess = @(); ExclusionIpAddress = @()
    }
    $script:fixtureDir = Join-Path $TestDrive 'artifacts'
    New-Item -ItemType Directory -Force -Path $fixtureDir | Out-Null
    foreach ($arch in @('amd64', 'arm64')) {
      Set-Content -LiteralPath (Join-Path $fixtureDir "preloop-windows-$arch.exe") -Value "synthetic-$arch"
    }
    $script:resultFile = Join-Path $TestDrive 'reports/result.json'
    Mock Get-MpComputerStatus { $script:status }
    Mock Get-MpPreference { $script:preferences }
    Mock Get-WinEvent { [pscustomobject]@{ IsEnabled = $true } }
    Mock Update-MpSignature { }
    Mock Get-AuthenticodeSignature { [pscustomobject]@{ Status = 'NotSigned'; SignerCertificate = $null } }
    Mock Get-DefenderValidationEvents { @() }
    Mock Get-MpThreatDetection { @() }
    Mock Invoke-DefenderFileScan { }
    Mock Invoke-WindowsCLIProbe { }
  }

  It 'scans both exact artifacts and executes AMD64 probes with telemetry disabled' {
    Invoke-WindowsDefenderValidation $fixtureDir $resultFile -ObservationSeconds 0
    $report = Get-Content -Raw $resultFile | ConvertFrom-Json
    $report.result | Should -Be 'passed'
    $report.artifacts.Count | Should -Be 2
    $report.artifacts[0].sha256 | Should -Be (Get-FileHash (Join-Path $fixtureDir 'preloop-windows-amd64.exe')).Hash
    $report.probes | Should -Contain 'discover'
    $env:PRELOOP_DISABLE_TELEMETRY | Should -Be 'true'
    Should -Invoke Invoke-DefenderFileScan -Times 2 -Exactly
    Should -Invoke Invoke-WindowsCLIProbe -Times 3 -Exactly -ParameterFilter { $FilePath -like '*amd64.exe' }
    Should -Invoke Update-MpSignature -Times 1 -Exactly
  }

  It 'fails before execution if real-time protection is disabled' {
    $script:status.RealTimeProtectionEnabled = $false
    { Invoke-WindowsDefenderValidation $fixtureDir $resultFile -ObservationSeconds 0 } | Should -Throw '*must already be enabled*'
    (Get-Content -Raw $resultFile | ConvertFrom-Json).result | Should -Be 'failed'
    Should -Invoke Invoke-WindowsCLIProbe -Times 0 -Exactly
  }

  It 'rejects stale signatures after update' {
    $script:status.AntivirusSignatureAge = 2
    { Invoke-WindowsDefenderValidation $fixtureDir $resultFile -ObservationSeconds 0 } | Should -Throw '*stale*'
  }

  It 'rejects exclusions and disabled cloud protection' {
    $script:preferences.ExclusionPath = @('C:\example')
    { Assert-DefenderCoverage } | Should -Throw '*exclusions*'
    $script:preferences.ExclusionPath = @()
    $script:preferences.MAPSReporting = 0
    { Assert-DefenderCoverage } | Should -Throw '*cloud protection*'
  }

  It 'rejects disabled event logging' {
    Mock Get-WinEvent { [pscustomobject]@{ IsEnabled = $false } }
    { Assert-DefenderCoverage } | Should -Throw '*event logging*'
  }

  It 'fails on unavailable signature updates' {
    Mock Update-MpSignature { throw 'signature transport failed' }
    { Invoke-WindowsDefenderValidation $fixtureDir $resultFile -ObservationSeconds 0 } | Should -Throw '*signature transport failed*'
    Should -Invoke Invoke-DefenderFileScan -Times 0 -Exactly
  }

  It 'fails on a missing architecture artifact' {
    Remove-Item (Join-Path $fixtureDir 'preloop-windows-arm64.exe')
    { Invoke-WindowsDefenderValidation $fixtureDir $resultFile -ObservationSeconds 0 } | Should -Throw
    Should -Invoke Invoke-WindowsCLIProbe -Times 0 -Exactly
  }

  It 'rejects invalid Authenticode rather than treating it as unsigned' {
    Mock Get-AuthenticodeSignature { [pscustomobject]@{ Status = 'HashMismatch'; SignerCertificate = $null } }
    { Invoke-WindowsDefenderValidation $fixtureDir $resultFile -ObservationSeconds 0 } | Should -Throw '*Invalid Authenticode*'
  }

  It 'fails and retains an evidence report when scanning fails' {
    Mock Invoke-DefenderFileScan { throw 'scanner failed' }
    { Invoke-WindowsDefenderValidation $fixtureDir $resultFile -ObservationSeconds 0 } | Should -Throw '*scanner failed*'
    (Get-Content -Raw $resultFile | ConvertFrom-Json).error | Should -Be 'scanner failed'
    Should -Invoke Invoke-WindowsCLIProbe -Times 0 -Exactly
  }

  It 'fails on a Defender event despite a successful scanner exit' {
    Mock Get-DefenderValidationEvents { @([pscustomobject]@{ Id = 1116; Message = 'synthetic detection' }) }
    { Invoke-WindowsDefenderValidation $fixtureDir $resultFile -ObservationSeconds 0 } | Should -Throw '*recorded a detection*'
    (Get-Content -Raw $resultFile | ConvertFrom-Json).events[0].Id | Should -Be 1116
  }

  It 'fails if threat-history access is unavailable' {
    Mock Get-MpThreatDetection { throw 'threat history unavailable' }
    { Invoke-WindowsDefenderValidation $fixtureDir $resultFile -ObservationSeconds 0 } | Should -Throw '*threat history unavailable*'
  }

  It 'fails on delayed quarantine or changed bytes' {
    Mock Invoke-WindowsCLIProbe { Remove-Item (Join-Path $fixtureDir 'preloop-windows-amd64.exe') -ErrorAction SilentlyContinue }
    { Invoke-WindowsDefenderValidation $fixtureDir $resultFile -ObservationSeconds 0 } | Should -Throw '*disappeared*'
  }

  It 'rejects changed bytes even when both artifacts still exist' {
    Mock Invoke-WindowsCLIProbe { Set-Content (Join-Path $fixtureDir 'preloop-windows-amd64.exe') 'different bytes' }
    { Invoke-WindowsDefenderValidation $fixtureDir $resultFile -ObservationSeconds 0 } | Should -Throw '*changed during validation*'
  }

  It 'checks again after the complete observation interval' {
    $script:eventCalls = 0
    Mock Get-DefenderValidationEvents {
      $script:eventCalls++
      if ($script:eventCalls -gt 1) { return @([pscustomobject]@{ Id = 1117; Message = 'late remediation' }) }
      return @()
    }
    { Invoke-WindowsDefenderValidation $fixtureDir $resultFile -ObservationSeconds 1 } | Should -Throw '*delayed observation*'
  }
}

Describe 'Native process exit status' {
  BeforeAll {
    Add-Type -TypeDefinition @'
using System;
using System.Diagnostics;
using System.IO;
public sealed class ExitedDuringTimeoutProcess : IDisposable {
    private int waits;
    public ProcessStartInfo StartInfo { get; set; }
    public StringReader StandardOutput = new StringReader("completed at timeout boundary");
    public StringReader StandardError = new StringReader("");
    public int ExitCode { get { return 0; } }
    public bool Start() { return true; }
    public bool WaitForExit(int milliseconds) { return ++waits > 1; }
    public void Kill() { throw new InvalidOperationException("Process has exited"); }
    public void Dispose() { StandardOutput.Dispose(); StandardError.Dispose(); }
}
'@
  }

  It 'retains timeout diagnostics when the child exits before Kill' {
    Mock New-Object { [ExitedDuringTimeoutProcess]::new() } -ParameterFilter { $TypeName -eq 'System.Diagnostics.Process' }
    $log = Join-Path $TestDrive 'timeout-race.log'
    { Invoke-ValidationProcess -FilePath 'synthetic-child' -CommandLine 'test' -LogPath $log -TimeoutSeconds 2 } | Should -Throw '*timed out*'
    (Get-Content -Raw $log) | Should -Match 'completed at timeout boundary'
  }

  It 'captures a successful Windows PowerShell process and its output' -Skip:($env:OS -ne 'Windows_NT') {
    $executable = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
    $log = Join-Path $TestDrive 'success.log'
    Invoke-ValidationProcess -FilePath $executable -CommandLine '-NoProfile -Command "Write-Output ''synthetic output''; exit 0"' -LogPath $log -TimeoutSeconds 30
    (Get-Content -Raw $log) | Should -Match 'synthetic output'
  }

  It 'rejects a failing Windows PowerShell process rather than losing ExitCode' -Skip:($env:OS -ne 'Windows_NT') {
    $executable = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
    $log = Join-Path $TestDrive 'failure.log'
    { Invoke-ValidationProcess -FilePath $executable -CommandLine '-NoProfile -Command "exit 17"' -LogPath $log -TimeoutSeconds 30 } | Should -Throw '*exit 17*'
  }

  It 'stops a timed-out child and retains its output' -Skip:($env:OS -ne 'Windows_NT') {
    $executable = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
    $log = Join-Path $TestDrive 'timeout.log'
    { Invoke-ValidationProcess -FilePath $executable -CommandLine '-NoProfile -Command "Write-Output $PID; Start-Sleep -Seconds 60"' -LogPath $log -TimeoutSeconds 2 } | Should -Throw '*timed out*'
    $childProcessID = [int](Get-Content -Raw $log).Trim()
    $childProcessID | Should -BeGreaterThan 0
    Get-Process -Id $childProcessID -ErrorAction SilentlyContinue | Should -BeNullOrEmpty
  }
}

Describe 'Threat history observation timestamps' {
  It 'includes recent western/eastern timestamps and excludes older eastern timestamps' {
    Mock Get-MpThreatDetection {
      @(
        [pscustomobject]@{ ThreatID = 1; InitialDetectionTime = [datetimeoffset]'2026-09-20T05:01:00-07:00'; LastThreatStatusChangeTime = $null },
        [pscustomobject]@{ ThreatID = 2; InitialDetectionTime = [datetimeoffset]'2026-09-20T14:01:00+02:00'; LastThreatStatusChangeTime = $null },
        [pscustomobject]@{ ThreatID = 3; InitialDetectionTime = [datetimeoffset]'2026-09-20T13:59:00+02:00'; LastThreatStatusChangeTime = $null },
        [pscustomobject]@{ ThreatID = 4; InitialDetectionTime = [datetimeoffset]'2026-09-19T12:00:00Z'; LastThreatStatusChangeTime = [datetimeoffset]'2026-09-20T05:01:00-07:00' }
      )
    }
    $recent = @(Get-RecentDefenderThreatDetections -Since ([datetimeoffset]'2026-09-20T12:00:00Z').UtcDateTime)
    $recent.Count | Should -Be 3
    $recent.ThreatID | Should -Contain 1
    $recent.ThreatID | Should -Contain 2
    $recent.ThreatID | Should -Contain 4
    $recent.ThreatID | Should -Not -Contain 3
  }

  It 'normalizes the local DateTime values returned by CIM' {
    Mock Get-MpThreatDetection {
      @(
        [pscustomobject]@{ ThreatID = 1; InitialDetectionTime = ([datetime]'2026-09-20T12:00:00Z').ToLocalTime(); LastThreatStatusChangeTime = $null },
        [pscustomobject]@{ ThreatID = 2; InitialDetectionTime = ([datetime]'2026-09-20T11:59:00Z').ToLocalTime(); LastThreatStatusChangeTime = $null }
      )
    }
    $recent = @(Get-RecentDefenderThreatDetections -Since ([datetimeoffset]'2026-09-20T12:00:00Z').UtcDateTime)
    $recent.Count | Should -Be 1
    $recent[0].ThreatID | Should -Be 1
  }
}

Describe 'Disposable runner protection setup' {
  BeforeEach {
    $env:GITHUB_ACTIONS = 'true'
    $env:RUNNER_ENVIRONMENT = 'github-hosted'
    $env:OS = 'Windows_NT'
    Mock Start-Service { }
    Mock Restart-Service { }
    Mock Set-MpPreference { }
    Mock Get-MpPreference { [pscustomobject]@{
      ExclusionPath = @('C:\', 'D:\'); ExclusionExtension = @()
      ExclusionProcess = @(); ExclusionIpAddress = @()
    } }
    Mock Remove-MpPreference { }
    Mock Enable-WindowsDefenderEventLog { }
    Mock Update-MpSignature { }
    Mock Start-Sleep { }
    $script:updateAttempts = 0
    Mock Assert-DefenderCoverage { [pscustomobject]@{ AMRunningMode = 'Normal' } }
  }

  It 'refuses a local host before changing settings' {
    $env:GITHUB_ACTIONS = 'false'
    { Initialize-GitHubHostedDefender } | Should -Throw '*restricted*'
    Should -Invoke Set-MpPreference -Times 0 -Exactly
    Should -Invoke Start-Service -Times 0 -Exactly
  }

  It 'refuses self-hosted runners before changing settings' {
    $env:RUNNER_ENVIRONMENT = 'self-hosted'
    { Initialize-GitHubHostedDefender } | Should -Throw '*restricted*'
    Should -Invoke Set-MpPreference -Times 0 -Exactly
  }

  It 'enables protections and removes inherited exclusions before asserting coverage' {
    Initialize-GitHubHostedDefender
    Should -Invoke Set-MpPreference -Times 1 -Exactly -ParameterFilter {
      $DisableRealtimeMonitoring -eq $false -and $DisableBehaviorMonitoring -eq $false -and
      $DisableIOAVProtection -eq $false -and $DisableScriptScanning -eq $false -and
      $DisableBlockAtFirstSeen -eq $false -and $MAPSReporting -eq 'Advanced' -and
      $SubmitSamplesConsent -eq 'SendSafeSamples'
    }
    Should -Invoke Remove-MpPreference -Times 1 -Exactly -ParameterFilter { $ExclusionPath.Count -eq 2 }
    Should -Invoke Assert-DefenderCoverage -Times 1 -Exactly
  }

  It 'retries a transient signature RPC failure and still asserts coverage' {
    Mock Update-MpSignature {
      $script:updateAttempts++
      if ($script:updateAttempts -lt 3) {
        throw [System.Management.Automation.ErrorRecord]::new(
          [System.Exception]::new('The remote procedure call failed.'),
          'HRESULT 0x800706be,Update-MpSignature',
          [System.Management.Automation.ErrorCategory]::NotSpecified, $null)
      }
    }
    Initialize-GitHubHostedDefender
    Should -Invoke Update-MpSignature -Times 3 -Exactly
    Should -Invoke Restart-Service -Times 2 -Exactly -ParameterFilter { $Name -eq 'WinDefend' -and $Force -eq $true }
    Should -Invoke Start-Sleep -Times 1 -Exactly -ParameterFilter { $Seconds -eq 5 }
    Should -Invoke Start-Sleep -Times 1 -Exactly -ParameterFilter { $Seconds -eq 10 }
    Should -Invoke Assert-DefenderCoverage -Times 1 -Exactly
  }

  It 'waits for a concurrent signature installation without restarting Defender' {
    Mock Update-MpSignature {
      $script:updateAttempts++
      if ($script:updateAttempts -lt 3) {
        throw [System.Management.Automation.ErrorRecord]::new(
          [System.Exception]::new('Another installation is in progress.'),
          'HRESULT 0x80070652,Update-MpSignature',
          [System.Management.Automation.ErrorCategory]::NotSpecified, $null)
      }
    }
    Initialize-GitHubHostedDefender
    Should -Invoke Update-MpSignature -Times 3 -Exactly
    Should -Invoke Start-Sleep -Times 2 -Exactly
    Should -Invoke Restart-Service -Times 0 -Exactly
    Should -Invoke Assert-DefenderCoverage -Times 1 -Exactly
  }

  It 'fails closed when a concurrent signature installation never clears' {
    Mock Update-MpSignature {
      throw [System.Management.Automation.ErrorRecord]::new(
        [System.Exception]::new('Another installation is in progress.'),
        'HRESULT 0x80070652,Update-MpSignature',
        [System.Management.Automation.ErrorCategory]::NotSpecified, $null)
    }
    { Initialize-GitHubHostedDefender } | Should -Throw '*Another installation is in progress*'
    Should -Invoke Update-MpSignature -Times 3 -Exactly
    Should -Invoke Start-Sleep -Times 2 -Exactly
    Should -Invoke Restart-Service -Times 0 -Exactly
    Should -Invoke Assert-DefenderCoverage -Times 0 -Exactly
  }

  It 'fails closed when signature RPC failures persist' {
    Mock Update-MpSignature {
      throw [System.Management.Automation.ErrorRecord]::new(
        [System.Exception]::new('The remote procedure call failed.'),
        'HRESULT 0x800706be,Update-MpSignature',
        [System.Management.Automation.ErrorCategory]::NotSpecified, $null)
    }
    { Initialize-GitHubHostedDefender } | Should -Throw '*remote procedure call failed*'
    Should -Invoke Update-MpSignature -Times 3 -Exactly
    Should -Invoke Restart-Service -Times 2 -Exactly -ParameterFilter { $Name -eq 'WinDefend' -and $Force -eq $true }
    Should -Invoke Start-Sleep -Times 2 -Exactly
    Should -Invoke Assert-DefenderCoverage -Times 0 -Exactly
  }

  It 'recovers when the signature RPC failure is followed by service startup' {
    Mock Update-MpSignature {
      $script:updateAttempts++
      $code = switch ($script:updateAttempts) {
        1 { '800706be' }
        2 { '800106ba' }
      }
      if ($code) {
        throw [System.Management.Automation.ErrorRecord]::new(
          [System.Exception]::new('Defender service is starting.'),
          "HRESULT 0x$code,Update-MpSignature",
          [System.Management.Automation.ErrorCategory]::NotSpecified, $null)
      }
    }
    Initialize-GitHubHostedDefender
    Should -Invoke Update-MpSignature -Times 3 -Exactly
    Should -Invoke Restart-Service -Times 1 -Exactly -ParameterFilter { $Name -eq 'WinDefend' -and $Force -eq $true }
    Should -Invoke Start-Service -Times 2 -Exactly -ParameterFilter { $Name -eq 'WinDefend' }
    Should -Invoke Assert-DefenderCoverage -Times 1 -Exactly
  }

  It 'fails closed when the Defender service remains unavailable' {
    Mock Update-MpSignature {
      throw [System.Management.Automation.ErrorRecord]::new(
        [System.Exception]::new('Defender service is unavailable.'),
        'HRESULT 0x800106ba,Update-MpSignature',
        [System.Management.Automation.ErrorCategory]::NotSpecified, $null)
    }
    { Initialize-GitHubHostedDefender } | Should -Throw '*service is unavailable*'
    Should -Invoke Update-MpSignature -Times 3 -Exactly
    Should -Invoke Start-Service -Times 3 -Exactly -ParameterFilter { $Name -eq 'WinDefend' }
    Should -Invoke Start-Sleep -Times 2 -Exactly
    Should -Invoke Assert-DefenderCoverage -Times 0 -Exactly
  }

  It 'retries the signature update when the hosted image will not stop WinDefend' {
    Mock Update-MpSignature {
      $script:updateAttempts++
      if ($script:updateAttempts -lt 3) {
        throw [System.Management.Automation.ErrorRecord]::new(
          [System.Exception]::new('The remote procedure call failed.'),
          'HRESULT 0x800706be,Update-MpSignature',
          [System.Management.Automation.ErrorCategory]::NotSpecified, $null)
      }
    }
    Mock Restart-Service {
      throw [System.Management.Automation.ErrorRecord]::new(
        [System.Exception]::new("Service 'WinDefend (WinDefend)' cannot be stopped."),
        'CouldNotStopService,Microsoft.PowerShell.Commands.RestartServiceCommand',
        [System.Management.Automation.ErrorCategory]::CloseError, $null)
    }
    Initialize-GitHubHostedDefender
    Should -Invoke Update-MpSignature -Times 3 -Exactly
    Should -Invoke Restart-Service -Times 2 -Exactly
    Should -Invoke Assert-DefenderCoverage -Times 1 -Exactly
  }

  It 'fails closed when Defender restart is refused by policy after an RPC failure' {
    Mock Update-MpSignature {
      throw [System.Management.Automation.ErrorRecord]::new(
        [System.Exception]::new('The remote procedure call failed.'),
        'HRESULT 0x800706be,Update-MpSignature',
        [System.Management.Automation.ErrorCategory]::NotSpecified, $null)
    }
    Mock Restart-Service { throw 'service disabled by policy' }
    { Initialize-GitHubHostedDefender } | Should -Throw '*disabled by policy*'
    Should -Invoke Update-MpSignature -Times 1 -Exactly
    Should -Invoke Restart-Service -Times 1 -Exactly
    Should -Invoke Start-Sleep -Times 0 -Exactly
    Should -Invoke Assert-DefenderCoverage -Times 0 -Exactly
  }

  It 'does not retry unrelated signature update failures' {
    Mock Update-MpSignature { throw 'signature transport failed' }
    { Initialize-GitHubHostedDefender } | Should -Throw '*signature transport failed*'
    Should -Invoke Update-MpSignature -Times 1 -Exactly
    Should -Invoke Start-Sleep -Times 0 -Exactly
    Should -Invoke Assert-DefenderCoverage -Times 0 -Exactly
  }

  It 'still rejects ineffective protection after a recovered RPC failure' {
    Mock Update-MpSignature {
      $script:updateAttempts++
      if ($script:updateAttempts -eq 1) {
        throw [System.Management.Automation.ErrorRecord]::new(
          [System.Exception]::new('The remote procedure call failed.'),
          'HRESULT 0x800706be,Update-MpSignature',
          [System.Management.Automation.ErrorCategory]::NotSpecified, $null)
      }
    }
    Mock Assert-DefenderCoverage { throw 'passive mode' }
    { Initialize-GitHubHostedDefender } | Should -Throw '*passive mode*'
    Should -Invoke Update-MpSignature -Times 2 -Exactly
    Should -Invoke Assert-DefenderCoverage -Times 1 -Exactly
  }

  It 'fails if policy prevents protection changes' {
    Mock Set-MpPreference { throw 'policy blocks protection setup' }
    { Initialize-GitHubHostedDefender } | Should -Throw '*policy blocks*'
    Should -Invoke Assert-DefenderCoverage -Times 0 -Exactly
  }

  It 'fails if effective protection still does not satisfy validation' {
    Mock Assert-DefenderCoverage { throw 'passive mode' }
    { Initialize-GitHubHostedDefender } | Should -Throw '*passive mode*'
  }
}
