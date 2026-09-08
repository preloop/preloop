# Security Policy

## Reporting a Vulnerability

Please do not open public GitHub issues for security vulnerabilities.

Report vulnerabilities privately to [security@preloop.ai](mailto:security@preloop.ai) with:

- A description of the issue
- Steps to reproduce or a proof of concept
- The affected version or deployment details
- Any suggested remediation, if available

We will acknowledge receipt as soon as possible and work with you on validation, impact, and disclosure timing.

## Support Period

> **PROPOSED WORDING, DATES NOT YET CONFIRMED.** Everything in this section is
> a drafted proposal awaiting sign-off. The end date below is a placeholder
> with a defensible rationale, not a commitment, until it is confirmed. Once it
> is, delete this notice. Nothing else in this file is draft.

The previous wording said security fixes were "generally applied to the latest
supported release line". CRA Article 13(8) and Annex II require a stated
support period, and an assessor reads "generally" as "undefined", so here it
is as a date.

**The support period for Preloop ends on 31 December 2031.**

Until that date, Preloop receives security updates without charge, for both the
open-source edition and the hosted service, in line with the release-line table
below.

### Why that date

CRA Article 13(8) sets a five-year floor on the support period unless the
product's expected lifetime is shorter, and Preloop's expected lifetime is not
shorter. Five years from the first release made under this policy, rounded up
to a year end so the date is trivial to restate on a Declaration of Conformity
and does not drift with the release schedule.

### Which releases get fixes

| Release line | Security fixes | Notes |
| --- | --- | --- |
| Latest minor (currently `0.15.x`) | Yes, until the support period ends | Fixes land here first |
| Previous minor | Yes, for 90 days after the next minor ships | A bounded upgrade window for self-hosted operators, not an indefinite branch |
| Anything older | No | Upgrade to a supported line |

The 90-day window exists because a self-hosted operator cannot always upgrade
on our schedule. It is a window, not a branch: it closes 90 days after the
successor ships, whether or not anyone has upgraded.

### How the period is extended

- The end date is reviewed at every minor release.
- It is extended in whole years, and it is **never shortened**. If the period
  is extended to 31 December 2032, that is binding in the same way the current
  date is.
- A change to the end date is announced in `CHANGELOG.md` and in this file at
  least 12 months before the current end date, and the new date is always at
  least 24 months in the future at the time it is announced. An operator
  planning a deployment therefore always has at least two years of visibility.
- Reaching the end date is announced the same way. It is not a silent expiry.

### After the end date

Security fixes stop. Released artefacts stay available: GitHub releases, the
PyPI package, the container images, the Helm chart, and the SBOMs and build
provenance attached to each release. Nothing is withdrawn, and nothing is
patched.

For self-hosted deployments, upgrade to the latest release as soon as
practical. The support period is a floor on how long fixes exist, not advice
about how long to wait.

## Windows antivirus false positives

Microsoft Defender sometimes quarantines unsigned or newly signed Go CLIs
(heuristic detections often end in `!ml`). This is a known class of false
positives for Go on Windows, not evidence that Preloop is malware.

Users: restore the file from Protection history, verify `SHA256SUMS` from the
GitHub release, and see [docs/windows-cli.md](./docs/windows-cli.md).

Maintainers: Authenticode signing via SignPath Foundation and PE version
metadata are wired in the release workflow; enable them by following
[docs/windows-code-signing.md](./docs/windows-code-signing.md). Submit flagged
release hashes to
[Microsoft WDSI](https://www.microsoft.com/en-us/wdsi/filesubmission).

## Telemetry

Preloop emits a small, fixed set of opt-out adoption events. All of them are pseudonymous (random UUIDs, no user data), and this section is the complete list.

### Events

**`install_completed`** — instance-level, sent once per installation. It rides the existing daily version check-in (`POST https://preloop.ai/api/v1/version`); there is no separate outbound call. The check-in payload carries:

- `instance_uuid` — random UUID generated at first server startup
- `version`, `edition` (`oss` or `enterprise`)
- `metadata` — instance metadata; on exactly one check-in it includes `install_completed: true`, plus `install_started_at` / `install_completed_at` **only if** the installer stamped `PRELOOP_INSTALL_STARTED_AT` / `PRELOOP_INSTALL_COMPLETED_AT` into the instance `.env` (absent stamps are omitted, never synthesized)

**`cli_first_run`** — CLI-level, sent once per CLI install. It rides the existing CLI version check-in (`POST /api/v1/cli/version-check`), which carries:

- `client_id` — random UUID stored in the CLI config directory
- `version`, `os`, `arch`
- `preloop_url` — the server URL the CLI is configured against
- `token_fingerprint` — first 16 hex characters of the SHA-256 of the access token; the token itself is never sent, and the bearer header is only attached when the CLI is authenticated against the version-check server itself
- `metadata` — `first_run: true` on the very first run only, and `cmd_<category>` counters (top-level command names only, never arguments or user data)

**`first_session_seen`** — account-level, recorded when an account's first agent runtime session is recorded: account id and timestamp, once per account. This event exists only on deployments running the enterprise growth plugin and is written to that instance's **own database** — it is never transmitted anywhere. Open-source builds contain only an inert no-op hook.

### Opt-out

- `PRELOOP_DISABLE_TELEMETRY=true` in the instance `.env` suppresses all server-side events: version check-ins, `install_completed`, and `first_session_seen`. (`DISABLE_VERSION_CHECK` is honored as a legacy alias.)
- The same variable in the CLI's environment suppresses all CLI events: the check-in, `cli_first_run`, and command counters. Update notifications stop too — they are derived from the check-in response.
- The bash installer itself never phones home.
