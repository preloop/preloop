# Troubleshooting

After this page you can diagnose the failures people actually hit: the OSS installer refusing to start, agents that onboard but stay "unverified", WSL discovery misses, locked-out registration, and MCP connections that silently show no tools.

## Installer

### "Docker is not installed" / daemon errors

The installer probes Docker before doing anything. The three messages and what they mean:

- `Docker is not installed (the 'docker' command was not found).` — install Docker Desktop (macOS) or Docker Engine (Linux) first.
- `Docker is installed but the daemon is not running.` — start it: `open -a Docker` on macOS, `sudo systemctl start docker` on Linux.
- `Docker is installed but its daemon did not respond within 10 seconds.` — the daemon is wedged. Restart it (`killall Docker && open -a Docker`, or `sudo systemctl restart docker`) and verify with `docker info`.

`Docker Compose v2 is required` means the `docker compose` subcommand failed — upgrade Docker or install the compose plugin.

### The install ran but something is off

Everything the installer did is in `~/.preloop-oss/install.log`. On a compose failure the installer prints the last 15 lines automatically; read the full log before retrying.

### Admin email rejected at the prompt

The installer validates the admin email up front (`name@domain.tld` shape) so first-user creation cannot fail at the very end. Interactive installs re-prompt (type `skip` to leave signups open instead); unattended installs fail fast with `PRELOOP_ADMIN_EMAIL must be a plausible address` before touching anything.

### "FIRST USER WAS NOT CREATED" banner

If unattended first-user creation fails, the installer exits non-zero with a loud banner — and deliberately leaves public signups **open** so you are not locked out. Create the first user in the console using the **setup link** the banner prints (`<PRELOOP_URL>/register#bootstrap=<token>` — while the instance has zero users, registration requires that token), then re-run the installer (or set `REGISTRATION_ENABLED=false` in `~/.preloop-oss/.env` and `docker compose up -d api`) to close registration.

### Registration is closed but the database is fresh

Re-running the installer against a fresh database (for example after deleting the postgres volume) while `.env` still says `REGISTRATION_ENABLED=false` would lock everyone out. The installer detects this — it counts users in the database and prints:

```text
The existing configuration disables public signup, but the database is fresh
(no users): re-enabling registration so the first user can sign up.
```

If you hit a closed `/register` on an instance you know is empty, re-run the installer or flip `REGISTRATION_ENABLED=true` in `.env` yourself and restart the api service.

Note that on an empty instance with `PRELOOP_BOOTSTRAP_TOKEN` set in `.env` (every installer-managed install), `/register` shows a **"Setup link required"** notice instead of the open form: the first signup must use the setup link (`<PRELOOP_URL>/register#bootstrap=<token>`), regardless of `REGISTRATION_ENABLED`. Rebuild the link from the token in `~/.preloop-oss/.env`, or re-run the installer to have it printed again. A **"This instance has already been claimed"** notice on the setup link means a user already exists — sign in instead, or ask the admin for an invitation.

## CLI and onboarding

### An agent shows `partial` in the onboarding summary

`partial` is not an error. It means MCP and model routing were configured but the managed launcher step was skipped, almost always because the agent binary is not on `PATH`:

```text
codex binary not found in PATH — launcher skipped; MCP and model routing configured
```

Install the binary (or fix `PATH`) and re-run `preloop agents onboard <agent>`.

### WSL: discovery finds nothing / everything is `partial`

Agents installed on the Windows side are not on the WSL `PATH`. The CLI prints this hint when it detects WSL:

```text
Running under WSL: agents installed on Windows are not on the WSL PATH — install the agent inside WSL or add its Windows install dir to PATH.
```

Prefer installing the agents inside WSL; mixing Windows binaries with a WSL-side Preloop CLI leaves the launcher step permanently skipped.

For Docker Desktop setup, the native Windows CLI binaries, and the current
Windows limitations, see [Windows (WSL2)](../operations/windows-wsl2.md).

### Agent has an "unverified" badge in the console

Onboarding fires one live validation prompt through the agent to prove model traffic actually flows through the gateway. Two outcomes leave the agent enrolled but unverified:

- **Live check throttled** — the provider rate-limited the verification call.
- **Upstream refused** — the credential authenticated and reached the provider, but the provider rejected the call (billing/quota). This proves the plumbing works, so it is not treated as a failure.

Both resolve the same way:

```bash
preloop agents validate <agent> --live
```

`preloop agents validate <agent>` (without `--live`) prints the full checklist — `preloop_server_present`, `gateway_base_url_ok`, `gateway_token_ok`, `model_provider_rewritten`, `control_*` — which pinpoints what is misconfigured.

### Rolling back an onboarding

Every onboarding backs up the original config next to it:

```bash
preloop agents restore <agent>    # put the local config back
preloop agents offboard <agent>   # restore config AND remove the managed enrollment
```

## MCP connections

### Client connects but shows no tools

The tool list is filtered per subject. Check, in order:

1. The `Authorization: Bearer <key>` header is present and the key is valid (`preloop auth status`, or curl `/api/v1/auth/api-keys` as the console user).
2. Built-in tracker tools (`get_issue`, `create_issue`, …) only appear when a tracker is connected to the account.
3. Subject-scoped governance can hide tools for a specific API key or managed agent — check the key's or agent's governance settings in the console.
4. External MCP server tools require a successful **Scan Tools** on the server entry under **Tools**.

### Approval-gated calls time out

The MCP call blocks until the workflow decides, up to the workflow's `timeout_seconds` (default 300). Either raise the timeout, enable `async_approval` on the workflow so the agent polls `get_approval_status` instead of holding the connection, or make sure approvers actually get notified (SMTP configured, mobile app installed).

## Self-hosted operations

### Telemetry in test/CI environments

`PRELOOP_DISABLE_TELEMETRY=true` on the server **and** in the shell running the CLI disables the daily version check-in and all adoption telemetry. Opted-out installs never phone home and never appear in instance metrics. Set it for every disposable install — see [Upgrading Preloop](../operations/upgrade.md#disabling-telemetry-internaltest-installs).

### Where things live

| What | Where |
|------|-------|
| OSS stack install | `~/.preloop-oss/` (`docker-compose.yaml`, `.env`, `install.log`, `backups/`, `tls/`) |
| Pre-upgrade DB dumps | `~/.preloop-oss/backups/preloop-<version>-<timestamp>.sql` |
| CLI config | `~/.preloop/config.yaml` |
| Per-agent approval-hook credentials | `~/.preloop/agents/` |
| Onboarding config backups | next to each agent's own config file |

## Still stuck?

- [Discord](https://discord.gg/P6nWSee4jv)
- [GitHub issues](https://github.com/preloop/preloop/issues)
- [support@preloop.ai](mailto:support@preloop.ai)
