# Install the Open-Source Stack

After this page you have the full Preloop control plane (API, console, MCP firewall, and model gateway) running on your own machine or server under the Apache 2.0 license, with the first user created and public signup closed.

!!! tip "Prefer not to run anything?"
    [Preloop Cloud](https://preloop.ai) is the hosted service with the same
    core features, see the [Quick Start](../guide/quickstart.md). Both paths use the
    same CLI and console.

## Prerequisites

- Docker with the Compose v2 plugin (the installer checks and tells you exactly what is wrong, see [Troubleshooting](../guide/troubleshooting.md#installer))
- macOS, Linux, or Windows with [WSL2](windows-wsl2.md): run the installer from inside your WSL shell

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
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `PRELOOP_SKIP_SMTP=1` | Mail settings |
| `PRELOOP_DISABLE_TELEMETRY=true` | No daily version check-in: set on every test/CI install |
| `INSTALL_DIR` | Override `~/.preloop-oss` |

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
    Windows CLI binaries (`amd64` and `arm64`) ship with every release. Do not
    use the shell installer under Git Bash, it silently installs to a directory
    that is not on the Windows `PATH`. See [Windows (WSL2)](windows-wsl2.md) for the
    binary download, `PATH` setup, and current limitations.

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

- [Windows (WSL2)](windows-wsl2.md): Docker Desktop setup, Windows CLI binaries, and known limits
- [Expose Preloop with TLS](tls.md): manual reverse-proxy setup if you did not use the installer's TLS overlay
- [Onboard local agents with the CLI](../guide/quickstart-cli.md)
- [Troubleshooting](../guide/troubleshooting.md): docker preflight messages, registration reopen semantics, install.log
- For Kubernetes, use the [Helm chart](https://github.com/preloop/preloop/tree/main/helm/preloop) (`helm install preloop ./helm/preloop` from a repo checkout)

!!! info "Editions"
    **Preloop** (open source, this page) is free forever. **Preloop Cloud** is
    the hosted service at preloop.ai. **Preloop Enterprise** adds RBAC and team
    management, budget policies, and FinOps workflows for self-hosted
    deployments, contact [sales@preloop.ai](mailto:sales@preloop.ai).
