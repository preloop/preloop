BeforeAll {
  . "$PSScriptRoot/../scripts/check-windows-runner.ps1"
}

Describe 'Disposable Defender infrastructure classification' {
  It 'retries unavailable protection and known service HRESULTs' {
    Test-RetryableDefenderSetup -Message 'Defender coverage unavailable: AntivirusEnabled is not enabled.' -ErrorId '' | Should -BeTrue
    Test-RetryableDefenderSetup -Message 'RPC unavailable' -ErrorId 'HRESULT 0x800706be,Update-MpSignature' | Should -BeTrue
  }
  It 'does not retry policy failures, detections or unknown exceptions' {
    foreach ($message in @('Defender exclusions prevent an unqualified validation: ExclusionPath.', 'Malware detected in preloop.exe', 'unknown error', 'Defender must be in Normal mode.')) {
      Test-RetryableDefenderSetup -Message $message -ErrorId 'Unknown' | Should -BeFalse
    }
  }
}
