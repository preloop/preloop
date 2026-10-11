# Agent discovery reporting (opt-in)

Editions: OSS, Cloud, Enterprise

`preloop agents discover` scans the local machine for known agent tools
(Claude Code, Cursor, Codex CLI and the rest). By default the scan stays on
the machine: it only reads `GET /api/v1/agents` to mark which agents are
already enrolled. Discovery reporting is the opt-in way to tell Preloop which
agent tools exist on workstations that were never onboarded, so they show up
in the console under **Agents > Not yet governed**.

## Turning it on

Either pass the flag or set the environment variable:

```bash
preloop agents discover --report --json --no-onboard-prompt
PRELOOP_DISCOVERY_REPORT=1 preloop agents discover --json --no-onboard-prompt
```

Without one of them nothing about the scan leaves the machine. With `--json`
the report confirmation goes to stderr so stdout stays valid JSON.

## Device-scoped token for scheduled runs

Reporting needs the `report_discovery` permission. Owner and admin roles have
it, as does any role that holds the control-plane bundle. For an MDM job that
runs on every workstation, create an API key whose only scope is
`report_discovery`:

```bash
curl -X POST https://preloop.example.com/api/v1/auth/api-keys \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "workstation discovery", "scopes": ["report_discovery"]}'
```

Such a key can call `GET /api/v1/agents/discovery-salt` and
`POST /api/v1/agents/discovery-reports` and nothing else: every other REST
route and every console WebSocket refuses it with `403 api_key_scope_denied`.
Run the job with `PRELOOP_TOKEN=<key>` and `PRELOOP_DISCOVERY_REPORT=1`.

## What is sent

| Field | Content |
| --- | --- |
| `workstation_fingerprint` | HMAC-SHA256 of the OS machine id, keyed with a per-account salt the server issues. The raw machine id is never sent. Another account's salt gives an unrelated value, so fingerprints cannot be joined across accounts. |
| `cli_version` | The CLI version. |
| `os` | OS family only: `darwin`, `linux`, `windows` or `other`. |
| `candidates[].agent_kind` | The product kind, for example `cursor` or `claude_code`. |
| `candidates[].config_path_hash` | HMAC-SHA256, same salt, of the config path with the home directory replaced by `~`. The user name in the home path never enters the hash. |
| `candidates[].mcp_server_count` | How many MCP servers the config defines. A count, not names. |
| `candidates[].enrolled` | Whether the CLI already saw an enrollment for it. |

The machine id is `/etc/machine-id` on Linux, `IOPlatformUUID` on macOS and
`MachineGuid` on Windows. When none is available the CLI generates a random
id once and keeps it in its config directory.

## What is never sent

User names, hostnames, home or config paths in clear, prompts, API keys or
other credentials, environment variables, MCP server names, URLs, commands or
arguments. The server enforces this too: the report schema accepts only hex
hashes and enumerated values and rejects unknown fields, so a client that
tries to send any of these gets `422` and nothing is stored.

## What the server keeps

One `discovered_agent_candidate` row per (account, workstation fingerprint,
agent kind, config path hash), with `first_seen_at`, `last_seen_at` and a
status: `new`, `onboarded` or `ignored`.

- A new row fires the [`agent.discovered`](webhooks.md) webhook once. A
  re-report of the same tool only moves `last_seen_at` and emits nothing.
- When an agent from the same workstation is onboarded and its enrollment
  validates, the CLI sends the same two hashes with the validation and the
  matching candidate becomes `onboarded`, linked to the managed agent.
- **Mark ignored** in the console hides a candidate. **Copy onboard command**
  copies `preloop agents onboard <kind>` to run on that workstation.
- Candidates nobody has reported for 90 days are deleted by a daily purge.

## API

| Method and path | Permission |
| --- | --- |
| `GET /api/v1/agents/discovery-salt` | `report_discovery` |
| `POST /api/v1/agents/discovery-reports` | `report_discovery` |
| `GET /api/v1/agents/discovery-candidates?status=new` | `view_agents` |
| `PATCH /api/v1/agents/discovery-candidates/{id}` (`{"status": "ignored"}`) | `manage_agents` |

`GET /api/v1/agents/discovery-candidates` returns `{items, total, truncated}`.
`items` is the newest 500 matching rows. `total` counts every match, and
`truncated` is true when the fleet is larger than that page, so the console
can say it is showing the first 500.

## Harness inventory from runners

Separately from this opt-in report, a connected
[runner](runners/quickstart-linux.md#harnesses-are-detected-automatically)
publishes the harnesses installed on its host with its registration and
heartbeat. It is stored on the runner (`harness_inventory`) and shown on the
Runners page. Every entry has these fields:

| Field | Content |
| --- | --- |
| `harness` | `copilot_cli`, `cursor_cli`, `claude_code`, `codex_cli`, `opencode`, `gemini_cli`, `claude_desktop` or `vscode_copilot`. |
| `display_name` | Product name. |
| `version` | Harness version, when the runner could read it. Visible to the runner owner and account admins only. |
| `login_state` | `signed_in`, `signed_out`, `unknown` or `not_applicable`. Never a token or user name. |
| `login_source` | `env` (a token variable is set; only its name was checked), `stored`, `cli_status`, `none` or `unknown`. |
| `account_host` | Host of the account (for example `github.com`), GitHub harnesses only. Visible to the runner owner and account admins only. |
| `governance` | `governed` (Preloop hooks and approval hook), `partial` (usage hooks or MCP only), `ungoverned` or `unknown`. |
| `support_level` | `flows_and_sessions`, `flows_only` or `presence_only`. |
| `enabled` | False when the host turned the harness off in `runner.json`. Disabled harnesses are still listed. |
| `sessions_enabled` | Set on the host only. |
| `session_mode` | `resume`, `stream`, `replay` or `none`. |
| `billing` | `seat` (subscription, not metered by the gateway), `metered` or `unknown`. |
| `models[]` | Up to 64 `{id, source}` pairs; `source` is `probed`, `configured`, `static` or `observed`. |
| `generated_profile` | Name of the host execution profile the runner generated (`copilot`, `cursor`), or null when a hand-written profile wins. |
| `capabilities` | Host execution capability flags. |

The inventory carries at most 32 entries and a `sha256:` hash of its entries.
Executable paths, arguments, environment values and user names never leave
the host. Each change is audited as `runner.harness_inventory_changed`.

## Versioned source observations

The existing report accepts an optional `evidence` envelope with
`schema_version: 1`. An installed evidence service consumes it within the same
report transaction; without that service the extension returns 503, while
legacy reports continue unchanged. No second collector endpoint is introduced.

Observations preserve enumerated scan scope/errors, timezone-aware collection
windows, completeness, safe app/version facts and independent control
assertions. Exact replay is idempotent per authenticated source and workstation;
changed replay conflicts. Sources and collection windows remain separate,
including empty scans. Retention is 90 days from receipt; the always-on
discovery sweeper purges expired observations even if the evidence plugin is
disabled or the source stops reporting. None of these records
prove device identity, app runtime enforcement or use. Collector assertions
cannot carry effective or successful runtime-verification fields.
