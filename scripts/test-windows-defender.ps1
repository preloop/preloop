#Requires -Version 5.1
<##
.SYNOPSIS
  Validate finalized Windows release binaries with Microsoft Defender enabled.
##>
[CmdletBinding()]
param(
  [string]$ArtifactDirectory,
  [string]$ReportPath,
  [ValidateRange(0, 1800)][int]$ObservationSeconds = 600
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-DefenderCoverage {
  $status = Get-MpComputerStatus -ErrorAction Stop
  $preferences = Get-MpPreference -ErrorAction Stop
  foreach ($name in @('AMServiceEnabled', 'AntivirusEnabled', 'RealTimeProtectionEnabled', 'BehaviorMonitorEnabled', 'IoavProtectionEnabled', 'OnAccessProtectionEnabled')) {
    if ($status.$name -ne $true) { throw "Defender coverage unavailable: $name is not enabled." }
  }
  if ($status.AMRunningMode -ne 'Normal') { throw 'Defender must be in Normal mode.' }
  if ($status.DefenderSignaturesOutOfDate -or $status.AntivirusSignatureAge -gt 1) {
    throw 'Defender antivirus signatures are stale.'
  }
  foreach ($name in @('DisableRealtimeMonitoring', 'DisableBehaviorMonitoring', 'DisableIOAVProtection', 'DisableScriptScanning', 'DisableBlockAtFirstSeen')) {
    if ($preferences.$name -ne $false) { throw "Defender protection disabled: $name." }
  }
  if ([int]$preferences.MAPSReporting -lt 1 -or [int]$preferences.SubmitSamplesConsent -notin @(1, 3)) {
    throw 'Defender cloud protection and automatic sample submission are required.'
  }
  foreach ($name in @('ExclusionPath', 'ExclusionExtension', 'ExclusionProcess', 'ExclusionIpAddress')) {
    if (@($preferences.$name | Where-Object { $_ }).Count -gt 0) {
      throw "Defender exclusions prevent an unqualified validation: $name."
    }
  }
  $eventLog = Get-WinEvent -ListLog 'Microsoft-Windows-Windows Defender/Operational' -ErrorAction Stop
  if (-not $eventLog.IsEnabled) { throw 'Defender Operational event logging is unavailable.' }
  return $status
}

function Get-DefenderValidationEvents {
  param([datetime]$Since)
  try {
    return @(Get-WinEvent -FilterHashtable @{
      LogName = 'Microsoft-Windows-Windows Defender/Operational'
      StartTime = $Since
      Id = @(1116, 1117, 5001)
    } -ErrorAction Stop | Select-Object Id, TimeCreated, RecordId, Message)
  } catch {
    if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') { return @() }
    throw
  }
}

function Get-RecentDefenderThreatDetections {
  param([datetime]$Since)
  $sinceUtc = $Since.ToUniversalTime()
  return @(Get-MpThreatDetection -ErrorAction Stop | Where-Object {
    # CIM timestamps are local DateTime values. DateTime comparisons alone do
    # not account for Kind; normalize both timestamps to the UTC report clock.
    ($_.InitialDetectionTime -and ([datetimeoffset]$_.InitialDetectionTime).UtcDateTime -ge $sinceUtc) -or
      ($_.LastThreatStatusChangeTime -and ([datetimeoffset]$_.LastThreatStatusChangeTime).UtcDateTime -ge $sinceUtc)
  })
}

function Invoke-ValidationProcess {
  param([string]$FilePath, [string]$CommandLine, [string]$LogPath, [int]$TimeoutSeconds)
  $info = New-Object System.Diagnostics.ProcessStartInfo
  $info.FileName = $FilePath
  $info.Arguments = $CommandLine
  $info.UseShellExecute = $false
  $info.RedirectStandardOutput = $true
  $info.RedirectStandardError = $true
  $process = New-Object System.Diagnostics.Process
  $process.StartInfo = $info
  try {
    if (-not $process.Start()) { throw "Could not start $FilePath." }
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    $timedOut = -not $process.WaitForExit($TimeoutSeconds * 1000)
    if ($timedOut) {
      # Cancel only our timed-out child, never the Defender service.
      try { $process.Kill() } catch [System.InvalidOperationException] {
        # The child can exit between WaitForExit and Kill. Retain the timeout
        # outcome and its logs rather than masking it with an already-exited error.
      }
      if (-not $process.WaitForExit(5000)) { throw 'Timed-out validation process did not stop.' }
    }
    # Direct Process ownership reliably retains ExitCode on Windows PowerShell 5.1.
    if (-not $stdout.Wait(5000) -or -not $stderr.Wait(5000)) { throw 'Validation process output did not close.' }
    $stdout.GetAwaiter().GetResult() | Set-Content -LiteralPath $LogPath -Encoding UTF8
    $stderr.GetAwaiter().GetResult() | Set-Content -LiteralPath "$LogPath.stderr" -Encoding UTF8
    if ($timedOut) { throw "Validation process timed out: $FilePath $CommandLine." }
    if ($process.ExitCode -ne 0) {
      throw "Validation process failed: $FilePath $CommandLine (exit $($process.ExitCode)); see $LogPath."
    }
  } finally {
    $process.Dispose()
  }
}

function Invoke-DefenderFileScan {
  param([string]$FilePath, [string]$LogPath)
  $platform = Join-Path $env:ProgramData 'Microsoft\Windows Defender\Platform'
  $scanner = Get-ChildItem -LiteralPath $platform -Directory -ErrorAction Stop |
    Sort-Object Name -Descending |
    ForEach-Object { Join-Path $_.FullName 'MpCmdRun.exe' } |
    Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
    Select-Object -First 1
  if (-not $scanner) { throw 'Cannot locate the current Defender command-line scanner.' }
  Invoke-ValidationProcess -FilePath $scanner -CommandLine ('-Scan -ScanType 3 -File "{0}"' -f $FilePath) -LogPath $LogPath -TimeoutSeconds 180
}

function Invoke-WindowsCLIProbe {
  param([string]$FilePath, [string[]]$Arguments, [string]$LogPath)
  # All arguments are fixed command names/flags; no user-controlled shell text.
  Invoke-ValidationProcess -FilePath $FilePath -CommandLine ($Arguments -join ' ') -LogPath $LogPath -TimeoutSeconds 60
}

function Assert-ValidationArtifacts {
  param([object[]]$Artifacts)
  foreach ($artifact in $Artifacts) {
    if (-not (Test-Path -LiteralPath $artifact.path -PathType Leaf)) {
      throw "Release artifact disappeared: $($artifact.name)."
    }
    if ((Get-FileHash -LiteralPath $artifact.path -Algorithm SHA256).Hash -ne $artifact.sha256) {
      throw "Release artifact changed during validation: $($artifact.name)."
    }
  }
}

function Invoke-WindowsDefenderValidation {
  param(
    [Parameter(Mandatory = $true)][string]$ArtifactDirectory,
    [Parameter(Mandatory = $true)][string]$ReportPath,
    [ValidateRange(0, 1800)][int]$ObservationSeconds = 600
  )
  $env:PRELOOP_DISABLE_TELEMETRY = 'true'
  $started = [datetime]::UtcNow
  $report = [ordered]@{
    started_utc = $started.ToString('o'); finished_utc = $null; result = 'failed'
    observation_seconds = $ObservationSeconds; error = $null
    host_os = [Environment]::OSVersion.VersionString
    coverage = 'Defender on this AMD64 Windows host; ARM64 static scan only. SmartScreen and corporate EDR are not validated.'
    defender_before = $null; defender_after = $null
    artifacts = @(); events = @(); threat_detections = @(); probes = @()
  }
  $ReportPath = [IO.Path]::GetFullPath($ReportPath)
  $reportDirectory = Split-Path -Parent $ReportPath
  New-Item -ItemType Directory -Path $reportDirectory -Force | Out-Null
  try {
    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'Native smoke tests require an AMD64 Windows host.' }
    # Refuse missing protection before updating, and recheck throughout the run.
    # Stale signatures are addressed by the update before the first full assertion.
    $initial = Get-MpComputerStatus -ErrorAction Stop
    $report.defender_before = $initial
    if (-not $initial.AMServiceEnabled -or -not $initial.AntivirusEnabled -or -not $initial.RealTimeProtectionEnabled) {
      throw 'Defender antivirus and real-time protection must already be enabled.'
    }
    Update-MpSignature -ErrorAction Stop
    $report.defender_before = Assert-DefenderCoverage
    foreach ($arch in @('amd64', 'arm64')) {
      $name = "preloop-windows-$arch.exe"
      $path = Join-Path ([IO.Path]::GetFullPath($ArtifactDirectory)) $name
      $item = Get-Item -LiteralPath $path -ErrorAction Stop
      $signature = Get-AuthenticodeSignature -LiteralPath $path -ErrorAction Stop
      $report.artifacts += [ordered]@{
        name = $name; path = $path; architecture = $arch; bytes = $item.Length
        sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
        authenticode_status = [string]$signature.Status
        signer_thumbprint = $(if ($signature.SignerCertificate) { $signature.SignerCertificate.Thumbprint } else { $null })
        scan_completed = $false
      }
      # Unsigned pass-through releases remain supported; invalid signatures do not.
      if ([string]$signature.Status -notin @('Valid', 'NotSigned')) {
        throw "Invalid Authenticode signature on $name : $($signature.Status)."
      }
      Invoke-DefenderFileScan -FilePath $path -LogPath (Join-Path $reportDirectory "$arch-scan.log")
      $report.artifacts[-1].scan_completed = $true
    }
    $nativePath = $report.artifacts[0].path
    foreach ($probe in @(
      @{ name = 'version'; arguments = @('version') },
      @{ name = 'help'; arguments = @('--help') },
      @{ name = 'discover'; arguments = @('agents', 'discover', '--no-onboard-prompt') }
    )) {
      Invoke-WindowsCLIProbe -FilePath $nativePath -Arguments $probe.arguments -LogPath (Join-Path $reportDirectory "$($probe.name).log")
      $report.probes += $probe.name
    }
    $deadline = [datetime]::UtcNow.AddSeconds($ObservationSeconds)
    do {
      $report.defender_after = Assert-DefenderCoverage
      $report.events = @(Get-DefenderValidationEvents -Since $started)
      $report.threat_detections = @(Get-RecentDefenderThreatDetections -Since $started)
      if ($report.events.Count -gt 0 -or $report.threat_detections.Count -gt 0) {
        throw 'Defender recorded a detection, remediation, or loss of real-time protection during validation.'
      }
      Assert-ValidationArtifacts -Artifacts $report.artifacts
      $remaining = ($deadline - [datetime]::UtcNow).TotalSeconds
      if ($remaining -gt 0) { Start-Sleep -Seconds ([int][Math]::Min(15, [Math]::Ceiling($remaining))) }
    } while ([datetime]::UtcNow -lt $deadline)
    # The final check occurs after the entire delayed observation interval.
    $report.defender_after = Assert-DefenderCoverage
    $report.events = @(Get-DefenderValidationEvents -Since $started)
    $report.threat_detections = @(Get-RecentDefenderThreatDetections -Since $started)
    if ($report.events.Count -gt 0 -or $report.threat_detections.Count -gt 0) { throw 'Defender reported a threat during delayed observation.' }
    Assert-ValidationArtifacts -Artifacts $report.artifacts
    $report.result = 'passed'
  } catch {
    $report.error = $_.Exception.Message
    # Preserve evidence even when a scan or process launch failed first.
    try { $report.events = @(Get-DefenderValidationEvents -Since $started) } catch { }
    try { $report.threat_detections = @(Get-MpThreatDetection -ErrorAction Stop) } catch { }
    throw
  } finally {
    $report.finished_utc = [datetime]::UtcNow.ToString('o')
    $report | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ReportPath -Encoding UTF8
  }
}

if ($MyInvocation.InvocationName -ne '.') {
  if (-not $ArtifactDirectory -or -not $ReportPath) { throw 'ArtifactDirectory and ReportPath are required.' }
  Invoke-WindowsDefenderValidation -ArtifactDirectory $ArtifactDirectory -ReportPath $ReportPath -ObservationSeconds $ObservationSeconds
}
