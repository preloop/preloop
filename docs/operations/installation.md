# Install the Open-Source Stack

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

After this page you have the full Preloop control plane (API, console, MCP firewall, and model gateway) running on your own machine or server under the Apache 2.0 license, with the first user created and public signup closed.

!!! tip "Prefer not to run anything?"
    [Preloop Cloud](https://preloop.ai) is the hosted service with the same
    core features, see the [Quick Start](../guide/quickstart.md). Both paths use the
    same CLI and console.

## Prerequisites

- Docker with the Compose v2 plugin (the installer checks and tells you exactly what is wrong, see [Troubleshooting](../guide/troubleshooting.md#installer))
- macOS, Linux, or Windows: on Windows the server stack runs under Docker Desktop with the [WSL2 backend](windows-wsl2.md), so run this installer from inside your WSL shell (the CLI itself also runs natively on Windows)

## 1. Install and start the stack

```bash
curl -fsSL https://preloop.ai/install/oss | sh
```

`https://preloop.ai/install/oss` redirects to the latest release's install script (`curl -L` follows it; `-fsSL` already includes `-L`). The installer:

1. Downloads the release Docker Compose file into `~/.preloop-oss` and generates a `.env` with a `SECRET_KEY`, a database password, and a first-user **setup token** (`PRELOOP_BOOTSTRAP_TOKEN`).
2. Asks for your **public URL** (default `http://localhost:3000`). Give it an `https://` URL on a public hostname and it terminates TLS for you with an nginx + certbot overlay (Let's Encrypt). `PRELOOP_SKIP_TLS=1` if you terminate TLS yourself.
3. Asks for **SMTP** settings: needed for approval emails, invitations, and password resets. Skippable (`PRELOOP_SKIP_SMTP=1`).
4. Pulls images and starts the stack. Output is quiet; the full log is at `~/.preloop-oss/install.log`.
5. Offers to create the **first user**, then **closes public registration** (`REGISTRATION_ENABLED=false`) so a freshly exposed instance is never open to whoever finds it first. The user is created *before* registration closes: a failure leaves signup open rather than locking you out.

Default local ports:

- Console: `http://localhost:3000`
- API: `http://localhost:8000`
- Model gateway: `http://localhost:8001`

If you skipped first-user creation, use the **setup link** the installer prints at the end (`<PRELOOP_URL>/register#bootstrap=<token>`). While the instance has zero users, registration requires this token, so a freshly exposed instance cannot be claimed by whoever finds it first; the token is ignored once any user exists. The token travels in the URL fragment and is printed only to the terminal, never written to `install.log`. Invite teammates from the console afterwards.

### Unattended installs

Every prompt has an environment variable, so the whole install can run non-interactively:

```bash
curl -fsSL https://preloop.ai/install/oss | \
  PRELOOP_URL=https://preloop.example.com \
  PRELOOP_TLS_EMAIL=ops@example.com \
  PRELOOP_ADMIN_USERNAME=admin \
  PRELOOP_ADMIN_EMAIL=admin@example.com \
  PRELOOP_ADMIN_PASSWORD='a-strong-password' \
  PRELOOP_DISABLE_TELEMETRY=true \
  sh
```

| Variable | Purpose |
|----------|---------|
| `PRELOOP_VERSION` | Pin a release instead of latest |
| `PRELOOP_URL` | Public origin (enables the TLS overlay when `https://` on a public hostname) |
| `PRELOOP_TLS_EMAIL`, `PRELOOP_TLS_STAGING=1`, `PRELOOP_SKIP_TLS=1` | Certificate options |
| `PRELOOP_ADMIN_USERNAME` / `PRELOOP_ADMIN_EMAIL` / `PRELOOP_ADMIN_PASSWORD` | Unattended first-user creation (email is validated up front; the run fails fast on a malformed address) |
| `PRELOOP_SKIP_ADMIN=1` | Skip first-user creation, leave signups open |
| `PRELOOP_BOOTSTRAP_TOKEN` | Provide your own first-user setup token instead of the generated one (kept across re-runs) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `PRELOOP_SKIP_SMTP=1` | Mail settings; see [Email (SMTP)](#email-smtp) |
| `PRELOOP_DISABLE_TELEMETRY=true` | Disable the daily version check-in; saved in the install's `.env`. Set on every test/CI install |
| `DISABLE_VERSION_CHECK=true` | Compatibility alias for the same opt-out; also saved in `.env` |
| `INSTALL_DIR` | Override `~/.preloop-oss` |

The installer reports whether the version check-in is **ON** or **OFF** in its
final summary. Opt-outs are stored in `~/.preloop-oss/.env` (or
`$INSTALL_DIR/.env`) and survive clean-shell Compose runs, installer re-runs,
and upgrades. An existing opt-out takes precedence over installer environment
variables, even if a later run supplies `false`.

To turn the check-in back on, remove both `PRELOOP_DISABLE_TELEMETRY` and
`DISABLE_VERSION_CHECK` from that `.env` file (or set both to `false`), unset
both variables in your shell, then run `docker compose up -d api` from the
install directory. Both flags accept `true`, `1`, `t`, or `yes`, ignoring case
and surrounding whitespace.

For Helm installs, persist the opt-out in your values file:

```yaml
extraEnv:
  - name: PRELOOP_DISABLE_TELEMETRY
    value: "true"
```

Use that values file on upgrades as well. `DISABLE_VERSION_CHECK` is also
accepted through `extraEnv`; remove or disable both entries to opt back in.

## 2. Connect the CLI to your instance

```bash
curl -fsSL https://preloop.ai/install/cli | sh
preloop login --url http://localhost:3000
preloop agents discover
```

The CLI stores the instance URL in `~/.preloop/config.yaml`, so every later
command targets your deployment. You can also set it per-command:
`PRELOOP_URL=http://localhost:3000 preloop login`.

!!! info "Windows"
    The CLI runs natively on Windows (`amd64` and `arm64` binaries ship with
    every release). Install it from PowerShell with
    `irm https://preloop.ai/install/cli.ps1 | iex`, see
    [Windows CLI install](../windows-cli.md). Do not use the shell installer
    under Git Bash, it silently installs to a directory that is not on the
    Windows `PATH`. See [Windows](windows-wsl2.md) for WSL, manual download,
    and current limits.

## Email (SMTP)

Preloop sends invitations, password resets and approval emails through the
SMTP server in `.env`. Without `SMTP_HOST` no email is sent.

| Setting | Meaning |
|---------|---------|
| `SMTP_HOST`, `SMTP_PORT` | The mail server. |
| `SMTP_SECURITY` | `starttls` (default): connect in plain text and upgrade with STARTTLS, usually port 587. `ssl`: implicit TLS from the first byte, usually port 465. `none`: no encryption, for an internal relay only, usually port 25. |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | Optional. Leave both empty for a relay that accepts mail without authentication; Preloop then sends without `AUTH`. Preloop never sends them when `SMTP_SECURITY=none` and reports an error instead. |
| `SMTP_FROM`, `SMTP_FROM_NAME` | The sender address and name. |

If the mail server reports `STARTTLS extension not supported by server`, set
`SMTP_SECURITY` to `ssl` or, for an internal relay without credentials,
`none`. Restart the stack after changing `.env`.

Re-running the installer keeps an existing `SMTP_SECURITY`. On Kubernetes, set
`config.smtp.security` in the Helm values (`starttls`, `ssl` or `none`).

When an invitation email cannot be sent, the invitation is still created.
The console says the email was not sent and why in one sentence (for
example, that `SMTP_HOST` is empty or that the server rejected the
credentials); fix the setting, then use **Resend invitation** on the pending invitation.

## Managing the stack

```bash
cd ~/.preloop-oss

docker compose ps       # status
docker compose logs -f  # logs
docker compose down     # stop
docker compose up -d    # start again
```

Re-running the installer upgrades in place and preserves your configuration, see [Upgrading Preloop](upgrade.md) for the full semantics (pre-upgrade database dumps land in `~/.preloop-oss/backups/`).

## Next steps

- [Windows](windows-wsl2.md): native CLI, Docker Desktop with WSL2 for the stack, and known limits
- [Expose Preloop with TLS](tls.md): manual reverse-proxy setup if you did not use the installer's TLS overlay
- [Onboard local agents with the CLI](../guide/quickstart-cli.md)
- [Troubleshooting](../guide/troubleshooting.md): docker preflight messages, registration reopen semantics, install.log
- For Kubernetes, use the [Helm chart](https://github.com/preloop/preloop/tree/main/helm/preloop) (`helm install preloop ./helm/preloop` from a repo checkout)
- On Azure Kubernetes Service, size the install with [Reference sizing for Helm on AKS](sizing-aks.md)

!!! info "Editions"
    **Preloop** (open source, this page) is free forever. **Preloop Cloud** is
    the hosted service at preloop.ai. Users, invites and teams are included in
    **Preloop (OSS)**, without access controls between users. **Preloop
    Enterprise** adds roles, RBAC/ABAC, team budgets and advanced FinOps workflows for self-hosted
    deployments, contact [sales@preloop.ai](mailto:sales@preloop.ai).
