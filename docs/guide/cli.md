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
preloop login --no-runner    # sign in, do not offer a runner install
preloop signup               # same flow, lands on the sign-up page
preloop auth status          # active token + API URL
preloop auth token           # print the current access token
preloop auth logout
```

`--loopback` forces the local-callback OAuth flow; `--code` resumes a previous headless login. `--loopback` and `--headless` are mutually exclusive.

After a successful interactive login the CLI asks whether to install a runner. The default answer is no. See [Install during login](runners/quickstart-linux.md#install-during-login). `--no-runner` and `PRELOOP_NO_RUNNER=1` skip that step. Headless login, `--token`, and `PRELOOP_TOKEN` never install a runner. `preloop runner setup` runs the same step on demand.

## Runners

```bash
preloop runner setup                 # install the service from account policy
preloop runner fg --labels local     # foreground: register, heartbeat, lease jobs
preloop runner enable                # install launchd / systemd / scheduled task
preloop runner start|stop|restart|status
preloop runner disable               # remove the service
preloop runner models                # discover local OpenAI-compatible servers
preloop runner models --register     # approve models for the account gateway
```

`preloop runner setup` asks on a terminal when the account policy is `optional`, installs without a question when it is `required`, and prints one line when it is `forbidden`. On Windows it registers a per-user logon task; no elevation is needed, and an error names the device policy when task creation is blocked. The installed service registers as the hostname with labels `personal`, `os:<os>` and `arch:<arch>`. Remove it with `preloop runner disable`.

Use `--register --yes` for explicit noninteractive approval and `--base-url http://localhost:11434/v1` for an additional loopback server. `PRELOOP_RUNNER_STATE_DIR` selects an isolated runner state directory. See [runner-proxied local models](runner-local-models.md) for registration and transport limits.

## Restricted CI

```bash
mkdir -m 700 ~/ci-secrets               # once: a private directory for issued secrets
preloop ci capabilities
preloop ci preview --input ci-preview.json
preloop ci create --input ci-request.json --secret-file ~/ci-secrets/new-ci-token
preloop ci list
preloop ci show PRINCIPAL_ID
preloop ci update PRINCIPAL_ID --input change.json
preloop ci issue PRINCIPAL_ID --secret-file ~/ci-secrets/replacement-ci-token
preloop ci rotate PRINCIPAL_ID KEY_ID --secret-file ~/ci-secrets/rotated-ci-token
preloop ci revoke PRINCIPAL_ID KEY_ID
preloop ci retire PRINCIPAL_ID
preloop ci subscribe PRINCIPAL_ID --input callback.json --secret-file ~/ci-secrets/new-signing-secret
preloop ci shared-flow FLOW_ID
preloop ci share-flow FLOW_ID --input projects.json
```

These commands use the saved login or `PRELOOP_TOKEN`. `--token` is rejected, so a plaintext token is not placed on the process command line. A secret is written only to a new exclusive file (mode 0600) in a directory you own; an existing file, a missing directory or one you cannot write is refused with the reason, before anything is issued. Stdout is safe metadata. `rotate` and `revoke` take both the identity id and the key id (`preloop ci show PRINCIPAL_ID` lists the keys). `subscribe` needs `subscription:create` in the identity's grant. An unknown `ci` subcommand exits non-zero with an error; CLIs before this change printed the help and exited 0, so `ci retire` on a CLI older than 1.0.0 looked successful while doing nothing. `shared-flow` shows which projects a flow admits for restricted CI; `share-flow` replaces that allowlist on a read-only review flow from `{"project_ids": [...]}`. See [Native restricted CI setup](flows/restricted-ci.md).

## Agents

```bash
preloop agents discover                  # detect local agents (read-only), then offer onboarding
preloop agents discover --json           # machine-readable, no prompts
preloop agents discover --inventory      # offline known-app inventory JSON
preloop agents discover --inventory --all-users # elevated, offline local-profile inventory
preloop agents discover --report --all-users    # elevated, opt-in local-profile report
preloop agents discover --report --no-extension-content   # report without uploading extension text
preloop agents discover --no-onboard-prompt
preloop agents onboard <agent>           # onboard one agent ("enroll" is an alias)
preloop agents onboard --all -y          # everything discovered, no prompts
preloop agents onboard <agent> --dry-run # preview account + config changes
preloop agents onboard <agent> --explain # full custody and plan text, and the detailed result
preloop agents onboard <agent> --approvals  # + native action gates (Claude Code, Codex CLI, Cursor, Copilot CLI, OpenCode, Pi, DeepSeek Harness)
preloop agents onboard <agent> --tags env=prod,team=infra
preloop agents onboard <agent> --manual     # print the config change instead of writing it
preloop agents onboard <agent> --manual-output <dir>  # where to save the manual instructions
preloop agents onboard --resume <agent>     # finish a manual setup: check the change, one real request
preloop agents onboard --resume <agent> --from-env             # check the environment-variable form
preloop agents onboard --resume <agent> --settings-file <path> # check a `claude --settings` file
preloop agents onboard --resume --resume-token <token>         # when ~/.preloop is not writable
preloop agents onboard --cancel <agent>     # cancel a pending manual setup
preloop agents status <agent>            # local + remote enrollment state
preloop agents status <agent> --json     # allowlisted status, no credentials
preloop agents pause [agent]             # pause CLI approval hooks on this machine
preloop agents resume [agent]            # end that pause
preloop agents list                      # managed agents in the account
preloop agents validate <agent>          # config validation
preloop agents validate <agent> --live   # + a direct gateway route/accounting probe
preloop agents reconnect "Claude Code"  # sign in and repair subscription credentials only
preloop agents reconnect "Codex CLI"    # same recovery for a ChatGPT subscription
preloop agents reconnect "Claude Code" --from-local # use a fresh login completed separately
preloop agents install-plugin <agent>    # Agent Control runtime plugin (openclaw | hermes)
preloop agents install-runtime <hermes|openclaw>  # install the runtime itself, then onboard
preloop agents install-runtime <hermes|openclaw> --desktop  # + headless desktop and live-view tunnel
preloop agents desktop <agent> --port 5901  # live view of the agent's desktop for a local VNC client
preloop desktop enable --agent <agent-id>   # add the desktop and live-view tunnel to this host (Debian/Ubuntu)
preloop desktop serve                    # the tunnel itself (run by preloop-desktop-tunnel.service)
preloop agents restore <agent>           # restore the most recent local config backup
preloop agents offboard <agent>          # restore config + remove managed enrollment
preloop agents offboard --all -y --remove-model yes --remove-mcp-servers yes
preloop agents offboard <agent> --server-only       # remove the Preloop side; the read-only local config stays
preloop agents offboard <agent> --no-login-restore  # do not write the subscription login back
preloop agents starter-policy <mcp-server> [-o file] [--apply]
preloop agents extensions                 # list installed plugins, skills, hooks, and commands
preloop agents extensions --json          # preloop.extensions.v1, identifiers in clear
preloop agents extensions --agent claude-code --kind plugin
preloop agents extensions check --policy-file policy.json
preloop agents extensions check --policy-file policy.json --fail-on unknown
preloop agents extensions check --online  # fetch the policy; sends no inventory
preloop agents onboard --unattended \
  --enrollment-token-file /private/authority.json \
  --apps claude_code,codex \
  --url https://preloop.example.com
```

### What onboarding prints

Onboarding works out the whole plan for an agent before it shows anything.
The review is one row per agent and the disclosures that need your consent:

```text
  1  Claude Code   model routing, MCP, Agent Control
                   model: anthropic/<model> (subscription login)

  ! Preloop will hold and rotate the Claude Code subscription login. Offboard
    returns it.
  ! While routed, Claude Code does not fetch organisation-managed settings or
    claude.ai connectors.
Apply managed Preloop onboarding for Claude Code? (Y/n):
  ok  Claude Code   agent and key created in Preloop
  ok  Claude Code   settings.json updated, backup saved
  ok  Claude Code   Agent Control plugin installed
  ok  Claude Code   test request through the gateway (1.2 s)

✓ Onboarded Claude Code
  Gateway validation: passed (route probe; application behavior unverified)
  Claude Code shows "API Usage Billing"; calls still use your subscription.
  Status: preloop agents status "Claude Code"
  Undo:   preloop agents offboard "Claude Code"
```

Each step is printed once, when it ends. Nothing is redrawn, so the output
reads the same in a terminal, in a pipe, over ssh and in a screen reader.
Warnings and failures are printed in full where they happen.

| Need | Use |
| --- | --- |
| The full credential custody and plan text before confirming, and the detailed result afterwards | `preloop agents onboard <agent> --explain` |
| The same text without changing anything | `preloop agents onboard <agent> --dry-run` |
| The state of an agent later | `preloop agents status <agent>` |

Output is wrapped to the terminal width, between 60 and 100 columns, and to
80 columns when the width is unknown. Colour is used only for failures, only
on a terminal, and never when `NO_COLOR` is set or `TERM` is `dumb`.

### Native approvals are opt-in

`preloop agents onboard Cursor` connects MCP and session reporting without
installing native permission gates. The same explicit opt-in applies to
Claude Code, Codex CLI and Copilot CLI, including discovery and `--yes`:
use `--approvals` to install native gates. Re-onboarding without that flag
removes earlier native gates while retaining session reporting and unrelated
hooks.

With `--approvals`, recognized read-only native tools (Read, Grep, Glob, LS
and the harness's dedicated read/list/search equivalents) do not require a
human unless an explicit rule says otherwise. Rules and the host's own deny
still win, including on first use. Arbitrary shell commands and MCP tools
are not classified as native reads by their names. Mutating and unknown
native calls still escalate when the host has not allowed them. An outage
still denies: reachable-service defaults do not change fail-closed availability.

If a human approval has only email notifications, or no notification channel,
the native hook returns immediately with a denial explaining how to find the
request in **Console > Approvals** or pause the local gate. The tool has not
run. The request remains available in the console until decided or expired;
retrying the tool is a new permission check. The Approvals page shows a warning
with the affected request, including after expiry. Configure mobile or chat
notifications before depending on interactive approvals.

### Console lifecycle and local recovery

**Pause** in the console blocks remote Preloop access and releases the local
native-tool gate to the editor's own permissions. **Resume** restores that gate.
**Decommission** and **Remove** revoke remote access and also release the local
gate, with instructions to run `preloop agents offboard <agent>`. They cannot
edit files on your machine. Offboarding restores those files and removes the
hooks; `restore` and `offboard` work from local enrollment state even if the
server-side agent was removed.

For an explicit administrative kill switch, send
`PATCH /api/v1/agents/<id>` with `{"lifecycle_action":"suspend_block"}` using
an account credential with `manage_agents`. That suspension blocks native
calls as well as remote access until resumed. This is a cooperative local
hook, not an OS security boundary: a machine owner can still explicitly pause
or offboard it. Flow execution credentials never inherit lifecycle release.
Unknown or independently revoked credentials and unreachable servers still
deny; only a server-recorded lifecycle instruction releases a hook.

To recover a Cursor installation that still has its local enrollment backup,
run these in a normal terminal (also supported by CLI v1.0.2):

```bash
preloop agents pause Cursor
preloop agents restore Cursor --yes
preloop agents offboard Cursor --yes --remove-model=no --remove-mcp-servers=no
```

The first command stops the local wait immediately. The second restores the
backup and removes Preloop hooks. The third clears enrollment state and
archives the remote entry if it still exists, keeping account models and MCP
servers. Reload Cursor after restoring its files.

### If approvals cannot reach Preloop

When an approval hook cannot reach Preloop, every tool call is refused, or it waits until the hook gives up. The agent cannot run a tool, so it cannot repair the hook.

Run this in a normal terminal, outside the agent:

```bash
preloop agents pause                  # Claude Code, Codex CLI, Cursor, Copilot CLI, and Devin
preloop agents pause "Codex CLI" --for 30m
preloop agents resume                 # end every pause
preloop agents resume "Codex CLI"     # end that agent's pause
```

With no agent name, `preloop agents pause` pauses the CLI approval hooks for Claude Code, Codex CLI, Cursor, Copilot CLI, and Devin. Name one of those agents to pause only that hook (`Codex CLI` or `codex`). Any other agent is rejected. The pause lasts 1 hour unless you set `--for`. The longest pause is 24 hours. It ends by itself.

The hooks stay installed. The agent's own configuration is not edited. While the pause is active, the hook lets the call through and does not ask Preloop. Claude Code, Codex CLI, Cursor, and Copilot CLI then use their own permission prompts. Devin's hook requires a verdict, so that call is approved. Tool calls made during a pause are not sent to Preloop and are not audited.

`preloop agents resume` ends the pause early. With no agent name it ends every pause. With an agent name it ends that agent's pause. A pause of every hook stays active until you run `preloop agents resume` with no name, and the command says so. `preloop agents status <agent>` prints `Approvals: paused until ...` when a pause applies to that agent.

Only those CLI approval hooks read the pause. Session hooks from the same onboarding still call Preloop. MCP tool calls still go through the MCP firewall. Gateway model calls still go through the gateway. Runtime-plugin harnesses (OpenCode, Pi, and others) do not read the pause; they call Preloop themselves. A flow whose approval hook was started with `--require-flow-credential` ignores the pause and stays governed. A paused Claude Code or Codex CLI hook can still sync its subscription login with Preloop. That sync is separate from the tool call.

### Manual setup for read-only configuration

Before it changes anything in Preloop, `preloop agents onboard` tests whether it can write every file it needs. When a file is read-only (a bind mount in a container, another owner, a read-only root filesystem), onboarding continues in manual mode for Claude Code, and for the main configuration file of Codex CLI and Cursor: it creates or reuses the agent and its key, prints the exact change for you to make, and waits. Other agents stop before any change with a message. The full flow is in [Sandboxed and containerised agents](clients/sandboxed-agents.md).

| `agents onboard` flag | Effect |
|------|--------|
| `--manual` | Print the configuration change for you to apply instead of writing it. Automatic when the agent's configuration is read-only |
| `--manual-output <dir>` | Directory to save the manual setup instructions in (mode 0700; they contain the agent key). Default: `~/.preloop/manual-setup/<agent>/` when `~/.preloop` is writable |
| `--resume` | Finish a manual setup: check the change is in place, make one real request, then report the agent as onboarded. Re-running `agents onboard <agent>` on a pending setup does the same |
| `--resume-token <token>` | The resume token printed by a manual setup. Needed when `~/.preloop` is not writable. It is not a secret |
| `--settings-file <path>` | With `--resume`: check this `claude --settings` file instead of the agent's own settings file |
| `--from-env` | With `--resume`: check the environment-variable form in this shell instead of the settings file |
| `--cancel` | Cancel a manual setup that is waiting for the local change and remove what it created in Preloop |

`--resume` and `--cancel` cannot be combined, and neither can `--settings-file` and `--from-env`.

| `agents offboard` flag | Effect |
|------|--------|
| `--server-only` | Remove the Preloop side even when the local configuration is read-only and still holds the Preloop block. The machine is reported as not offboarded and the command exits non-zero |
| `--no-login-restore` | Do not write the live subscription login back to this machine (for read-only or shared credential files). Sign in again afterwards |

With a read-only configuration and neither flag, `agents offboard` prints the exact removal, changes nothing in Preloop until the block is gone, and prints `Offboarded` only for a file that no longer points at Preloop.

### Unattended enrollment

An MDM or other privileged installer can enroll selected local apps without a person at the machine. The command above is the whole device-side step. It does not discover apps and it does not use an account login.

1. An administrator issues a short-lived, single-use authority with `POST /api/v1/agents/enrollment/authorities` and writes that JSON to a regular file. On Unix the file is mode `0600`, owned by the caller or root, and not a symlink. `--enrollment-token-file -` reads the same JSON from stdin. Windows carries it in a temporary SYSTEM-owned envelope with a restricted DACL and launches without inherited handles across sessions.
2. The device runs `preloop agents onboard --unattended`. Linux requires `--target-user`. A privileged macOS run uses the console user, and Windows uses the single active session. If no interactive user is selected, the run stays pending and does not consume the authority.
3. The CLI exchanges the authority at `POST /api/v1/agents/enrollment/exchange`. That creates separate device, user, and app credentials with explicit MCP tool grants. The authority cannot read account data, report discovery, call MCP, or invoke inference. A revoked credential cannot mint another one.
4. The CLI writes only the MCP config for the named apps and acknowledges it with `POST /api/v1/agents/enrollment/applied`, using the app credential. Claude Code is `~/.claude.json` under `mcpServers` with `"type": "http"`. Codex CLI is `~/.codex/config.toml`. Cursor is `~/.cursor/mcp.json`. Credentials are checkpointed in the target user's OS credential store, so an interrupted apply can resume. An unchanged rerun makes no API call and no config change.

`--apps` accepts `claude_code`, `codex`, and `cursor` (one positional app name works too). `--device-ref` may carry an MDM device reference; only an account-salted hash of it is sent. `--url` must be `https://host` with no userinfo, query, or fragment.

This path configures MCP access only. It does not read or import upstream provider credentials, execute application binaries, or transfer subscription OAuth. Local acknowledgment records that the config was written. Application login and runtime validation stay unknown. A symlinked app config is refused by name; attended onboarding is the path that follows the link and keeps the target. Account tokens, `--token`, profiles, config-file overrides, and attended flags such as `--all`, `--dry-run`, and `--model` are rejected. Live Jamf and Intune acceptance is not verified yet.

`discover --json` emits only allowlisted summaries (`name`, `app_id`, `mcp_server_count`, `auth_state`, `runtime_state`). `agents status <agent> --json` reuses that agent allowlist and adds enrollment health: local apply state, remote lifecycle, validation booleans, and model credential status. It does not emit MCP env, headers, auth, config paths, tokens, or raw local or remote config. `--inventory` writes an offline `preloop.inventory.v1` envelope of known-app presence and aggregate MCP counts, with no credentials, prompts, or network calls.

`agents extensions` lists plugins, skills, hooks, and commands for Claude Code and Codex CLI. It is read-only: no login, no network, and no writes. Identifiers are printed in clear, including names an inventory report would hash. `--agent` repeats `claude-code` or `codex-cli`; any other app id is a usage error. `--kind` is repeatable. `--all-users` follows the same elevation rule as `discover --inventory --all-users`. `--json` prints `preloop.extensions.v1`. A partial scan prints `Scan incomplete: <reason>` and exits 2. The check matches each policy entry on the identifier an inventory report would store, so a name that contains a home-path segment is matched as its `hash:` id while the printed row stays in clear.

`agents extensions check` compares that list with a policy file. Pass exactly one of `--policy-file` (offline) or `--online`. `--online` calls `GET /api/v1/agents/extension-approvals/export` and does not upload inventory. `--fail-on` is `unapproved` (default) or `unknown`. The console Extensions tab downloads the same file.

| Exit | Meaning |
| --- | --- |
| 0 | Nothing at or above `--fail-on` |
| 3 | At least one unapproved extension |
| 4 | At least one unknown extension, and `--fail-on unknown` (3 wins when both are present) |
| 2 | The scan is incomplete, or the policy is unreadable or invalid |
| 1 | Any other error, including `--online` authentication or transport failures |
| 64 | Both `--policy-file` and `--online` were set, or neither was |

The check proves files are present or absent in the locations the collector reads. It does not prove what an agent loaded or ran. See [MDM recipes](../operations/mdm-recipes.md#extension-compliance-check).

`discover --all-users` requires either `--inventory` or an explicit `--report`,
plus root on Linux/macOS or an elevated administrator on Windows. For example,
run `sudo preloop agents discover --inventory --all-users` for an offline scan.
It reads bounded local profile stores: `/Users` on macOS, `/home` and `/root`
on Linux, or local Windows `ProfileList` entries. It does not onboard agents
or prompt for onboarding. Reporting requires a Preloop login or token with
`report_discovery` permission in the elevated process.

All-user output uses `preloop.inventory.v2`, adding `profiles` with keyed
`profile_ref`, `completeness` and optional `error_code`; app and probe entries
carry the matching `profile_ref`. Profile names and paths stay local. Offline
references use a fresh random salt for each run, so they are not stable across
offline scans. Reports use the account's salt and workstation context.

Collection is limited to 64 profiles and 4 MiB per configuration file; a report
includes at most 100 app contexts. Unreadable profiles, oversized or unsafe
files, and limits are represented as failed or partial coverage, so an empty
result is not evidence that the workstation has no agents. These aggregate
profile contexts do not automatically link to later agent enrollments. See
[Agent discovery reporting](agent-discovery-reporting.md#all-user-profile-reports)
for report fields and coverage semantics.

Onboarding runs a direct gateway route/accounting probe by default; skip it with `--skip-live-validate`. The probe reads managed configuration but does not launch the application or verify that it consumed that configuration. Application behavior remains unverified.

`agents reconnect` preserves the existing enrollment, model bindings, policies,
gateway config and backups. It repairs a shared credential once and attaches any
legacy split model rows to it. It only updates subscription models owned by this
machine's enrollment in the selected account. Use `--from-local` after completing
`claude auth login --claudeai` or `codex login`. Expired or incomplete local
credentials are refused. Update the CLI if your installed version does not yet
provide this command.

Live desktop views (view only by default, nothing recorded) are described in
[Agent deployment](../operations/agent-deployment.md#live-desktop-view).

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
- **3**: a single-agent `agents onboard` or `agents offboard` is waiting for a manual step: the configuration is read-only and the printed change has not been applied (or removed) yet. See [Manual setup for read-only configuration](#manual-setup-for-read-only-configuration).

Script against the summary, not just the exit code, when you care about specific agents.

### WSL

If you run the CLI inside WSL but installed agents on the Windows side, onboarding prints:

```text
Hint: Running under WSL: agents installed on Windows are not on the WSL PATH - install the agent inside WSL or add its Windows install dir to PATH.
```

The CLI also runs natively on Windows: `amd64` and `arm64` binaries ship with
every release (install with PowerShell, see [Windows CLI install](../windows-cli.md)).
Running it inside WSL is a supported option too. See
[Windows](../operations/windows-wsl2.md) for setup and the current limits of the
native binary.

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
| Claude Desktop | Yes | Yes: the default onboarding installs the direct route into this device's managed configuration (asks for administrator rights); `--model-route direct` or `apps-gateway` prints it for your MDM ([Claude Desktop](clients/claude-desktop.md)) | n/a |
| Windsurf | Yes | No | n/a |
| VS Code / Copilot | Native user-profile configuration; verify server/session authentication | Manual BYOK: Chat: Manage Language Models -> Add Models -> Custom Endpoint, then `chatLanguageModels.json`. Full URLs end in `/openai/v1/chat/completions`, `/openai/v1/responses`, or `/anthropic/v1/messages`. Set `toolCalling: true`. Business and Enterprise need the admin BYOK policy. See [VS Code Copilot](clients/vscode-copilot.md) | n/a |
| Copilot CLI | Yes (`~/.copilot/mcp-config.json`) | No on onboard. `preloop copilot` launches the CLI with gateway env vars | n/a |
| Antigravity | Yes | No (locked to Google-hosted models) | n/a |
| Devin | Yes | No (inference runs in Cognition's cloud) | n/a |

"MCP firewall: Yes" means tool calls are governed. Automatic gateway rows meter every model call the adapter rewrites. Manual BYOK rows meter the calls the client sends to the gateway URLs above. GitHub-hosted Copilot models and Cursor bundled models stay on their own providers, so cost analytics and model budgets do not apply to that traffic.

Claude Code discovery: the CLI treats `~/.claude.json` or a `claude` binary on `PATH` as install markers (fresh installs have no `~/.claude/settings.json` yet) and bootstraps the canonical `~/.claude/settings.json` during onboarding.

## Policies

```bash
preloop policy list [--limit N] [--output table|json|yaml]   # policy versions, newest first
preloop policy validate <file>
preloop policy apply <file> [--dry-run] [-r]   # -r: recurse into a directory
preloop policy diff <file>
preloop policy export [-o file]
preloop policy apply-stored [--apply] [--since T] [--until T] [--store NAME] [--json]   # apply current sensitive-data rules to stored rows; dry run without --apply
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

## Sessions

```bash
preloop sessions list [--active] [--agent <id|name>] [--kind <kind>] [--since 2h] \
  [--parent <session-id>] [--execution <id>] [--limit 50] [--json | -o id] [--wide]
preloop sessions attach <session-id|short-id> [--execution <id>] [--read-only] [--since 10m] [--json]
preloop agents attach <agent-id|name> [--no-wait] [--read-only] [--since 10m] [--json]
preloop sessions search "<query>" [--from 2026-09-01] [--to 2026-09-15] [--limit 20] [--json]
```

`sessions list` prints your account's runtime sessions, most recently active
first: short id (`--wide` for the full id and the agent kind), agent, started,
last activity, state (`live` for activity in the last 2 minutes, `idle` for
open but quiet, `ended`), tool calls, model calls, pending approvals and a
title. An untitled session is labelled with its agent kind, the base name of
the working directory its hook reported and its start time; if two rows on the
page would still share a label, the short id is appended.

| Flag | Server filter |
| --- | --- |
| `--active` | open sessions with activity in the last 10 minutes |
| `--agent <id\|name>` | one managed agent; an unknown name is refused, and a name shared by several agents asks for the id |
| `--kind <kind>` | `claude-code`, `codex`, `cursor`, `hermes`, ...: managed agents of that kind and sessions recorded from that source |
| `--since <duration>` | active within `30m`, `2h`, `7d`, ... (default: 30 days) |
| `--parent <session-id>` | only the sessions that session spawned |
| `--execution <id>` | only sessions linked to that flow execution |
| `--limit N` | at most N rows (default 50, at most 1000) |

`--json` prints the endpoint's own items (`GET /api/v1/runtime-sessions`), each
with `computed_title` and `state` added, under `{"total": ..., "items": [...]}`.
`-o id` prints one full id per line. The hint line naming
`preloop notes send --session` is printed only to a terminal. To steer the
session you found, see [Finding the session to steer](operator-notes.md#finding-the-session-to-steer).

`sessions attach` follows one session live (model requests, tool calls,
approvals, notes, the end), sends a typed line as an operator note and decides
a pending approval with `a` or `d`. On a managed agent with a live Agent
Control connection the input is in command mode instead: a typed line starts
a new turn and its delivery (queued, delivered, started, finished) is shown
inline. `a` and `d` still decide a pending approval (with nothing pending
they print a notice and send nothing), `/note <text>` still sends a note, and
`/turn <text>` sends that text as a turn. `agents attach` does the same by
agent, attaching its current session or waiting for the next. See
[Attaching to a session from the terminal](sessions-attach.md).

`sessions search` ranks session content by relevance with the same server
query the console uses; `preloop sessions search --help` lists its flags.

## Artifacts

```bash
preloop artifacts put <file|-> --session <id> [--kind <kind>] [--name <name>] \
  [--label key=value ...] [--parent <artifact-id>] [--content-type <type>] [--json]
preloop artifacts ls --session <id> [--kind <kind>] [--label key=value ...] [--since 7d] [--limit 100] [--json]
preloop artifacts get <artifact-id> --session <id> [-o <file>]
```

These commands use the same deposit API as the console and the
`deposit_artifact` MCP tool (`/api/v1/runtime-sessions/{id}/artifacts`, see
[Artifacts](artifacts.md)).

`artifacts put` streams one file (or stdin with `-`) to the session and prints
the artifact id and a console link that opens the session at that artifact.
The media type comes from `--content-type`, then the file extension, then the
first bytes of the file. Stdin has no name to go by, so it needs
`--content-type`. Leave out `--kind` and the server picks one from the media
type: PNG, JPEG and WebP images become `screenshot`, audio becomes `audio`,
video becomes `recording`, `text/vtt` becomes `transcript`, plain text,
markdown and JSON become `document`, and any other file type (PDF, CSV, GIF,
...) becomes `generated_file`. Pass `--kind document` for
a PDF. Labels are `key=value`. Repeat `--label tags=...` to
build the `tags` list. A refusal prints the server's error code as sent, for
example `artifact_too_large (HTTP 413)`.

`artifacts ls` lists one session's artifacts, newest first. The server applies
`--kind` and `--label`. `--since` keeps artifacts created within that window
and stops paging at the first older one. `--session` is required for now,
because listing across every session needs the account-wide artifact search.

`artifacts get` streams the bytes to stdout, or with `-o` to a file. The file
is renamed into place only after the download completes. If the bytes were
evicted or expired, the command prints why (for example
`artifact <id> is no longer available: evicted (HTTP 410)`) and exits non-zero.

`--json` on `put` prints the API descriptor unchanged, including its MCP
`content_block` (a `resource_link`). On `ls` it prints `{"items": [...]}` with
each descriptor unchanged.

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

### Named workers

`preloop sessions list --alias <name>` filters spawn-time aliases. The table
shows ALIAS and PARENT (short ids; `--wide` shows full ids).
`preloop notes send --alias <name>` targets one active worker;
`--external-session-id <id>` targets its harness id.
`--children latest|all` targets live children of the session in
`PRELOOP_PARENT_SESSION_ID`, with the same 20-session cap as MCP `send_note`.
See [Spawning workers](spawning-workers.md) for the complete recipe.

## Notes

Send and inspect operator-note receipts:

```bash
preloop notes send --session <id> --wait 5m "Run the final checks."
preloop notes status <note-id> [--json]
preloop notes list --session <id> [--state pending] [--json]
preloop notes list --agent <id> [--json]
preloop notes list --execution <id> [--json]
```

List requires exactly one target. `--wait` polls the single-note route every
2 seconds for at most the specified positive duration. It exits 0 on delivery,
3 on undeliverable or expired, and 4 on timeout. Status and list show receipt
timestamps and channel; JSON includes the author and note text under the same
read permission as the API. See [operator notes](operator-notes.md#delivery-receipts).

### Managed hook bundle registration

`preloop agents managed-config claude-code --output ./bundle --platform linux
--cli-path /opt/preloop/bin/preloop` exports an offline managed hook overlay with a
SHA-256 policy revision. Add `--register` to record the revision and non-secret
manifest metadata with the selected account. Registration requires
`manage_agents`; reading the registry and generated count requires `view_agents`.
It does not install the bundle or enroll workstations. See
[managed hook exports](clients/claude-code.md#export-a-managed-hook-overlay) for
hash bytes, retry behavior and evidence eligibility.
