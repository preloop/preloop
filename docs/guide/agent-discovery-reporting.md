# Agent discovery reporting

Editions: OSS, Cloud, Enterprise

`preloop agents discover` scans the local machine for known agent tools
(Claude Code, Cursor, Codex CLI and the rest). By default the scan stays on
the machine: it only reads `GET /api/v1/agents` to mark which agents are
already enrolled. Discovery reporting tells Preloop which agent tools exist on
a workstation, including ones that were never onboarded, so they show up in
the console under **Agents > Not yet governed**. A manual `agents discover`
reports only when asked to. `agents onboard` sends the same report by default,
and an installed resident runner also reports automatically.

## Reporting during onboarding (on by default)

When `preloop agents onboard` has set up at least one agent, it sends one
report for the machine: the same request, with the same contents and limits,
as `preloop agents discover --report`. The review says so before you confirm,
in one line, and lists "extension report on" among the options:

```text
  ! Extension report: sends the names, versions, content hashes, descriptions
    and authors of installed plugins, skills, hooks and commands, then their
    redacted text for review. Off: --no-report-extensions
```

| To | Use |
| --- | --- |
| Turn the report off for one run | `preloop agents onboard --no-report-extensions`, or answer `c` in the review |
| Turn it off for every run on a machine | `PRELOOP_DISCOVERY_REPORT=0` (also `false`, `no`, `off`) in the environment |
| Keep the report and not upload extension text | `--no-extension-content`, `PRELOOP_NO_EXTENSION_CONTENT=1`, or the account setting (see [Extension content](#extension-content)) |

The default also applies under `--yes` and `PRELOOP_CONFIRM=1`; the plan and
the line above are printed before anything is applied. `--unattended`
enrollment does not send the report: it holds an enrollment token, not a
login, and cannot call the report endpoints.

The report needs the `report_discovery` permission. When the signed-in role
does not have it, or the request fails for any other reason, onboarding prints
one line ("extension report not sent: ...") and finishes normally. A failed
report never fails onboarding and is not retried.

There is no account-wide switch for the report itself. An account can turn off
the upload of extension text; the collector asks before uploading and honours
that.

For the per-OS distinction between installed, configured, observed active and
governed, read the [coverage guide](endpoint-coverage.md#platform-scope).
Its [collector data and retention inventory](endpoint-coverage.md#data-minimization-and-retention)
includes DPIA inputs and the limits of report-only credentials.
Reported devices appear on [Device fleet](device-fleet.md), which can alert
when an enrolled agent goes quiet or an expected control is no longer seen.

## Turning it on for discovery

Either pass the flag or set the environment variable:

```bash
preloop agents discover --report --json --no-onboard-prompt
PRELOOP_DISCOVERY_REPORT=1 preloop agents discover --json --no-onboard-prompt
```

Without one of them the manual scan sends no discovery report. With `--json`
the report confirmation goes to stderr so stdout stays valid JSON.

With closed or non-terminal stdin, discovery never offers or performs onboarding,
including with `--report`, `--yes` or `PRELOOP_CONFIRM=1`. Text output explains
that onboarding was skipped and directs you to run `preloop agents onboard`
from a terminal. JSON and inventory output retain their machine-readable format.
Use `--no-onboard-prompt` to suppress onboarding in an interactive terminal too.

## Automatic runner reports

A resident runner reports once after a randomized 30-second to five-minute startup
delay, then about every 24 hours with up to 10% jitter. A successful report is
remembered across restarts, so restarting a service does not immediately report
again. One-shot ephemeral runners do not collect discovery.

Automatic reports use the same read-only collector and privacy rules as manual
reports. They include installed agents, including agents not onboarded, and their
extensions. They never prompt, enroll an agent, install an extension, execute an
agent, or rewrite an application's configuration. The only local writes are
runner scheduling and status records and the one-time content-notice marker.

Configure the device in the Preloop config file:

```yaml
runner:
  discovery:
    enabled: true
    interval: 24h
```

Extension description opt-out applies to manual and automatic reports alike:

```yaml
discovery:
  no_extension_details: true
```

The equivalent foreground flag is `preloop runner fg --no-extension-details`.
Content follows the [extension content contract](#extension-content), including
redaction and all account, device and per-extension opt-outs. Use
`discovery.extension_content: false`, `PRELOOP_NO_EXTENSION_CONTENT=1`, or
`preloop runner fg --no-extension-content` to omit content. The first upload
prints its disclosure in the runner service log before sending. A failed content
query or upload uses the same retry schedule; already stored hashes are not resent.

Set `enabled: false` to stop automatic discovery on that device. An explicit
`PRELOOP_DISCOVERY_REPORT=0` (also `false`, `no` or `off`) in the service environment
also opts out. The runner reads
this setting every minute. Intervals are bounded to one hour through seven days;
an invalid duration uses 24 hours. `preloop runner status` shows the last attempt,
last successful report, result, and the opt-out instruction, even while offline.

Administrators with `manage_policies` can read or replace account controls at
`GET` / `PUT /api/v1/account/runner-discovery`:

```json
{"enabled": false, "interval_seconds": null}
```

`enabled: false` also rejects incoming automatic reports. A non-null
`interval_seconds` overrides device intervals and must be 3600 through 604800.
Null lets devices use their configured interval. Policy is refreshed every five
minutes; a failed policy fetch prevents collection and retries with backoff.
Device opt-out always wins. These controls leave explicitly requested manual
reports available.

Failures retry after one minute, doubling with jitter up to one hour. The runner
does not spool inventories on disk. It collects fresh evidence on a retry and
keeps the last successful timestamp. An older server without runner reporting
support fails closed and is retried; it never falls back to a user's login token.

The runner uses its own credential. Its inventory capability grants only
`report_discovery` on its policy, inventory and content upload routes, scoped to
its account and workstation. Content negotiation is limited to hashes this device
has already reported. It has no content read permission. It cannot list candidates or read another device's reports. Token
rotation and removal revoke this reporting access too.

The standard launchd agent, systemd user service and Windows logon task scan the
user who runs them. An administrator-installed elevated service scans all local
profiles through the existing bounded all-users collector. Unreadable profiles,
probe failures and scan limits remain incomplete evidence, never proof of absence.

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

Such a key can call `GET /api/v1/agents/discovery-salt`,
`POST /api/v1/agents/discovery-reports` and the two
[extension content](#extension-content) upload routes,
`POST /api/v1/agents/extension-content/missing` and
`POST /api/v1/agents/extension-content`, and nothing else: it cannot read
stored content. Every other REST
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
`MachineGuid` on Windows. When none is available, reporting reuses an existing fallback identity or requires
`--device-ref`. The read-only collector never creates an identity file.

## What the report never carries

Since the release that added [extension content for review](#extension-content),
a report from the current user is followed by a second upload: the redacted
text of the extensions it lists. That upload is on by default and has three
opt-outs. Read that section before turning reporting on. It replaces the
earlier statement on this page that no command string and no file content
leaves the host.

The discovery report itself still omits local profile names, hostnames, home or
config paths in clear, file bodies, credential files, environment values,
headers, source URLs, hook command strings and arguments. MCP server identifiers
are sent in clear like other extension identifiers; their definitions are hashed,
and MCP bodies are not uploaded by the content pass. V4 adds the
bounded authored metadata described under [Extensions](#extensions), including
descriptions, author names, hook matchers and executable basenames. This is a
new data class: an extension author could put sensitive text in a description.
The privacy filter is not a general secret detector. Use
`--no-extension-details` when authored metadata must stay local. The server rejects unknown fields
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
  re-report of the same tool updates `last_seen_at` and its reported enrollment
  state, preserves `first_seen_at`, and emits no new discovery event. The console
  shows **First seen** and a **New this week** badge for candidates first seen
  within seven days that are still not onboarded.
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
| `GET /api/v1/runners/{id}/discovery-policy` | Runner token with inventory capability |
| `POST /api/v1/runners/{id}/discovery-reports` | Runner token, own device only |
| `POST /api/v1/runners/{id}/extension-content/missing` | Runner token, own reported hashes only |
| `POST /api/v1/runners/{id}/extension-content` | Runner token, own device only |
| `GET` / `PUT /api/v1/account/runner-discovery` | `manage_policies` |
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
| `generated_profile` | Name of the host execution profile the runner generated (`copilot`, `cursor`, `devin`, `claude`, `codex`, `opencode`), or null when a hand-written profile wins. |
| `capabilities` | Host execution capability flags. |
| `setup_step` | The onboarding step that keeps a `presence_only` harness from running flows, for example the `preloop agents onboard "Codex CLI" --approvals` command. Absent once the harness is runnable. |

Codex CLI and OpenCode get a generated profile and `flows_only` support once
the executable is on the runner's `PATH`, the login is verified (an API key
variable or the harness's own auth file) and the Preloop approval integration
is installed. A flow with agent type `codex` or `opencode` then routes to the
runner when `agent_config.harness` names `codex_cli` or `opencode`; without
it the flow keeps running hosted. `preloop runner inventory` prints the
missing step as `next step:` until then.

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
| `evidence.applications[].profile_ref` | The reference of the profile in which this app context was observed; omitted for machine-level managed configuration. |
| `evidence.applications[].config_scope` | `user_config` (default) or `managed_config`. |
| `evidence.applications[].controls[]` | Allowlisted axis, adapter support, configured state, and configuration source. No execution or verification claim. |

All-user reporting also inspects the standard Claude Code system
`managed-settings.json` on Linux, macOS and Windows. It reports the presence
or absence of the exported Preloop PreToolUse hook as a separate machine-level
application context with a salted config-path hash and `managed_config` scope.
The absence of the file or hook is configuration evidence only. Unreadable or
malformed files mark managed configuration incomplete and the control unknown.
`evidence.scope_completeness` keeps the original user-config scan status separate
from the managed-config status. The overall report retains its most severe state
and accumulated error codes. This does not
inspect higher managed sources or establish the effective policy in the app.
The inventory envelope contains no file bodies or command strings. All-user
reports can upload redacted extension content in the separate content pass,
subject to the same account, device and per-extension opt-outs.
Offline profile inventory remains limited to profile configuration.

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

## Extensions

V3 reports distinguish extension collection coverage from runtime probes through
`extension_completeness`. Unverified runtime usage does not make an otherwise
complete extension inventory partial. Unreadable profiles, bounded scans and
failed extension probes still report incomplete coverage.

Reports preserve fixed extension failure codes in `extension_errors`. The
console uses the latest device report's collector version and reasons to explain
incomplete coverage. For `extension_project_unscanned`, run
`preloop agents discover --report --project .` from each affected repository.
Upgrade advice appears only when the reported collector is older than the
supported extension collector version. Older reports that omitted specific
reasons remain unknown; missing reasons do not imply an outdated collector.

`preloop agents discover --inventory` emits `preloop.inventory.v4`, including
installed extensions for Claude Code and Codex CLI. `--report` sends the same
metadata on each candidate, bound to the matching app's hashed config context.
Servers continue accepting v1, v2 and v3. On an explicit schema-version
rejection, a v4 CLI retries with v3 after removing `details` from both candidate
and evidence records. If the server also rejects v3, the existing v2 retry
removes extensions and managed configuration. Other validation failures do not
trigger a downgrade. Older CLIs continue to work against the new server.

Extension identifiers (plugin, skill, command, MCP server and hook event names, versions,
and marketplace names) are sent in clear. Marketplace and git URLs, hook
commands, file contents and paths are not part of this inventory record. Hook
commands and file text are uploaded separately as
[extension content](#extension-content) unless that is turned off; URLs and
home paths are not. Local/git identifiers containing a
profile login or home path segment, and project identifiers with that personal
information, become `hash:<16 hex characters>` with `id_hashed: true`.
Marketplace plugin identifiers remain unchanged. Source locations are represented
only by SHA-256 hashes; project references use the config-path hashing scheme
(keyed with the account salt when reporting).

| Agent | Kind | Known locations below the profile home | Collected metadata |
| --- | --- | --- | --- |
| Claude Code | Plugin | `.claude/plugins/installed_plugins.json` (version 2), `known_marketplaces.json`, installed content under `cache/<marketplace>/<name>/<version>/` | Name and marketplace, manifest version (falling back to record version), description, author name, component counts, source hash, enabled state |
| Claude Code | Skill | `.claude/skills/<name>/SKILL.md` | Directory identifier, front matter name and description, invocation, script count, tree hash |
| Claude Code | Hook | `hooks` in `.claude/settings.json`; JSON event mappings in `.claude/hooks/` | Event, matchers, command counts, executable basenames, Preloop classification, aggregate hash |
| Claude Code | Command | `.claude/commands/*.md` | File stem, front matter description, slash invocation, file hash |
| Claude Code | MCP server | User `mcpServers` and project-local `projects.<root>.mcpServers` in `.claude.json`; `.mcp.json` in known or explicit project roots | Server identifier, scope, configuration hash; no commands, arguments, environment values, headers or URLs |
| Codex CLI | Plugin | `.codex/plugins/cache/<marketplace>/<name>/<version>/`, `[plugins]` in `.codex/config.toml` | Name and marketplace, manifest version, description, author name, component counts, source hash, enabled state |
| Codex CLI | Skill | `.agents/skills/<name>/`, legacy `.codex/skills/<name>/`, and `.codex/skills/.system/<name>/` | One row per skill directory, front matter name and description, invocation, script count, tree hash |
| Codex CLI | Hook | `.codex/hooks.json`, `[hooks]` in `.codex/config.toml` | Event, matchers, command counts, executable basenames, Preloop classification, aggregate hash |
| Codex CLI | MCP server | `[mcp_servers.<name>]` in `.codex/config.toml`, including explicit project roots | Server identifier, scope, configured enabled state, configuration hash; no commands, arguments, environment values, headers or URLs |

Claude local MCP entries use project scope, matching local plugin reporting.
A local entry overrides a same-name project entry. User entries retain their
own rows. Claude MCP enabled state remains unknown unless explicitly disabled;
configured presence does not prove approval or execution. MCP hashes cover the
canonical server definition, including private values without sending them in clear.

MCP content hashes use the same unkeyed SHA-256 comparison scheme as hook
commands and skill files, so offline policy checks and pins agree across
devices without fetching an account key. They do not send cleartext definitions,
but they are not encryption: someone with the rest of a definition and its hash
can test guesses for a low-entropy password or token. Treat exported hashes as
sensitive metadata. Rotating a credential in `env`, an argument, a header or a
URL changes the hash and makes an existing content pin report `content_changed`.
Account-keyed hashes would require a different offline-policy contract; the
current contract intentionally retains portable content hashes.

The plugin layouts were verified with Claude Code 2.1.332 and Codex CLI 0.172.0
using their plugin managers. Claude's install registry is authoritative: unused
cache entries and marketplace catalogs are not installs. The undocumented
`plugins/store/` layout is not collected. Codex selects the `local` cache version
when present, otherwise the highest version (SemVer for full semantic versions,
lexical order otherwise), and reads portable `plugin.json`, then
`.codex-plugin/plugin.json`, then `.claude-plugin/plugin.json`.

An unreadable registry produces `extension_unreadable`; invalid JSON or an
invalid registry shape produces `extension_malformed`; an unrecognized Claude
registry version produces `extension_unsupported_version`. These appear as
extension probe reason codes and make the app partial. Unsupported install
scopes produce `extension_unsupported_scope`. Plugin content uses the same
bounded tree hash as other directory extensions. A missing or oversized content
tree has an explicit hash status rather than an invented hash.

Collection uses the default locations below each profile's home. Relocated
agent homes, remote account-only plugins, and runtime plugin activation are not
established by this scan.

For the current user, project roots listed in `~/.claude.json` or explicitly
included with the repeatable `--project` flag are also scanned for
`.claude/skills/`, `.claude/commands/`, `.claude/hooks/`, and
`.claude/settings.json` and `.claude/settings.local.json`. Unknown projects are not searched. A project plugin's
registry entry does not register its root for the skill/command/hook walk. If
that root, or the current directory's `.claude` directory, is outside the selected
roots, collection is partial (`extension_project_unscanned`) in both the local
listing and the console's report evidence. The listing exits 2 and explains how
to include the current directory without modifying Claude's configuration:

```sh
preloop agents extensions --project .
preloop agents discover --inventory --project .
preloop agents discover --report --project .
```

Use the same project selection for local checks and reporting. `--project` cannot
be combined with `--all-users`; the all-users scan remains profile-local.
Completeness covers the supported locations and selected roots, not all projects
on disk or extensions loaded at runtime. Unrelated discovery candidates do not
change extension completeness for Claude Code and Codex CLI.

Claude registry records with `project` or `local` scope retain their own
installed version and a hashed project reference; their project settings and
local settings override user settings. A project's explicit plugin setting
also produces a project-scope record for a user install, unless that project
has its own install. Without an applicable setting, enabled state is `unknown`.
All-users inventory scans profile-local locations
only. Plugin-bundled skills also appear as skill records with the plugin
identifier in `details.plugin`; hooks and commands within a plugin contribute
to its component counts. Explicit `--project` roots also include Codex's
`.agents/skills/` and legacy `.codex/skills/` directories.

```json
{
  "kind": "plugin",
  "id": "reviewer@claude-plugins-official",
  "version": "1.4.0",
  "source_kind": "official_marketplace",
  "source_ref_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "scope": "user",
  "enabled": "true",
  "id_hashed": false
}
```

Optional fields are `project_ref_hash`, `content_hash`, `event` and `count` (the
last two occur only on hooks). `enabled` is the string `true`, `false` or
`unknown`. Sources are `official_marketplace`, `third_party_marketplace`, `git`,
`local`, `builtin` or `unknown`. Only Claude's `claude-plugins-official` and
Codex's `openai-curated-remote` are classified as official marketplaces.

### Descriptive metadata, enabled by default

Each v4 extension can carry one optional `details` object:

| Field | Source |
| --- | --- |
| `name`, `description` | Skill `SKILL.md` YAML front matter; command front matter supplies its description; plugin manifests supply their description. Bodies are never used as description fallbacks. |
| `description_withheld` | True when the privacy filter removes the description. |
| `invocation` | Generated from the harness, skill or command name and invocation controls; hook records name the event. |
| `script_count` | Number of regular files in a skill's `scripts/` directory, recursively. |
| `plugin` | Parent plugin identifier for a bundled skill. |
| `author` | The plugin manifest's `author.name`, or string-form author. Email and URL fields are excluded. |
| `bundles` | Counts of skills, commands, hooks, MCP servers and agents in conventional plugin locations and safe manifest-declared locations. |
| `hook_matchers` | Per matcher: sanitized matcher text, command count, and one `{executable, preloop_managed}` classification per command. Private matchers have `matcher_withheld: true`. |

All authored text is checked against the case-insensitive home-path segment
rule used for local identifiers before truncation. A matching description is
omitted and marked withheld. Other unsafe text is omitted. Text containing a
path or URL is also withheld. Controls and format characters are stripped,
whitespace is collapsed, and each text field is limited to 300 Unicode
characters. The server validates lengths, text shape, allowed fields and
counts; it cannot independently know a device's private login or home path.

Claude Code skills use `/name` (plugin skills use `/plugin:name`).
`user-invocable: false` removes the slash form; `disable-model-invocation: true`
removes the model-initiated form. Codex skills use `$name` or the `/skills`
picker, including `$plugin:name` for plugin skills. Codex's
`agents/openai.yaml` `policy.allow_implicit_invocation: false` removes the
model-initiated form. These are declared invocation mechanisms, not proof that
a skill was loaded or that current runtime policy permits it. See the
[Claude Code skill reference](https://code.claude.com/docs/en/skills),
[Codex skill invocation examples](https://developers.openai.com/blog/eval-skills),
and [Codex plugin namespaces](https://github.com/openai/codex/blob/main/codex-rs/ext/skills/src/loader/namespace.rs).

A hook classification contains only the executable basename, such as `bash`,
`python3`, or `preloop`. Shell arguments and script paths are never included.
Unclassifiable executable tokens become `unknown`. `preloop_managed` identifies
a `preloop` executable invoking a known Preloop hook subcommand; it is not a
signature check of that executable. No hook is executed during collection.

Scope remains `user` or `project`. Project locations remain hashed in this
release because no account permission to disclose repository basenames is
part of this contract. No full or partial location path is uploaded.

To omit the entire descriptive object while retaining identifiers and hashes:

```sh
preloop agents discover --report --no-extension-details
preloop agents discover --inventory --no-extension-details
```

The flag also applies to `--all-users`. Reporting itself is still opt-in.
Nullable JSONB on `discovered_extension` keeps kind-specific fields in one
extensible structure, with strict typed validation before CRUD persistence.
Both device and aggregate reads return `details`; an aggregate uses the latest
matching device report. Expand the row to see devices, scopes, project hashes,
component counts and content hashes. Search matches identifier, front matter
name and description across the selected inventory.

### Read full content locally

```sh
preloop agents extensions show skill careful --agent claude-code
preloop agents extensions show hook PreToolUse --agent claude-code
preloop agents extensions show plugin reviewer@community --agent claude-code
```

The first line states `Local only: nothing is sent to Preloop.` This command
prints full skill or command text, hook command strings, plugin manifest details
and the local file list, within the collector's existing bounded reads. It
never authenticates, uploads, calls a model, or executes an extension. Use
`--project .` in the repository for project extensions. The console provides a
copyable command for each row. Report-hashed identifiers can also be resolved
locally. Finding and displaying the requested item exits 0 even when another
part of the inventory scan is incomplete. A note names unscanned project roots
and the `--project` flag needed to include them. A missing requested item still
fails. `extensions check` prints the same project notes while retaining exit 2
for an incomplete scan. MCP `show` prints metadata and file locations only.
Terminal control characters are displayed as escapes; newlines and tabs are
preserved so inspecting an extension cannot execute terminal escape sequences.

### Content hashes

Every extension record carries `content_hash` and `content_hash_status`
(`ok`, `too_large` or `unreadable`). The inventory record carries only the
final hash of content, never a file list or a body; the descriptive front
matter fields above are a separate, bounded class. With
[extension content](#extension-content) on, the file list and the redacted
text behind that hash are uploaded separately, addressed by the same hash.

| Kind | Hash input |
|---|---|
| Skill | Tree hash of every file under the skill directory, including scripts and references beside `SKILL.md` |
| Command | SHA-256 of the command file bytes |
| Hook | SHA-256 over the sorted per-command hashes (below) |
| Plugin | Tree hash of the plugin's install directory |

The tree hash lists one entry per regular file, `<relative path>\0<lowercase
hex sha256 of the bytes>\n`, with `/` as the separator on every OS. A symlink is
never followed; it contributes `<relative path>\0symlink\n`. The pin therefore
covers the link's name but not where it points: re-pointing a link, or changing
the file it points to, does not change the hash. Entries are sorted
by path as raw bytes, concatenated, and hashed: `sha256:<lowercase hex>`. Changing
one byte of any file or renaming a file changes the hash; directory listing order
does not.

A tree hash reads at most 500 entries (files and symlinks), 16 MiB in total, and
directories 8 levels below the extension root. It enumerates at most 2,000
directory entries of any type, directories included. Beyond a bound, `content_hash` is
null and the status is `too_large`. An unreadable file or directory, a special
file, or a file whose size changes while it is read gives `unreadable` (no retry).

For hooks the collector hashes each command locally, sorts the `sha256:<hex>`
values, joins them with newlines (no trailing newline), then hashes that string.
Neither commands nor intermediate hashes leave the device.

A hash shows that the files changed. It does not prove an agent loaded those
files. What changed is shown by the console's content diff when content is
stored for both hashes.

### Pinned approvals

An approval with no pins is a name approval: any content of that extension is
approved. Pinning makes it a pinned approval: only the pinned hashes are approved.
Each device row reports an `effective_state`:

| Approval | Pins | Device hash | `effective_state` |
|---|---|---|---|
| none | | | `unknown` |
| `unapproved` | | | `unapproved` |
| `approved` | none | any | `approved` |
| `approved` | some | pinned | `approved` |
| `approved` | some | not pinned | `content_changed` |
| `approved` | some | null | `content_unverified` |

The lifecycle: approve this exact content (a `PUT` with `pin`), or approve by name
and pin hashes later. When a device reports new content it reads
`content_changed` until someone with `manage_agents` pins the new hash. Deleting
the last pin returns the approval to a name approval. Changing the approval to
`unapproved` keeps its pins; approving again restores them. An approval holds at
most 50 pins.

The upgrade that adds pins pins every existing `approved` decision to the hashes
its devices reported at that moment (at most 50, most widely installed first), so
content that changes afterwards reads as `content_changed` instead of staying
approved. An approved extension with no reported hash stays a name approval.
Collectors from this release hash skills and plugins over their whole directory,
so a skill or plugin reported by an older collector reads as `content_changed`
after the device upgrades until its new hash is pinned.

At most 200 extensions are emitted per app context, with a 4 MiB per-file cap,
64 profiles and 100 app contexts. Directory enumeration is bounded to 256 entries.
Symlinks are refused, discovered agents are never executed, and `--inventory`
remains offline without writes. Exceeding a bound, unreadable/malformed metadata,
or a standalone hook script with no declared event makes the scan partial and
emits a fixed error code. Unbound scripts are not assigned invented events.

The server reconciles each device/profile/agent only after a complete scan of its
extension locations. Missing extensions become `present=false` and leave the
default lists; partial scans preserve previously observed rows. Historical v1/v2
reports do not reconcile extensions. Discovery retention removes inventory with
its source observation after 90 days; account approval decisions survive.

`GET /api/v1/agents/discovery-extensions` lists present inventory, with filters
`fingerprint`, `agent_kind`, `state`, and `source_kind`. `state` filters on the
per-device `effective_state`. Device rows carry `effective_state`,
`content_hash` and `content_hash_status`. `group=extension` returns
account aggregates with distinct device counts, versions, source kinds,
`pin_count`, and `content_hashes`: up to 20 `{content_hash, status,
device_count, pinned}` buckets, most common first (ties by hash), where
`device_count` counts distinct present device/profile pairs (the console labels
it "device profiles"). The aggregate's `effective_state` is the worst per-device
state among the rows it counts (`content_changed`, then `content_unverified`,
then `approved`), so it follows the active filters and does not depend on the
20-bucket cut. All null hashes share
one bucket. Every pinned hash is listed, with a count of 0 when no device reports
it.
`GET /api/v1/agents/extension-approvals` lists durable decisions. Both use `limit`
(default 50, maximum 500) and an opaque `cursor`, returning `next_cursor` until
all results have been read. Approval is `unknown` when no decision exists.

`GET /api/v1/agents/extension-approvals/{agent_kind}/{extension_kind}/{extension_id}`
reads one decision with `view_agents` permission, or returns 404 when no decision
exists. The console uses this lookup to preserve notes and version constraints
without scanning the account's approval list.

`GET /api/v1/agents/extension-approvals/export` requires `view_agents` and returns
`preloop.extension-policy.v1`: a hashed account reference, a generation time, and
one entry per approval row (`agent_kind`, `extension_kind`, `extension_id`,
`approval_state`, `version_constraint`, `content_hashes`). Notes, user ids, and
device data are omitted. `content_hashes` lists that row's pinned hashes, oldest
first. An approval with no pins exports an empty list, which the check treats as
a name approval. The console Extensions tab offers the same document as
**Download policy file**.
`preloop agents extensions check --policy-file` evaluates it offline;
`--online` fetches this route and does not upload inventory.

`PUT /api/v1/agents/extension-approvals/{agent_kind}/{extension_kind}/{extension_id}`
requires `manage_agents` and accepts `approval_state` (`approved`, `unapproved`,
`unknown`), optional `note`, and optional `version_constraint`. A constraint is an
exact version or a prefix ending in `.*`; null means any version. An optional
`pin: {content_hash, version, note}` with `approval_state: approved` creates the
approval and its pin in one transaction. The list shows
the stored name decision, not a claim that a particular version complies.
Approvals apply account-wide, not per device. Inventory and approvals do not
block an extension from loading or running.

`POST .../{extension_id}/pins` with `{"content_hash": "sha256:...", "version":
null, "note": null}` pins a hash and `DELETE .../{extension_id}/pins/{pin_id}`
removes one; both require `manage_agents`. A malformed hash or an approval that
is not `approved` returns 422, a missing approval or a pin of another approval
404, and a duplicate or a 51st pin 409. Approval reads include `pins: [{id,
content_hash, version, note, pinned_by_user_id, pinned_by, pinned_at}]`.

In the console, the Agents toolbar's **Extensions** entry lists distinct extensions
with device counts and versions seen. A managed agent's detail page has an
**Extensions** tab (`?tab=extensions`) with the same list filtered to that agent's kind (for
example `claude_code`) across the account, not to one device, and the count in
its tab label after first opening. Device detail shows per-profile inventory,
source/project hash prefixes, scope and enabled state. Both views filter by
effective state (including **Content changed** and **Content unverified**) and
source. A pinned approval shows **Pinned (n)**; **Content hashes** expands the
observed hashes with device counts, who pinned each and when, and **Pin this
content** or **Unpin** (Unpin asks first, and says when it would return the
approval to a name approval). A device row whose content is not approved offers
**Approve this exact content**. Users with `manage_agents` can change an approval;
other viewers see the decision and badges without an editor or pin actions. The table warns when collection
is partial or unknown, and **Load more** follows the server's cursor.

The Agents toolbar offers **Extensions** with the account-wide count in list,
cards and canvas views. It opens the same inventory without changing view mode;
closing and reopening keeps the loaded rows. The agent detail tab loads on
first selection, retains its rows between tab switches, and links directly to
this account inventory.

## Extension content

Editions: OSS, Cloud, Enterprise

An approver cannot judge "skill careful" or "hook PreToolUse (1 command)" from
a name and a hash. After a manual or automatic runner report, the collector also uploads the text behind each reported content hash. This is
**on by default** and is a different data class from everything above: it is
text written by extension authors, and it can contain credentials, internal
host names and customer data. The sections below say exactly what is sent, what
is done to it first, how to turn it off and who can read it.

### What is uploaded

| Kind | Uploaded |
|---|---|
| Skill | The list of files in the skill directory (relative path, SHA-256, size) and the text of `SKILL.md` and of the other text files beside it |
| Command | The text of the command file |
| Hook | Each command string with the matcher it runs under. Both pass through redaction |
| Plugin | The list of files in the install directory, and the text of the manifest (`plugin.json`) and of everything under `skills/`, `commands/` and `hooks/`. Other files are listed by name only |

Never uploaded, and marked in the file list with the reason:

- a binary file (not valid UTF-8, or containing a NUL byte): `binary`;
- a symlink, which is never followed: `symlink`;
- a file over 256 KiB: `too_large`;
- a file that contains a private key block: `private_key`;
- a file that changed or could not be read while it was collected: `unreadable`;
- text beyond the per extension cap: `over_budget`.

All-user reports (`--all-users`) collect content only from the local profiles
covered by their inventory, with profile-local scope and the same opt-outs.
Offline `--inventory` never uploads content. Extensions whose content hash could not be computed (`too_large`,
`unreadable`) have no content either.

### Size caps

| Cap | Collector | Server |
|---|---|---|
| One file or hook command | 256 KiB | 256 KiB |
| Text of one extension | 1 MiB and 200 files with text | 1 MiB, 500 listed files, 200 hook commands |
| One report | 8 MiB, sent in requests of at most 2 MiB and 50 extensions | 8 MiB and 100 extensions per request |
| One account | | 256 MiB of stored text; uploads past it are refused |

Collector and server upload budgets include UTF-8 file contents, hook commands
and each incoming matcher once. The account storage budget also includes the
server's retained matcher history, including its copy of the current assignment.
A refresh charges
only the change in stored bytes, so reporting unchanged content remains possible
at the cap. Rejected updates leave the previously stored content intact.

The numbers were checked against a developer home with 63 skills and 7
plugins: the largest `SKILL.md` was 85 KiB and the 90th percentile 52 KiB, the
largest plugin held 612 KiB of text in 61 skill, command and hook files, and
all `SKILL.md` files together were 1.5 MiB. An extension that does not fit in
one report is sent by the next one, smallest first. The server enforces its
own caps and does not trust the collector's.

### Content is addressed by the inventory hash

Content is stored under the `content_hash` the inventory already reports, once
per account. Before uploading, the collector asks the server which reported
hashes it lacks (`POST /api/v1/agents/extension-content/missing`) and sends
only those, so unchanged extensions are uploaded once per account, not once
per device or per run.

The server recomputes the hash from what it receives: the tree hash from the
uploaded file list, the command hash from the file digest, the hook hash from
the per-command hashes. An upload that does not reproduce its hash is refused.
Text that was not redacted must also match its own SHA-256, and is then shown
as **Matches its hash**. Text that was redacted cannot be checked (the hash is
of the original bytes) and is shown as redacted. The first upload of a hash
wins. A later one replaces it only when it is better evidence and loses
nothing: it has text where the stored row has none, or it is fully verified
where the stored row is not and carries at least as many texts, or it carries
more texts and is at least as well verified. A row that holds no text is asked
for again on every report. Content is accepted only for a hash that the same
device's inventory reports, and a credential bound to one device is held to
that device on both content routes by the same authorizer that checks its
discovery reports.

#### What a hook hash covers

A hook's content hash is computed from its command strings. The matcher, which
decides when a command runs, is not in that hash. So a matcher can be widened,
or differ between two devices, while the hash and any pin on it stay the same.
The console says this on every hook: **a pin on a hook hash covers the
commands, not the matchers.** To make a change visible, a hook is asked for
again on every report (it is small), and every distinct assignment of matchers
to commands reported for a hash is kept, the newest 10, with the time it was
last reported. When more than one exists the console lists them all.

### Redaction

Redaction runs in the collector, before upload. It is deterministic: the same
file always produces the same text. Each finding is replaced by a typed
placeholder such as `[REDACTED:github-pat]`, and the file record carries the
count per type. The content hash stays the hash of the original bytes.

| Rule set | What it covers |
|---|---|
| gitleaks default rules, version 8.30.1 (the version this repository's own secret scan pins), 221 rules | Known credential formats: cloud provider keys, source forge tokens, chat and payment tokens, JWTs and others, with gitleaks' keyword filters, entropy floors and its value allowlists. Its path allowlists (`node_modules/`, lock files, images) are not applied: a file name never switches a rule off |
| `authorization-header` | The value of `Authorization`, `Proxy-Authorization`, `X-Api-Key`, `X-Auth-Token`, `X-Access-Token`, `Api-Key`, `Cookie` and `Set-Cookie` headers, after any scheme word (`Bearer`, `Basic`, `OAuth`, a vendor's own) |
| `url-credentials` | The `user:password` part of any URL, up to the last `@` before the host, and a long token used as the user part |
| `secret-assignment` | The value assigned (`=`, `:=`, `=>`, `:`) to a name that contains `secret`, `token`, `password`, `passphrase`, `pwd`, `api_key`, `access_key`, `private_key`, `signing_key`, `encryption_key`, `session_key`, `credential`, `authorization`, `auth_key`, `dsn` or `connection_string`, whatever its entropy. Type annotations (`password: str = "..."`), string prefixes (`b"..."`), triple quotes, backticks and escaped quotes are understood |
| `high-entropy` | A bare token of 24 or more characters with letters and digits and Shannon entropy of at least 4.0, or 32 or more hexadecimal characters |
| `home-path` | The home directory of the reporting user and any `/Users/<name>`, `/home/<name>` or `C:\Users\<name>` prefix |

A file with a private key block (PEM `-----BEGIN ... PRIVATE KEY`, matched
loosely so that odd spacing or a wrapped header line still counts, or a PuTTY
key file) is not uploaded at all, redacted or not. The server refuses such
text too if a modified collector sends it.

Values that only refer to a secret are kept so the text stays readable:
`$TOKEN`, `${TOKEN}`, `$(command)`, `<token>`, `{{ template }}`, `%VAR%`,
`true`, `false`, `null`. Bracketed forms must be closed: `<S3cret` is a value.
After `name:` alone on a line, words that describe a field are kept too
(`string`, `required`, `optional` and similar).

**Redaction reduces the chance that a secret is stored; it is not a
guarantee.** Known limits:

- A secret with no recognisable format, no secret-looking name beside it and
  low entropy is not found. A password in a sentence is an example.
- In the middle of a sentence, after a colon and without quotes
  (`the password: hunter2x is old`), a value is redacted only if it has 8 or
  more characters and a digit, or 16 or more characters with entropy of at
  least 3.5. This keeps prose readable. `name: value` alone on a line is
  always redacted.
- A value that starts with `$` and looks like a variable name (`$ecret123`)
  cannot be told from a variable reference and is kept.
- Secrets that are split across lines, encoded (base64 of a JSON document,
  for example), or built at run time are not found.
- Internal host names, customer names and other sensitive text that is not a
  credential are not redacted.
- File names are sent as they are.
- The hash of the original file is sent with the redacted text. Someone who
  can read both could test guesses for a short, low-entropy redacted value.
- Redaction can remove text that was not a secret, such as a long checksum.

If any of this is unacceptable for an extension, a device or the account, use
one of the opt-outs below.

### Turning it off

| Level | How | Effect |
|---|---|---|
| Account | **Settings > Account > Extension content > Collect extension content for review** (`PUT /api/v1/account/extension-content/settings`, permission `manage_policies`) | The server refuses content with `409 extension_content_disabled`, tells collectors `collect: false` so they read and send nothing, and deletes every content stored for the account in the same transaction. Both are audited |
| Device | `--no-extension-content`, `PRELOOP_NO_EXTENSION_CONTENT=1`, or `discovery.extension_content: false` in the CLI config | The collector makes no content request at all |
| Extension | An empty `.preloop-no-content` file in the skill or plugin directory, or a pattern in `discovery.extension_content_exclude` | The collector sends a marker with no text; the console says the extension is excluded on the device. Excluding a plugin also excludes the skills it bundles |

Exclusion patterns are globs on the extension name, optionally prefixed with
the kind:

```yaml
discovery:
  extension_content: true
  extension_content_exclude:
    - "skill:internal-*"
    - "hook:*"
    - "plugin:*@private-marketplace"
```

A `.preloop-no-content` file is part of the directory, so adding it changes the
content hash once. Commands and hooks have no directory of their own; exclude
them with a pattern.

### Notice on the device

The first time a device is about to upload content, the collector prints, once
and before sending, what will be sent, what is redacted, how long it is kept
and each way to turn it off. With `--json` the notice goes to stderr. The
notice is recorded in the CLI config directory (`extension-content-notice`).
A scheduled job sees it in its first run's output.

### Upgrading an account that reported before this release

Nothing is collected retroactively and nothing changes for an existing
deployment until a collector is upgraded:

- Content is uploaded only by a CLI that has this feature, and that CLI prints
  the notice above before its first upload. An older CLI keeps sending the
  report it always sent, and the server accepts it. Its rows have no content.
- A current CLI against an older server finds no content routes and sends
  nothing. It does not print the notice and does not fail the report.
- The console shows account administrators a one-time banner on the
  Extensions tab that names the setting and links to it. Dismissing it, or
  changing the setting, is recorded.
- The migration creates an empty table and touches no existing row.

### Storage, retention and who can read

- **Scope.** One row per account, kind and hash in `extension_content`.
  Identical hashes in two accounts are two rows; a hash another account holds
  reads as absent and is uploaded again.
- **Encryption.** The document (file list, text, hook commands) is encrypted
  with the deployment's Fernet key (`SECURITY__ENCRYPTION_KEY`), the same key
  and primitive that protect session artifacts and stored credentials. The
  plain columns hold counts only.
- **Retention.** Content is deleted 90 days after the last report that carried
  its hash, or sooner when the account's plan keeps history for fewer days. It
  is also deleted as soon as no inventory row and no approval pin of the
  account refers to its hash, which is what happens when the discovery purge
  removes the inventory. A pin keeps content only inside the retention window;
  afterwards the pin remains and the console says the text is gone. Deleting
  the account deletes its content.
- **Readers.** `GET /api/v1/agents/extension-content/{kind}/{content_hash}`
  needs `manage_agents`, the permission that gates approvals. Viewers with
  `view_agents` see names, hashes and states, not content. A `report_discovery`
  key can upload and can never read. Every read writes an
  `extension_content.read` audit row with the reader, kind and hash.
- **Logs.** No route logs content or returns it in an error. Validation errors
  on the content routes are a fixed message without the rejected input.

| Method and path | Permission |
|---|---|
| `POST /api/v1/agents/extension-content/missing` | `report_discovery` |
| `POST /api/v1/agents/extension-content` | `report_discovery` |
| `GET /api/v1/agents/extension-content/{kind}/{content_hash}` | `manage_agents` |
| `GET /api/v1/account/extension-content/settings` | `view_agents` |
| `PUT /api/v1/account/extension-content/settings` | `manage_policies` |

Audit actions: `extension_content.read`, `extension_content.settings_updated`,
`extension_content.deleted`, `extension_content.notice_acknowledged`.

### Reading content in the console

On the **Extensions** tab, **Content hashes** lists the hashes seen for an
extension. **Read content** on a hash shows what a device uploaded for it:

- a skill or command as rendered Markdown, with **Show raw** for the source;
- hook commands in a code block, each with its matcher;
- a plugin as a file tree with a viewer.

Each redaction is stated with its type and count, and each file without text
says why. A Markdown file whose rendered form would leave something out (an
HTML comment, a raw HTML element, an image, a link title or a link that is not
`http`, `https` or `mailto`) opens as raw source with a note that says so: an
agent reads the whole file, so the reviewer is shown the whole file. When a pinned hash differs from the one being read, **Compare with
pinned** shows a line diff between the two. **Approve this exact content** pins
the hash on screen.

Extension text is written by a third party. The console renders it as text, or
as Markdown through an allowlist: headings, paragraphs, lists, code, tables and
links to `http`, `https` and `mailto` only. Images, raw HTML, scripts, styles,
forms and frames are removed, so opening a document cannot run code, load a
remote resource or draw over the console.
