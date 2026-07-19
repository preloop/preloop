# Security Policy

## Reporting a Vulnerability

Please do not open public GitHub issues for security vulnerabilities.

Report vulnerabilities privately to [security@preloop.ai](mailto:security@preloop.ai) with:

- A description of the issue
- Steps to reproduce or a proof of concept
- The affected version or deployment details
- Any suggested remediation, if available

We will acknowledge receipt as soon as possible and work with you on validation, impact, and disclosure timing.

## Supported Versions

Security fixes are generally applied to the latest supported release line.

For self-hosted deployments, we recommend upgrading to the latest release as soon as practical.

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
