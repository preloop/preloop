# Windows code signing (SignPath Foundation)

> **Code signing policy (user-facing):** see
> [code-signing-policy.md](./code-signing-policy.md).
> Free code signing provided by [SignPath.io](https://about.signpath.io/),
> certificate by [SignPath Foundation](https://signpath.org/).

Preloop’s release workflow can Authenticode-sign Windows CLI binaries via
[SignPath Foundation](https://signpath.org/) (free for open-source projects).
Signing is **optional until credentials are configured**: releases still
may publish unsigned binaries, but the required Defender validation must pass.

CI wiring lives in `.github/workflows/release.yml` (`sign-windows-cli` job)
and `.signpath/artifact-configurations/windows-cli.xml`.

## What the repo already automates

Once SignPath secrets/vars are present on `preloop/preloop`:

1. Release builds embed PE version metadata (`CompanyName`, `ProductName`, …)
2. Windows `*.exe` artifacts are uploaded to GitHub Actions
3. SignPath signs them (Authenticode)
4. Finalized binaries pass the required Defender validation below
5. Signed binaries (and `SHA256SUMS`) are attached to the GitHub Release

No private key is stored in GitHub. SignPath holds the certificate on an HSM.

## Manual steps (required once — maintainers)

These cannot be done from the codebase alone.

### 1. Apply for SignPath Foundation (OSS)

1. Open [SignPath Foundation / open source](https://signpath.org/)
2. Apply with the GitHub org/repo that publishes releases: `https://github.com/preloop/preloop`
3. Describe the product (Preloop CLI) and why signing is needed (Defender /
   SmartScreen false positives on unsigned Go binaries)
4. Wait for approval (often about 1–2 weeks)

Only an org owner / maintainer with authority over the GitHub repo can complete
this.

### 2. Configure the SignPath project

After approval, in the SignPath UI:

1. Create (or open) an organization and note the **Organization ID** (UUID)
2. Create a project, e.g. slug `preloop`
3. Link **Trusted Build System → GitHub.com** for `preloop/preloop`
4. Install the **SignPath GitHub App** on the `preloop` org / repo when prompted
5. Add an **Artifact Configuration**:
   - Slug: `windows-cli`
   - XML: copy from [`.signpath/artifact-configurations/windows-cli.xml`](../.signpath/artifact-configurations/windows-cli.xml)
6. Add a **Signing policy**, e.g. slug `release-signing`
   - Restrict to tag builds / `refs/tags/v*` as required by your policy
   - Grant submitter permission to the bot/user that will use the API token

### 3. Add GitHub Actions secrets and variables

In `https://github.com/preloop/preloop/settings/secrets/actions`:

| Kind | Name | Source |
|------|------|--------|
| **Secret** | `SIGNPATH_API_TOKEN` | SignPath → API token (submit signing request) |
| **Variable** | `SIGNPATH_ORGANIZATION_ID` | SignPath org UUID |
| **Variable** | `SIGNPATH_PROJECT_SLUG` | e.g. `preloop` |
| **Variable** | `SIGNPATH_SIGNING_POLICY_SLUG` | e.g. `release-signing` |
| **Variable** | `SIGNPATH_ARTIFACT_CONFIGURATION_SLUG` | `windows-cli` (optional; workflow defaults to this) |

Optional:

| Kind | Name | Purpose |
|------|------|---------|
| **Secret** | `VIRUSTOTAL_API_KEY` | Upload Windows CLI binaries to VirusTotal on each tag release |

### 4. Verify on the next tag

1. Tag a release (`vX.Y.Z`) as usual
2. Confirm the `Sign Windows CLI` job runs without skipping SignPath
3. On a Windows machine:

   ```powershell
   Get-AuthenticodeSignature .\preloop-windows-amd64.exe
   # Status should be Valid after SignPath is enabled
   ```

Until step 3 is done, the workflow prints a warning and publishes **unsigned**
binaries only after the Defender gate passes.

## Priority checklist (P0 / P1 / P2)

| Priority | Item | Status in this repo |
|----------|------|---------------------|
| **P0** | Fix `detect_arch` for Git Bash `i686` / WOW64 | Done — `scripts/install-cli.sh` |
| **P0** | Ship PowerShell installer as the Windows path | Done — `scripts/install-cli.ps1` + docs |
| **P1** | Authenticode-sign Windows release binaries (SignPath) | Wired — enable with secrets (this doc) |
| **P1** | Embed PE version info (`go-winres`) | Done — release `build-cli` job |
| **P1** | VirusTotal scan each Windows release | Wired — optional `VIRUSTOTAL_API_KEY` |
| **P1** | Submit Microsoft WDSI false positives when flagged | Manual — see below (cannot automate portal) |
| **P2** | Docs: Windows install, Defender recovery, checksums | Done — [windows-cli.md](./windows-cli.md), SECURITY.md |
| **P2** | Official `go install` / build-from-source escape hatch | Done — root + `cli/README.md` |

## Ongoing release hygiene

When `VIRUSTOTAL_API_KEY` is set, the release workflow uploads
`preloop-windows-*.exe` to VirusTotal and appends analysis links to the
release notes.

After each Windows release (especially before SignPath reputation builds):

1. Open the VirusTotal links from the release notes (or upload manually if the
   secret is not configured)
2. If Microsoft flags the binary, submit a false positive at
   [WDSI file submission](https://www.microsoft.com/en-us/wdsi/filesubmission)
   with the GitHub release URL and source repo
3. Keep PE metadata and signing enabled — reputation accumulates over signed
   releases

## User-facing docs

See [windows-cli.md](./windows-cli.md) for install paths and Defender recovery.

## Required Defender release validation

The `validate-windows-cli` job runs after signing and blocks `create-release`
unless the finalized Windows AMD64 and ARM64 artifacts pass validation. It
first runs `prepare-windows-defender.ps1` on its disposable GitHub-hosted runner
to enable protections and remove the hosted build image's inherited exclusions
before downloading release executables. Setup refuses other environments and
fails if policy prevents enabling protection. The separate required Windows
security test workflow exercises this setup and the actual scanner on PRs.

Validation updates Defender intelligence, requires active antivirus, real-time, behavior,
cloud and download protection without exclusions, and scans both executables.
It runs `version`, `--help`, and `agents discover --no-onboard-prompt` on AMD64 with telemetry
disabled, then observes the unchanged files for ten minutes. A threat event,
remediation, missing or changed file, failed command, or unavailable required
protection fails the job. The workflow never adds exclusions, disables
protection, or removes Mark of the Web. The validator does not change security
settings; runner setup only strengthens them. Runner environments that cannot meet
these requirements block publication instead of reporting a clean scan.

The `windows-defender-report` Actions artifact retains JSON evidence and scan
and command logs even on failure. The report records hashes, Authenticode
status, OS, scanner engine/platform/signature versions, timestamps, and detected
threats. ARM64 coverage is a static scan on AMD64; the job does not claim native
ARM64 execution, SmartScreen reputation, corporate EDR acceptance, or that
future security intelligence will always return the same verdict. Signing
identifies the publisher and does not guarantee a clean malware verdict.

To reproduce on a disposable AMD64 Windows host with Defender already enabled:

```powershell
$env:PRELOOP_DISABLE_TELEMETRY = 'true'
./scripts/test-windows-defender.ps1 -ArtifactDirectory ./windows-artifacts `
  -ReportPath ./windows-defender-report/result.json -ObservationSeconds 600
```

The directory must contain the finalized `preloop-windows-amd64.exe` and
`preloop-windows-arm64.exe`. Retain the report and submit a suspected false
positive to Microsoft with its exact release hash; do not bypass a failed gate.
