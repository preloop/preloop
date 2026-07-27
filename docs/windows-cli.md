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

The script installs to `%LOCALAPPDATA%\Preloop\bin\preloop.exe`, unblocks
Mark of the Web, and adds that directory to your user `PATH`.

If `https://preloop.ai/install/cli.ps1` is not yet deployed on your control
plane (enterprise installer plugin), fetch the release asset directly:

```powershell
irm https://github.com/preloop/preloop/releases/latest/download/install-cli.ps1 | iex
```

## Build from source (avoids download heuristics)

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

Locally built binaries are not marked with Mark of the Web and usually avoid
Microsoft Defender’s “downloaded unsigned Go binary” heuristics.

## Bash installer / Git Bash

```sh
curl -fsSL https://preloop.ai/install/cli | sh
```

works on Windows when `PROCESSOR_ARCHITECTURE` / `PROCESSOR_ARCHITEW6432`
are set (normal). Prefer the PowerShell installer: 32-bit Git Bash reports
`uname -m` as `i686` even on 64-bit Windows.

## Microsoft Defender false positives

Unsigned or newly signed Go CLIs are sometimes quarantined by heuristic
detections (names often end in `!ml`). This is a [known class of false
positives](https://github.com/microsoft/go/issues/1255) for Go on Windows.

### Short-term recovery

1. Open **Windows Security → Virus & threat protection → Protection history**
2. Find `preloop.exe` → **Actions → Allow on device** (or Restore)
3. Verify the file hash against the release `SHA256SUMS` asset
4. Add an exclusion for `%LOCALAPPDATA%\Preloop\bin` (or your `INSTALL_DIR`)
5. Prefer `Unblock-File` on the binary after a manual download

Do **not** turn Defender off globally.

### Verify a release binary

```powershell
Get-FileHash .\preloop-windows-amd64.exe -Algorithm SHA256
# Compare to SHA256SUMS from the same GitHub release
```

### Report a false positive

Anyone can submit the quarantined file at
[Microsoft Security Intelligence](https://www.microsoft.com/en-us/wdsi/filesubmission)
as a false positive. Maintainers should also submit each newly flagged release
hash (see [windows-code-signing.md](./windows-code-signing.md)).

## Updating

```powershell
preloop update
```

Or re-run the PowerShell installer.
