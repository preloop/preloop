# CLI Reference

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

After this page you can install the `preloop` CLI, authenticate it against Cloud or your own instance, onboard and offboard local agents, manage policies and approvals from the terminal, and read the onboarding summary table and exit codes correctly in scripts.

## Install

```bash
curl -fsSL https://preloop.ai/install/cli | sh
```

The installer downloads the release binary for your OS/arch from GitHub Releases. Useful environment variables:

| Variable | Effect |
|----------|--------|
| `PRELOOP_VERSION` | Pin a specific release instead of the latest |
| `INSTALL_DIR` | Override the install directory |
| `PRELOOP_CONFIRM` | Truthy (`1`, `y`, `yes`, `true`, `on`): accept all prompts, for provisioning scripts, devcontainers, CI |
| `PRELOOP_DISABLE_TELEMETRY` | `true`: no version check-ins, no conversion events, no update notices. Set this on every test or CI run |

Pre-built binaries are also on [GitHub Releases](https://github.com/preloop/preloop/releases), or build from source with `make install` in `cli/`.

## Global flags and configuration

```text
--config    config file (default $HOME/.preloop/config.yaml)
--token     access token (overrides PRELOOP_TOKEN and config file)
--url       API base URL (overrides PRELOOP_URL and config file)
-v, --verbose
```

Resolution order, token: `--token` > `PRELOOP_TOKEN` > config file. URL: `--url` > `PRELOOP_URL` > config file. Point the CLI at a self-hosted instance with `preloop login --url https://preloop.example.com` once; the URL is stored in `~/.preloop/config.yaml`.

## Authentication

```bash
preloop login                # OAuth via browser (also: preloop auth login)
preloop login --headless     # copy/paste OAuth for SSH / no-GUI hosts
preloop login --token <tok>  # skip OAuth, save a token directly
preloop signup               # same flow, lands on the sign-up page
preloop auth status          # active token + API URL
preloop auth token           # print the current access token
preloop auth logout
```

`--loopback` forces the local-callback OAuth flow; `--code` resumes a previous headless login. `--loopback` and `--headless` are mutually exclusive.

## Agents

```bash
preloop agents discover                  # detect local agents (read-only), then offer onboarding
preloop agents discover --json           # machine-readable, no prompts
preloop agents discover --no-onboard-prompt
preloop agents onboard <agent>           # onboard one agent ("enroll" is an alias)
preloop agents onboard --all -y          # everything discovered, no prompts
preloop agents onboard <agent> --dry-run # preview account + config changes
preloop agents onboard <agent> --approvals  # + native tool-permission hook (Claude Code, Codex CLI, Cursor)
preloop agents onboard <agent> --tags env=prod,team=infra
preloop agents status <agent>            # local + remote enrollment state
preloop agents list                      # managed agents in the account
preloop agents validate <agent>          # config validation
preloop agents validate <agent> --live   # + a live prompt through the agent
preloop agents reconnect "Claude Code"  # sign in and repair subscription credentials only
preloop agents reconnect "Codex CLI"    # same recovery for a ChatGPT subscription
preloop agents reconnect "Claude Code" --from-local # use a fresh login completed separately
preloop agents install-plugin <agent>    # Agent Control runtime plugin (openclaw | hermes)
preloop agents install-runtime <hermes|openclaw>  # install the runtime itself, then onboard
preloop agents restore <agent>           # restore the most recent local config backup
preloop agents offboard <agent>          # restore config + remove managed enrollment
preloop agents offboard --all -y --remove-model yes --remove-mcp-servers yes
preloop agents starter-policy <mcp-server> [-o file] [--apply]
```

Onboarding runs a live validation prompt by default; skip it with `--skip-live-validate`.

`agents reconnect` preserves the existing enrollment, model bindings, policies,
gateway config and backups. It repairs a shared credential once and attaches any
legacy split model rows to it. It only updates subscription models owned by this
machine's enrollment in the selected account. Use `--from-local` after completing
`claude auth login --claudeai` or `codex login`. Expired or incomplete local
credentials are refused. Update the CLI if your installed version does not yet
provide this command.

### The onboarding summary table

Batch onboarding continues past individual failures and prints a summary:

```text
Onboarding summary:
  Agent        Status     Reason
  Claude Code  onboarded  -
  Codex CLI    partial    codex binary not found in PATH - launcher skipped; MCP and model routing configured
  Windsurf     failed     ...
```

| Status | Meaning |
|--------|---------|
| `onboarded` | Full onboarding: MCP firewall and (where supported) model routing configured |
| `partial` | MCP and model config applied, but the managed launcher step was skipped, usually a missing agent binary. Not a failure. |
| `failed` | Enrollment error for this agent; see Reason |

The Reason column also carries live-validation outcomes. `live validation throttled - model traffic unverified` means enrollment succeeded but the verification prompt was rate-limited; the agent shows an **unverified** badge in the console until you re-verify with `preloop agents validate <agent> --live`. `live validation inconclusive (upstream billing/quota)` means the credential authenticated and reached the provider, but the provider refused the call: also re-verify later.

### Exit codes

- **0**: at least one attempted agent onboarded, fully or partially (and trivially when nothing was attempted). A single-agent `partial` also exits 0.
- **1**: every attempted agent failed, or another error occurred.

Script against the summary, not just the exit code, when you care about specific agents.

### WSL

If you run the CLI inside WSL but installed agents on the Windows side, onboarding prints:

```text
Hint: Running under WSL: agents installed on Windows are not on the WSL PATH - install the agent inside WSL or add its Windows install dir to PATH.
```

Native Windows CLI binaries (`amd64` and `arm64`) also ship with every release.
WSL remains the recommended environment, see
[Windows (WSL2)](../operations/windows-wsl2.md) for setup and current limitations.

### Support levels

| Agent | MCP firewall | Model gateway routing | Agent Control (live channel) |
|-------|--------------|----------------------|------------------------------|
| Claude Code | Yes | Yes | n/a |
| Codex CLI | Yes | Yes | n/a |
| Gemini CLI | Yes | Yes | n/a |
| OpenCode | Yes | Yes | n/a |
| Hermes | Yes | Yes | Yes (runtime plugin) |
| OpenClaw | Yes | Yes (OpenAI-compatible gateway) | Yes (runtime plugin) |
| Cursor | Yes | Manual BYOK: set the OpenAI base-URL override in Cursor's Settings → Models yourself; covers the AI panel incl. Agent mode | n/a |
| Claude Desktop | Yes | No | n/a |
| Windsurf | Yes | No | n/a |
| VS Code / Copilot | Yes | No | n/a |
| Copilot CLI | Yes (`~/.copilot/mcp-config.json`) | No on onboard. `preloop copilot` launches the CLI with gateway env vars | n/a |
| Antigravity | Yes | No (locked to Google-hosted models) | n/a |
| Devin | Yes | No (inference runs in Cognition's cloud) | n/a |

"MCP firewall: Yes" means tool calls are governed; agents without gateway routing keep using their own model credentials, so cost analytics and model budgets do not apply to them.

Claude Code discovery: the CLI treats `~/.claude.json` or a `claude` binary on `PATH` as install markers (fresh installs have no `~/.claude/settings.json` yet) and bootstraps the canonical `~/.claude/settings.json` during onboarding.

## Policies

```bash
preloop policy list [-f table|json|yaml]
preloop policy validate <file>
preloop policy apply <file> [--dry-run] [-r]   # -r: recurse into a directory
preloop policy diff <file>
preloop policy export [-o file]
preloop policy generate "<prompt>" [-o file] [-f prompt-file] \
  [--from-audit-logs --start-date 2026-01-01 --end-date 2026-02-01] [--no-context]
```

## Tools and approvals

```bash
preloop tools list [-f table|json|yaml]
preloop tools describe <tool-name>
preloop tools exec <tool-name> --args '{"key": "value"}' [--timeout 60s]

preloop approvals list                 # approval workflows
preloop approvals pending [-l 20]
preloop approvals approve <request-id> [-r "reason"]
preloop approvals deny <request-id> [-r "reason"]
```

## Usage import

```bash
preloop usage import cursor-usage.csv                  # Cursor dashboard Usage export
preloop usage import events.json                       # normalized usage events
preloop usage import cursor-usage.csv --agent-id <id>  # explicit attribution
preloop usage import cursor-usage.csv --source cursor  # origin label (default: cursor)
preloop usage import export.csv --column-map '{"cost":"Cost to You"}'
```

Imports spend the model gateway never metered, such as Cursor's bundled Composer and Auto models. The endpoint is chosen from the file extension: `.csv` for a Cursor Usage export, `.json` for normalized events. Records are labeled as imported, reported separately from gateway-metered spend, and never counted against gateway budgets. Re-importing the same file is safe: duplicates are detected and reported as skipped.

Without `--agent-id` the account's onboarded Cursor agent is used, so run `preloop agents onboard cursor` first or pass an id explicitly. See [Importing usage from Cursor](cost/importing-cursor-usage.md).

## Version and updates

```bash
preloop version           # version, commit, build date
preloop version --check   # check for updates
```

The CLI checks for updates at most once per day. With `PRELOOP_DISABLE_TELEMETRY=true` the check-in is disabled entirely: update notices are suppressed too, since they ride on the check-in response.

## Related

- [Quick Start: onboard local agents](quickstart-cli.md)
- [Install the OSS stack](../operations/installation.md)
- [Importing usage from Cursor](cost/importing-cursor-usage.md)
- [Troubleshooting](troubleshooting.md)
