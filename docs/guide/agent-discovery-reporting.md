# Agent discovery reporting (opt-in)

Editions: OSS, Cloud, Enterprise

`preloop agents discover` scans the local machine for known agent tools
(Claude Code, Cursor, Codex CLI and the rest). By default the scan stays on
the machine: it only reads `GET /api/v1/agents` to mark which agents are
already enrolled. Discovery reporting is the opt-in way to tell Preloop which
agent tools exist on workstations that were never onboarded, so they show up
in the console under **Agents > Not yet governed**.

For the per-OS distinction between installed, configured, observed active and
governed, read the [coverage guide](endpoint-coverage.md#platform-scope).
Its [collector data and retention inventory](endpoint-coverage.md#data-minimization-and-retention)
includes DPIA inputs and the limits of report-only credentials.

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

Here, the scope limits API access; it does not itself bind the key to one
workstation fingerprint. Provision a distinct credential per device and check
any additional device restrictions supplied by your deployment. See the
[stolen collector key threat](endpoint-coverage.md#threat-model).

## What is sent

| Field | Content |
| --- | --- |
| `workstation_fingerprint` | HMAC-SHA256 of the OS machine id or explicit `--device-ref`, keyed with a per-account salt the server issues. The raw identifier is not sent. Different account salts produce different references; these remain pseudonymous, not anonymous. |
| `cli_version` | The CLI version. |
| `os` | OS family only: `darwin`, `linux`, `windows` or `other`. |
| `candidates[].agent_kind` | The product kind, for example `cursor` or `claude_code`. |
| `candidates[].config_path_hash` | For current-user discovery, HMAC-SHA256, same salt, of the config path with the home directory replaced by `~`. All-user reports use a separate keyed profile/app context hash, described below. |
| `candidates[].mcp_server_count` | How many MCP servers the config defines. A count, not names. |
| `candidates[].enrolled` | Whether the CLI already saw an enrollment for it. |

The API also accepts optional `candidates[].agent_version`; the current CLI
report builder does not populate it. Optional evidence fields and the exact
hash inputs are documented in the [collector field inventory](endpoint-coverage.md#fields-and-data-paths).

The machine id is `/etc/machine-id` on Linux, `IOPlatformUUID` on macOS and
`MachineGuid` on Windows. When none is available current-user reporting
generates a random id once and keeps it in the CLI config directory. All-user
reporting reuses an existing fallback or requires `--device-ref`; it does not
create an identity file.

## What is never sent

The shipped report serializers omit user names, hostnames, home or config paths
in clear, prompts, API keys or other credentials, environment variables, MCP
server names, URLs, commands or arguments. The server rejects unknown fields
and constrains hashes, enums,
counts and agent-kind/version strings. This validates shape, not the truth or
sensitivity of an arbitrary client's schema-valid values. The narrow report
body does not establish what HTTP access logs, session records or downstream
webhook recipients retain. See the [DPIA boundaries](endpoint-coverage.md#dpia-decisions-still-required).

## What the server keeps

One `discovered_agent_candidate` row per (account, workstation fingerprint,
agent kind, config path hash), with `first_seen_at`, `last_seen_at` and a
status: `new`, `onboarded` or `ignored`.

- A new row fires the [`agent.discovered`](webhooks.md) webhook once. A
  re-report of the same tool only moves `last_seen_at` and emits nothing.
- For current-user discovery, every enrollment write for an agent from the
  same workstation (applied, validated or failing validation, for any agent
  kind including MCP-only and IDE-config agents) carries the same two hashes,
  and the matching candidate becomes `onboarded`, linked to the managed
  agent. A restore carries no link. When no report was ever sent for that
  install, the enrollment records an `onboarded` candidate itself (the two
  hashes plus the OS family and CLI version, nothing else), so onboarding
  alone places the agent on its device in the device fleet. Re-onboarding
  updates the same row.
- Agents enrolled by an older CLI have no such link. The fleet map and the
  agent page then place the agent on a device by evidence already held, never
  rewriting it: first a runner-reported harness of the agent's kind on a
  device its owner uses, when exactly one managed agent fits; otherwise the
  agent's enrollment hostname, when exactly one device has that hostname
  (compared case-insensitively, ignoring `.local` or a domain, with `-`, `_`
  and spaces alike) and the agent's owner, when known, uses that device.
  Ambiguous matches stay unplaced.
- **Mark ignored** in the console hides a candidate. **Copy onboard command**
  copies `preloop agents onboard <kind>` to run on that workstation.
- Candidates nobody has reported for 90 days are deleted by a daily purge,
  except `onboarded` candidates still linked to a managed agent: those are
  the agent's device link. Deleting the agent clears the link and makes a
  stale candidate eligible for a later purge. The
  [retention table](endpoint-coverage.md#retention-and-deletion-boundaries)
  distinguishes expiry from actual deletion and other data stores.

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
`schema_version: 1` or `2`. An installed evidence service consumes it within the same
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

## All-user profile reports

Use `preloop agents discover --all-users --report` from an elevated process
(root on Linux/macOS, administrator on Windows). `--all-users` requires an
explicit `--report` or `--inventory`; the environment variable alone does not
select all-user reporting. The elevated process needs the reporting token or
login. For an offline scan, use `--all-users --inventory`; this makes no network
calls and prints a `preloop.inventory.v2` envelope. Both modes print safe
inventory JSON and do not onboard agents.

The scan enumerates local profiles from `/Users` on macOS, `/home` and `/root`
on Linux, or Windows `ProfileList`. It covers the known application registry,
not every possible install location or network/directory-service profile.
It scans at most 64 profiles, reads at most 4 MiB per configuration file, and
reports at most 100 application contexts. Symlinks, hard-linked configurations,
special files and unsafe configuration paths are refused. Configuration files
are opened relative to the selected profile with link checks at open time;
the collector never executes a discovered application or reads its credential
store. Reaching a limit or failing to read a profile leaves the
coverage incomplete rather than treating that scope as an empty successful scan.

All-user reports send `evidence.schema_version: 2` on the existing report
endpoint. Version 1 reports remain supported. Version 2 adds:

| Field | Content |
| --- | --- |
| `evidence.profiles[].profile_ref` | A keyed reference scoped to the account and workstation. It identifies profile scope without sending a profile name or home path. |
| `evidence.profiles[].completeness` | `complete`, `partial` or `failed`, including profiles that yielded no application rows. |
| `evidence.profiles[].error_code` | Absent for complete scope; otherwise `profile_unavailable`, `profile_unreadable` or `probe_incomplete`. |
| `evidence.applications[].profile_ref` | The reference of the profile in which this app context was observed. |

Offline v2 output also associates probe results with `profile_ref`. Its fresh
per-run random salt makes profile references unrelated across offline runs.
Reported references use the account salt and workstation context instead.

If no profiles can be enumerated, the inventory has `completeness: failed` and
`scope.error_code: profiles_unavailable`. An unreadable Windows `ProfileList`
entry produces its own hashed failed profile, and collection continues for
other profiles. Unsafe registry paths (including network paths, alternate data
streams and traversal) also produce a failed profile without being opened.
If enumeration returns readable profiles alongside an error, those profiles
are scanned and the inventory is partial with `profiles_unavailable`. On Linux,
`/root` is still scanned when `/home` cannot be enumerated.
A profile limit produces partial
inventory with `scope.error_code: profile_limit`. The report retains enumerated
profile states even when no apps were readable; its evidence records partial
or failed completeness and an `unavailable` error. A report truncated at 100
apps is also partial. Incomplete scope never proves absence of agents.

All-user candidates represent aggregate profile/app contexts. Their
`config_path_hash` uses the `preloop-profile-app:v2:` namespace, while
current-user enrollment uses `preloop-config-path:v1:`. Consequently these
candidates do not automatically become `onboarded` when a current-user agent
is later enrolled, and their emitted `enrolled: false` means enrollment was
not observed. Automatic linking requires a future profile-aware enrollment
contract. App presence and configuration observations do not prove runtime
enforcement or use.

## Administrative device identifiers

For a fleet roster with an explicit administrative identifier, use
`preloop agents discover --report --device-ref synthetic-device`. This replaces
OS machine-id discovery for the report fingerprint. The identifier is stripped
of surrounding whitespace, then hashed with the same account salt and
`preloop-workstation:v1:` namespace; the raw value is never sent. It does not
turn reporting on without `--report` or the existing opt-in environment flag.
Matching hashes correlate administrative identifiers and do not attest a device,
its executable identity or effective governance. Candidate-only reports establish
receipt history, not collection completeness or trusted control verification.

`--all-users --report --device-ref synthetic-device` uses this same identifier
for both the workstation fingerprint and its profile references. All-user
reporting never creates a fallback identity file: if the OS identifier is
unavailable, it reuses an existing CLI workstation id or fails with a request
to supply `--device-ref`. Offline inventory does not look up machine identity.
macOS identity lookup uses the fixed system `ioreg` path, and Windows reads
`MachineGuid` directly from the registry, so inherited `PATH` cannot select a
program to execute with collector privileges.

## Deployment and coverage

See the [MDM recipes](../operations/mdm-recipes.md) for staged delivery and separate
credential provisioning, the [threat model](endpoint-coverage.md#threat-model)
for bypass risks, and [planned work](endpoint-coverage.md#planned-work-and-separate-controls)
for features that must not be treated as shipped coverage.
