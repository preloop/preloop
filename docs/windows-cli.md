# Windows CLI install

## Recommended install (PowerShell)

```powershell
irm https://preloop.ai/install/cli.ps1 | iex
```

Pin a version:

```powershell
$env:PRELOOP_VERSION = '1.2.3'
irm https://preloop.ai/install/cli.ps1 | iex
```

The script verifies the downloaded binary against the release's `SHA256SUMS`,
installs to `%LOCALAPPDATA%\Preloop\bin\preloop.exe`, and adds that directory to
your user `PATH`. An unavailable, malformed, missing, duplicate, or mismatched
checksum stops installation before replacing or running the binary. An existing
installation remains unchanged on verification failure.

The installer preserves any download-zone metadata (Mark of the Web) and does
not change Microsoft Defender settings. A matching checksum establishes that
the download matches the release manifest; it is not a malware verdict or a
publisher signature.

If `https://preloop.ai/install/cli.ps1` is not yet deployed on your control
plane (enterprise installer plugin), fetch the release asset directly:

```powershell
irm https://github.com/preloop/preloop/releases/latest/download/install-cli.ps1 | iex
```

## Build from source

If you have a Go toolchain:

```powershell
go install github.com/preloop/preloop/cli/cmd/preloop@latest
```

Or from a clone:

```powershell
git clone https://github.com/preloop/preloop.git
cd preloop\cli
go build -o preloop.exe .\cmd\preloop
```

Building locally is useful for development and investigation. It changes the
binary and its download provenance, so a local build passing a scan does not
establish that the distributed release will pass. Keep Windows protection
enabled when testing either build.

## Bash installer / Git Bash

```sh
curl -fsSL https://preloop.ai/install/cli | sh
```

works on Windows when `PROCESSOR_ARCHITECTURE` / `PROCESSOR_ARCHITEW6432`
are set (normal). Prefer the PowerShell installer: 32-bit Git Bash reports
`uname -m` as `i686` even on 64-bit Windows.

## Microsoft Defender false positives

Go applications can receive false-positive detections, as discussed in
[Microsoft's Go issue tracker](https://github.com/microsoft/go/issues/1255).
A detection name or an unsigned binary alone does not establish why a specific
file was flagged. Investigate the exact release hash, protection settings, and
Defender detection record before classifying the alert.

### If Defender flags the CLI

1. Leave the file quarantined and keep Defender protection enabled.
2. Open **Windows Security → Virus & threat protection → Protection history**
   and record the detection name, affected path, action, and time.
3. Record the CLI release version, Windows version, and Defender security
   intelligence version. Include the SHA256 if it was captured before quarantine
   or is available in the detection record; label a manifest hash as expected
   rather than a measured file hash.
4. Report these details to the maintainers so they can reproduce the detection
   and submit the affected release for Microsoft review. Remove personal paths
   and account details from public reports.

Do not disable protection, add directory/process exclusions, or remove download
metadata to work around an unresolved alert. Use a release that has been
investigated and validated in the affected environment.

### Verify a release binary

```powershell
Get-FileHash .\preloop-windows-amd64.exe -Algorithm SHA256
# Compare to SHA256SUMS from the same GitHub release
```

### Report a false positive

Maintainers can submit the original release artifact and detection details to
[Microsoft Security Intelligence](https://www.microsoft.com/en-us/wdsi/filesubmission)
for review without asking users to restore quarantined files. See
[Windows release validation and signing](./windows-code-signing.md) for release
checks and evidence requirements. A clean scan is evidence for the exact tested
artifact, engine, signatures, settings, and observation period; it cannot
guarantee no future alerts or identical results on other Windows versions.

## Updating

```powershell
preloop update
```

Or re-run the PowerShell installer.
