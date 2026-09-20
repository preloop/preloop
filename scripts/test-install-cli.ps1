#Requires -Version 5.1
<#
.SYNOPSIS
  Run offline Windows installer regression tests without external modules.
.DESCRIPTION
  Uses a harmless locally compiled executable and mocked downloads. Each case
  runs the actual installer in a child Windows PowerShell. Temporary files and
  the original user PATH are restored even when an assertion fails.
#>
[CmdletBinding()]
param(
  [string]$Case,
  [string]$Fixture,
  [string]$TestRoot,
  [string]$Installer = (Join-Path $PSScriptRoot 'install-cli.ps1')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$env:PRELOOP_DISABLE_TELEMETRY = 'true'

if ($Case) {
  $env:PRELOOP_VERSION = '0.0.0-test'
  $env:PRELOOP_REPO = 'example/fixture'
  $env:PRELOOP_CONFIRM = '1'
  $env:PROCESSOR_ARCHITECTURE = 'AMD64'
  $env:PROCESSOR_ARCHITEW6432 = ''
  $env:INSTALL_DIR = Join-Path $TestRoot 'install'
  $env:USERPROFILE = Join-Path $TestRoot 'profile'
  $env:PRELOOP_TEST_EXECUTION_LOG = Join-Path $TestRoot 'executed.txt'
  $fixtureHash = (Get-FileHash -LiteralPath $Fixture -Algorithm SHA256).Hash

  function Invoke-WebRequest {
    param([string]$Uri, [string]$OutFile, [switch]$UseBasicParsing)
    if ($Uri.EndsWith('/SHA256SUMS')) {
      $valid = "$fixtureHash  preloop-windows-amd64.exe"
      switch ($Case) {
        'unavailable' {
          Set-Content -LiteralPath $OutFile -Value 'partial response' -Encoding ASCII
          throw 'Fixture checksum download failed.'
        }
        'malformed' { $content = 'not-a-hash  preloop-windows-amd64.exe' }
        'missing' { $content = "$fixtureHash  preloop-linux-amd64" }
        'empty' { $content = '' }
        'duplicate' { $content = "$valid`n$valid" }
        'duplicate-malformed' { $content = "$valid`ninvalid  preloop-windows-amd64.exe" }
        'mismatch' { $content = (('0' * 64) + '  preloop-windows-amd64.exe') }
        'success-binary' { $content = "$($fixtureHash.ToLowerInvariant()) *preloop-windows-amd64.exe`n$fixtureHash  preloop-linux-amd64`n" }
        default { $content = $valid }
      }
      Set-Content -LiteralPath $OutFile -Value $content -Encoding ASCII
    } else {
      Copy-Item -LiteralPath $Fixture -Destination $OutFile
      Set-Content -LiteralPath $OutFile -Stream Zone.Identifier -Value "[ZoneTransfer]`r`nZoneId=3" -Encoding ASCII
      if ($Case -eq 'binary-unavailable') { throw 'Fixture binary download failed after partial write.' }
    }
  }

  function Unblock-File { throw 'Installer must not remove download-zone metadata.' }
  function Add-MpPreference { throw 'Installer must not change Defender preferences.' }
  function Set-MpPreference { throw 'Installer must not change Defender preferences.' }
  function Remove-MpPreference { throw 'Installer must not change Defender preferences.' }

  & $Installer
  exit 0
}

function Assert-True {
  param([bool]$Condition, [string]$Message)
  if (-not $Condition) { throw $Message }
}

$root = Join-Path ([IO.Path]::GetTempPath()) ('preloop-installer-tests-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root | Out-Null
$originalUserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$originalTemp = $env:TEMP
$originalTmp = $env:TMP
$fixturePath = Join-Path $root 'fixture.exe'
$passed = 0
try {
  # Compile a benign program that only records invocation and prints a version.
  Add-Type -TypeDefinition @'
using System;
using System.IO;
public static class InstallerFixture {
    public static int Main(string[] args) {
        File.AppendAllText(Environment.GetEnvironmentVariable("PRELOOP_TEST_EXECUTION_LOG"), string.Join(" ", args) + "\n");
        Console.WriteLine("preloop installer test fixture");
        return 0;
    }
}
'@ -OutputAssembly $fixturePath -OutputType ConsoleApplication
  $expectedHash = (Get-FileHash -LiteralPath $fixturePath -Algorithm SHA256).Hash
  $cases = @('success', 'success-binary', 'unavailable', 'malformed', 'missing', 'empty', 'duplicate', 'duplicate-malformed', 'mismatch', 'binary-unavailable')
  foreach ($testCase in $cases) {
    $caseRoot = Join-Path $root $testCase
    $installDir = Join-Path $caseRoot 'install'
    $downloadDir = Join-Path $caseRoot 'downloads'
    New-Item -ItemType Directory -Path $installDir, $downloadDir -Force | Out-Null
    $target = Join-Path $installDir 'preloop.exe'
    [IO.File]::WriteAllText($target, 'previous installation must survive verification failures')
    $previousHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
    $env:TEMP = $downloadDir
    $env:TMP = $downloadDir
    $arguments = @('-NoProfile', '-NonInteractive', '-File', "`"$PSCommandPath`"", '-Case', $testCase,
      '-Fixture', "`"$fixturePath`"", '-TestRoot', "`"$caseRoot`"", '-Installer', "`"$Installer`"")
    $process = Start-Process -FilePath "$PSHOME\powershell.exe" -ArgumentList $arguments -Wait -PassThru -NoNewWindow `
      -RedirectStandardOutput (Join-Path $caseRoot 'stdout.txt') `
      -RedirectStandardError (Join-Path $caseRoot 'stderr.txt')
    $success = $testCase.StartsWith('success')
    $stderr = Get-Content -LiteralPath (Join-Path $caseRoot 'stderr.txt') -Raw
    Assert-True (($process.ExitCode -eq 0) -eq $success) "$testCase unexpected exit $($process.ExitCode): $stderr"
    Assert-True (Test-Path -LiteralPath $target) "$testCase removed the installed file."
    $actualHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
    $executionLog = Join-Path $caseRoot 'executed.txt'
    if ($success) {
      Assert-True ($actualHash -eq $expectedHash) "$testCase did not install the verified binary."
      Assert-True (Test-Path -LiteralPath $executionLog) "$testCase did not execute the verified binary."
      Assert-True ((Get-Content -LiteralPath $executionLog -Raw).Trim() -eq 'version') "$testCase invoked an unexpected command."
      $zone = Get-Content -LiteralPath $target -Stream Zone.Identifier -Raw
      Assert-True ($zone -match 'ZoneId=3') "$testCase removed download-zone metadata."
    } else {
      Assert-True ($actualHash -eq $previousHash) "$testCase replaced the previous installation."
      Assert-True (-not (Test-Path -LiteralPath $executionLog)) "$testCase executed an unverified binary."
      Assert-True ([Environment]::GetEnvironmentVariable('Path', 'User') -eq $originalUserPath) "$testCase changed the user PATH."
    }
    Assert-True (@(Get-ChildItem -LiteralPath $downloadDir -Force).Count -eq 0) "$testCase left downloaded temporary files behind."
    [Environment]::SetEnvironmentVariable('Path', $originalUserPath, 'User')
    $passed++
    Write-Host "PASS $testCase"
  }
  Write-Host "$passed Windows installer regression cases passed."
} catch {
  Write-Warning "Installer test failed. Case artifacts: $root"
  throw
} finally {
  [Environment]::SetEnvironmentVariable('Path', $originalUserPath, 'User')
  $env:TEMP = $originalTemp
  $env:TMP = $originalTmp
  if ($passed -eq 10) { Remove-Item -LiteralPath $root -Recurse -Force }
}
