# Windows code signing (SignPath Foundation)

Preloop’s release workflow can Authenticode-sign Windows CLI binaries via
[SignPath Foundation](https://signpath.org/) (free for open-source projects).
Signing is **optional until credentials are configured**: releases still
publish unsigned binaries so tagging is never blocked.

CI wiring lives in `.github/workflows/release.yml` (`sign-windows-cli` job)
and `.signpath/artifact-configurations/windows-cli.xml`.

## What the repo already automates

Once SignPath secrets/vars are present on `preloop/preloop`:

1. Release builds embed PE version metadata (`CompanyName`, `ProductName`, …)
2. Windows `*.exe` artifacts are uploaded to GitHub Actions
3. SignPath signs them (Authenticode)
4. Signed binaries (and `SHA256SUMS`) are attached to the GitHub Release

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
| **Secret** | `VIRUSTOTAL_API_KEY` | Future release scanning (not required for signing) |

### 4. Verify on the next tag

1. Tag a release (`vX.Y.Z`) as usual
2. Confirm the `Sign Windows CLI` job runs without skipping SignPath
3. On a Windows machine:

   ```powershell
   Get-AuthenticodeSignature .\preloop-windows-amd64.exe
   # Status should be Valid after SignPath is enabled
   ```

Until step 3 is done, the workflow prints a warning and publishes **unsigned**
binaries (same as today).

## Ongoing release hygiene

After each Windows release (especially before SignPath reputation builds):

1. Upload `preloop-windows-amd64.exe` to [VirusTotal](https://www.virustotal.com/)
2. If Microsoft flags it, submit a false positive at
   [WDSI file submission](https://www.microsoft.com/en-us/wdsi/filesubmission)
   with the GitHub release URL and source repo
3. Keep PE metadata and signing enabled — reputation accumulates over signed
   releases

## User-facing docs

See [windows-cli.md](./windows-cli.md) for install paths and Defender recovery.
