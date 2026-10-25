# Endpoint coverage and threat model

Preloop combines bounded endpoint inventory with evidence from configured
clients and routed requests. It does not see every agent on every machine.
Choose the scope of a pilot explicitly: OS, app/version, user/device population,
model route, business MCP service and permitted data. An inventory result is
not proof that every action on that device is governed.

This guide describes merged source at
[`726d71a3899a`](https://github.com/preloop/preloop/tree/726d71a3899af476b1170d9e45a64f7fd544ce26),
reviewed on 2026-10-22. Source references below pin that revision; check the
actual CLI/server versions in your deployment. A source feature or passing
fixture does not establish availability in a particular installer, edition,
vendor tenant or app version. See [discovery reporting](agent-discovery-reporting.md)
for commands and [staged deployment recipes](../operations/mdm-recipes.md) for rollout.

## What each signal proves

Use these four states independently. They are evidence categories, not a
progression in which one implies the next.

| State | Evidence that supports it | Limit |
| --- | --- | --- |
| Installed | A known executable, app bundle or install marker exists in the inspected location | A leftover config alone does not prove an executable is installed or runnable. The collector's `presence: present` can mean either configuration or installation evidence; inspect `evidence_kinds` |
| Configured | A recognized config or route declaration is readable; enrollment records describe configuration attempts | A file or enrollment acknowledgment does not prove login, effective policy, app consumption or a successful controlled action |
| Observed active | A hook, gateway, MCP or runtime-session event records activity on that route in a stated window | Inventory does not observe running processes, prompts or recent use. Offline `usage` and `auth` remain `unknown`; a runner heartbeat alone is not app activity |
| Governed | A particular action reaches a configured policy boundary and its decision is recorded; real-app acceptance verifies the tested behavior | A managed-looking MCP entry, runner `governance` label, enrollment or model-route setting does not verify all native tools, other connectors or alternate accounts |

The [offline collector][inventory-source], [route classifier][routes-source] and
[control assertions][controls-source] keep those distinctions explicit. Offline
control axes are `model_route`, `native_actions` and `managed_mcp`; they distinguish
adapter support from recognized local configuration. Verification stays
`unverified` with `evidence_kind: unavailable`, even if an earlier enrollment
probe passed. A route classified as managed is a recognized configuration
shape, not an authenticated account binding or proof that a request succeeded.

Other sources answer different questions:

| Signal | Supported conclusion | Does not prove |
| --- | --- | --- |
| [Runner harness inventory](agent-discovery-reporting.md#harness-inventory-from-runners) | Bounded installed/configured metadata from a connected runner | Employee use, successful policy enforcement or all of the owner's devices |
| [Gateway](concepts/model-gateway.md), [native approval hook](clients/claude-code.md) or [MCP policy](concepts/safety-layer.md) event | Activity and decisions for requests reaching that configured surface | Off-route API calls, browser activity or native tools outside that hook |
| Expected-device roster joined to reports | Reporting history for a separately declared device population, when the optional fleet service/capability is available | Discovery of unknown devices, hardware attestation or malicious intent behind a missing report |
| [Imported provider usage](copilot-usage-import.md) or [billing reconciliation](integrations/enterprise-billing.md) | Vendor-reported activity/spend for the imported account and period | A device-level event, gateway-metered tokens or prevention of those calls |
| Separately supplied network logs | Destinations and times within that logging system's scope | Prompt content, the actor behind a shared address or endpoint enforcement by this collector |

The [fleet architecture][fleet-source] separates roster, receipt, observation and
credential records. Fresh receipt must not be relabeled verified compliance.
An empty scan applies only to its declared scope; incomplete, failed or stale
collection does not establish absence. Preserve each source's window and
conflicts. No report means unknown, not compliant or malicious.

## Platform scope

The table describes current source paths, not a blanket app support policy.
macOS and Windows have native collector implementations; Linux is best effort.
All rows require readable known locations and the correct execution identity.

| OS | Installed signal | Configured signal | Observed active | Governed signal / boundary |
| --- | --- | --- | --- | --- |
| macOS | Current-user known executable/bundle/marker probes; all-profile scans leave runtime installation unknown | Current-user known configs; elevated local `/Users` profile enumeration | Unknown from inventory; requires separate routed events | Offline verification unverified. Claude Code hook overlay can be exported for macOS; app-user execution and effective managed source need acceptance |
| Windows | Current-user known executable/marker probes; all-profile scans leave runtime installation unknown | Current-user known configs; elevated local registry `ProfileList` enumeration | Unknown from inventory; requires separate routed events | Offline verification unverified. Windows Code overlay can be exported; SYSTEM is not the app user, and live PowerShell/app/MDM acceptance remains unverified |
| Linux, including a CLI running inside WSL | Current-user known executable/marker probes; all-profile scans leave runtime installation unknown | Current-user known configs; elevated `/home` and `/root` enumeration | Unknown from inventory; requires separate routed events | Best effort. Linux Code overlay can be exported; distribution, permissions and session environment need acceptance. A WSL scan does not establish coverage of Windows host profiles or other distributions |

Sources: [platform profile paths and limits][profiles-source],
[Unix profile enumeration][unix-profiles-source],
[Windows profile enumeration][windows-profiles-source] and the
[recipe prerequisite matrix](../operations/mdm-recipes.md#scope-and-prerequisites).
The repository already has a [three-OS discovery CI matrix][matrix-source].
Expanded native-path assertions and isolated fixture runs are **planned** under
[#1342](https://github.com/preloop/preloop/issues/1342), with passing supplemental
results on the still-unmerged [PR #1995](https://github.com/preloop/preloop/pull/1995)
at review time. Those results do not certify a vendor-tenant rollout.

### Collection limits

- All-profile collection requires root on macOS/Linux or administrator on
  Windows, and explicit `--all-users --inventory` or `--all-users --report`.
  It uses known local profile stores, not directory-service or remote enumeration.
- All-profile scans inspect at most 64 profiles and bound configuration reads
  to 4 MiB per file. Reports contain at most 100 application contexts. These
  are collection/report limits, not a promise to cover every installed app.
- Profile scans reject unsafe paths, symlinks, hard-linked configurations and
  special files. They never execute a discovered app or read its credential
  store. They do not search each user's runtime `PATH`; runtime installation
  remains unknown. Failures and limits preserve incomplete scope.
- Portable installs, nonstandard paths, remote homes, containers, other VMs,
  unmanaged/second devices, personal browser accounts and browser AI content
  are outside the inspected scope unless separately evidenced. The collector
  does not perform inline network inspection.
- The managed Code overlay supplies only `PreToolUse`. Desktop routing, model
  routing, direct MCP connectors and native action gates are separate surfaces.
  Unattended enrollment covers selected Code, Codex and Cursor MCP configs;
  it is not authorization to enroll every discovered profile.

See [all-user reporting](agent-discovery-reporting.md#all-user-profile-reports)
for failure states. The [report builder][profile-report-source] sends only
present profile/app contexts, with `installed: unknown`, `configured: true`
when a config parsed successfully (otherwise `unknown`), and `enrolled: false`.
It does not emit active-use or runtime-verification evidence. An empty
candidate list without completeness evidence cannot establish a clean scan.

### Planned work and separate controls

App-level control verification remains **planned** under
[#1182](https://github.com/preloop/preloop/issues/1182); a local configuration
assertion is not its result. A Preloop-owned resident collector is a **gated
plan**, [#1348](https://github.com/preloop/preloop/issues/1348), not a property
of the scheduled CLI collector. Broader signatures are tracked in
[#1341](https://github.com/preloop/preloop/issues/1341).

Existing [Copilot imports](copilot-usage-import.md) supply vendor-reported data.
The additional activity/inventory panels and import-completeness changes in
[PR #1996](https://github.com/preloop/preloop/pull/1996) are **planned**, not
merged behavior at this review. See the public
[endpoint program tracker #1353](https://github.com/preloop/preloop/issues/1353)
for reconciliation and egress-import plans; they are not collector signals.
No device or runtime enforcement should be inferred from a vendor usage total.

Endpoint management, SaaS administration, identity/access controls, billing
reconciliation and network security tools provide complementary evidence.
Their coverage, permissions and retention must be assessed separately. This
guide does not claim a native integration or inline inspection merely because
one of those categories can help close a gap.

## Threat model

The trust boundary is the selected collector scope and the requests that
actually reach a policy boundary. Inventory is a client assertion, not device
attestation. Use the controls below only within their stated authority.

| Threat or blind spot | Detecting or preventing control in scope | Residual risk / separate control to validate |
| --- | --- | --- |
| Local administrator bypass | Recognized config/route probes can expose differences on a later scan; device-manager file permissions can restrict standard-user writes | Admin can replace software, disable hooks, suppress reports or choose another route. No local-admin resistance or signed hardware attestation is established. Validate independently administered resource/provider controls |
| Personal account or alternate client | Hook/gateway/MCP policy evaluates requests reaching its configured surface; imported usage covers the connected vendor account | Personal/browser accounts and off-route calls can be outside both sources. Validate organization identity and resource access policies separately |
| API-to-API usage or remote cloud agent | Gateway events cover routed API calls; separately imported provider records may expose activity/spend | A direct provider/service call bypasses endpoint discovery and gateway policy. Scope service credentials and egress controls independently |
| Browser AI | No browser-content detection by this collector | Browser prompts, uploads and personal sessions remain unknown. Any browser, vendor or network control requires its own authorization and evidence |
| Unmanaged or second device | An optional expected-device roster can expose missing reports for devices already known to it | Unknown devices cannot be inferred from silence. Validate enrollment and resource access requirements separately |
| Stolen collector key | Exactly `report_discovery` limits access to salt/report APIs, subject to permission checks; keys can be revoked independently | A key can submit misleading metadata and obtain the account hash key. A generic report-only key is not bound to a device fingerprint by its scope alone. Use separately provisioned device restrictions where available; hashes and receipt are not attestation |
| Direct MCP connector | Recognized route counts expose configured direct remote/stdio entries; policy controls only calls through the managed entry | Other connector credentials may bypass that entry. Restrict them at the resource and verify a harmless denied action has no downstream effect |
| Offline or suppressed collector | Collection errors and receipt windows expose gaps for declared sources | No report does not mean no activity or deliberate evasion; distinguish outages, scheduling gaps and missing user sessions |
| Higher-priority policy, credential change or app upgrade | Reinspect configuration and repeat real-app acceptance for the changed environment | Delivered JSON may be ignored or superseded. Previous runtime results cannot certify a new configuration or version |

The key boundary is implemented in [scope checks][scope-source] and
[report authorization][endpoint-source]. Device-restricted credentials with
`device_report` context fail with 503 when their authorization service is
absent. This does not add a fingerprint restriction to a generic scoped key.
Use distinct collector and app/runtime credentials; neither belongs in a
static package with account-wide administrator authority.

## Data minimization and retention

These are collector-specific DPIA inputs, not a completed DPIA or a retention
promise for all Preloop data. The purpose is to identify known app/config
contexts and reporting gaps for an authorized device population. Pseudonymous
references, software choices and collection times can still relate to people,
especially when joined to an administrative roster or enrollment.

### Fields and data paths

| Path | Emitted fields and behavior | Important unknowns / exclusions |
| --- | --- | --- |
| Offline `discover --inventory` | `schema`, `observed_at`, collector version/detector version, scope, completeness, probe `app_id`/kind/status/error code, apps with presence/evidence kinds, usage/auth, MCP config/server counts, route category counts/completeness, control support/configuration/verification fields | No network/authentication by this mode; usage/auth remain unknown. Config names and values are inspected locally for classification but not copied into its output |
| Offline `--all-users --inventory` | The same inventory plus hashed `profile_ref` on apps/probes and per-profile completeness/error codes | Fresh random hash key each run; no machine-identity lookup. Output can still be retained or uploaded by your scheduler |
| Current-user `discover --report` | `workstation_fingerprint`, OS family, optional valid CLI version; each candidate has `agent_kind`, `config_path_hash`, `mcp_server_count`, `enrolled` | No collection-window/completeness envelope is emitted by this path. Enrollment observed by the CLI is not runtime governance |
| `--all-users --report` | The candidate fields plus v2 evidence: observation UUID, collector/detector versions, OS, `source_environment: unknown`, start/end times, `scan_scope: [user_config]`, completeness/errors, profile refs/states, app kind/context/profile refs and installed/configured states | No app versions, activity state or controls are emitted by this builder. All-user candidate `enrolled: false` means enrollment was not observed |
| Additional fields accepted by the API | Optional candidate `agent_version`; evidence v1/v2 can additionally carry app version, surface, `observed_active` and independent control assertions (`supported`, `requested`, `applied`, `method`) | Accepted schema fields are not proof that the stock CLI collects them. There is no effective-policy or successful runtime-verification field. Evidence requires an installed `discovery_evidence` service; without it the report returns 503 |

The exact contracts are the [offline DTOs][inventory-source],
[current-user builder][report-source], [all-user builder][profile-report-source],
[request schema][schema-source] and [evidence schema][evidence-source]. The latter
bounds collection windows to one day, profiles to 64, applications to 100 and
control assertions to seven per app. Unknown object fields are rejected.
Agent-kind/version strings have restricted alphabets; they are not all enums.
Validation constrains shape and size, not the truthfulness or sensitivity of
an arbitrary client's schema-valid values.

The shipped serializers omit raw user names, hostnames, home/config paths,
credentials, environment values, prompts, MCP names, URLs, commands and arguments
from these report bodies. This is not a statement about gateway/session data,
HTTP access logs, source IPs, webhook recipients or MDM logs. Assess those
separately. [Runner inventory](agent-discovery-reporting.md#harness-inventory-from-runners)
is also a separate payload, including login state and restricted version/account
host fields, not part of this narrow collector contract.

On receipt, [candidate storage][candidate-source] adds account/row identifiers,
first/last-seen times, status and any managed-agent link. Re-reporting updates
`last_seen_at` only; the other candidate details are not a current-state history.
[Observation storage][observation-source] separately retains validated evidence,
authenticated source reference, observation UUID, end/receipt times and a
SHA-256 content digest for replay consistency. That digest is not an identity
hash or attestation. A new candidate also creates an
[`agent.discovered` event][event-source] with candidate identifiers, keyed refs,
kind/version, MCP count, enrollment flag, OS, status and first-seen time; configured
webhook destinations are additional recipients with their own retention.

### Hashing and identity

The [server issues a random 32-byte, hex-encoded account salt][candidate-source].
The CLI uses that string as the HMAC-SHA256 key, producing lowercase 64-character
hex references. The salt is available to authorized reporters and is not
rotated automatically ([salt model][candidate-model-source]).

| Reference | Input under that key |
| --- | --- |
| Workstation | `preloop-workstation:v1:` plus the trimmed OS machine ID or explicit `--device-ref` |
| Current-user config | `preloop-config-path:v1:` plus the cleaned path, with the home prefix replaced by `~` and separators normalized |
| Reported profile | `preloop-profile:v1:` plus workstation fingerprint, `:` and cleaned home path (or local reference for an unreadable profile) |
| Reported profile/app context | `preloop-profile-app:v2:` plus profile reference, `:` and fixed app ID |

Machine identity uses macOS `IOPlatformUUID`, Windows `MachineGuid`, or Linux
`/etc/machine-id` with `/var/lib/dbus/machine-id` fallback
([Unix][unix-machine-source], [Windows][windows-machine-source]). Current-user
reporting can persist a random fallback in the CLI config directory. All-user
reporting only reuses an existing fallback or requires `--device-ref`; it does
not create one. Offline all-profile references instead use a new random
32-byte hex key and the context `offline` on each run.

Different account keys produce different references, but this does not
anonymize the data or prevent correlation through other identifiers. A holder
of the salt can test candidate identifiers. The current-user config hash can
repeat for the same home-relative path across machines; its workstation key
provides the candidate context. Profile/app hashes use a different namespace
from enrollment config hashes and do not automatically link to current-user
enrollment. Neither an explicit device reference nor an OS identifier attests
physical hardware or the reporting executable.

### Retention and deletion boundaries

| Store | Shipped lifecycle | DPIA implication |
| --- | --- | --- |
| Discovery candidates | Eligible for deletion when `last_seen_at` is older than 90 days, **except `onboarded` candidates still linked to a managed agent** | Linked device records have no 90-day cap. Agent deletion clears the link; a stale candidate becomes eligible for a later purge, not necessarily immediate deletion |
| Source observations | Expire 90 days from server `received_at`; reads filter expired records and purge deletes them | A later report does not refresh an old observation's retention window |
| Purge execution | Background sweeper runs every 24 hours in API/all service roles, including when the evidence plugin is disabled | Deletion depends on a running, successful sweeper. The cutoff is not a guarantee of physical deletion exactly at 90 days |
| Account discovery salt | Stored per account, no automatic rotation or age-based expiry | It is not purged with old candidates; account deletion has a database cascade |
| Local inventory output and fallback identity file | No age-based deletion in these collector paths or example scripts | Configure scheduler/log rotation and endpoint decommissioning separately |
| Roster mappings, runner inventory, event/outbox deliveries, gateway/session/audit stores, backups and exports | Separate lifecycles, outside this discovery purge | No deployment-specific maximum or backup erasure deadline is established here; obtain and test each applicable policy |

Sources: [candidate purge][candidate-source], [observation purge][observation-source],
[sweeper][purge-source], [service roles][roles-source] and
[salt/managed-agent foreign keys][candidate-model-source]. The salt endpoint's
`retention_days: 90` does not override the linked-candidate exception or govern
these other stores.

### DPIA decisions still required

Before a rollout, record the controller/processor roles, purpose and lawful
basis, necessity of all-profile collection, affected people and notice, roles
allowed to view evidence, recipient systems, storage locations, transfers,
retention for each separate store and the deletion process. `view_agents`
permits candidate reads and `manage_agents` permits status changes; marking
ignored hides a candidate and is not erasure. Evidence/fleet service access
must be checked for the deployed edition and role configuration.

Repository source cannot establish your organization's lawful basis, vendor
retention, data residency, backup expiration, scheduler output retention or
actual purge health. Record these as unresolved until supplied by the operator.
Use synthetic data to inspect stored reports and downstream events, verify
expiry and linked-agent deletion, and exercise access/revocation before relying
on the deployment's privacy statement.

## Pilot evidence to retain

Record CLI/server/app versions, effective managed source/hash, collection scope
and freshness, one permitted read, one denied action with no downstream effect,
one approval, credential revocation, outage, alternate route and rollback.
Keep synthetic service data and redact credentials/content from screenshots.
Separate delivery receipts, inventory and actual app actions in the result.
Browser use and unmanaged devices stay explicit exclusions until independently
covered. Follow the [recipe acceptance checklist](../operations/mdm-recipes.md)
without treating fixture checks as vendor-tenant acceptance.

[inventory-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/cli/internal/cmd/agents_inventory.go
[routes-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/cli/internal/cmd/agents_inventory_routes.go
[controls-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/cli/internal/cmd/agents_inventory_controls.go
[profiles-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/cli/internal/cmd/agents_inventory_profiles.go
[unix-profiles-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/cli/internal/cmd/agents_inventory_profiles_unix.go
[windows-profiles-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/cli/internal/cmd/agents_inventory_profiles_windows.go
[report-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/cli/internal/cmd/agents_discovery_report.go
[profile-report-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/cli/internal/cmd/agents_inventory_profiles_report.go
[unix-machine-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/cli/internal/cmd/agents_discovery_machine_unix.go
[windows-machine-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/cli/internal/cmd/agents_discovery_machine_windows.go
[matrix-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/.github/workflows/discovery-parity.yml
[fleet-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/ARCHITECTURE.md#discovery-source-observations
[scope-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/backend/preloop/api/auth/key_scopes.py
[endpoint-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/backend/preloop/api/endpoints/agent_discovery.py
[schema-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/backend/preloop/schemas/agent_discovery.py
[evidence-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/backend/preloop/schemas/discovery_evidence.py
[candidate-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/backend/preloop/models/crud/discovered_agent_candidate.py
[candidate-model-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/backend/preloop/models/models/discovered_agent_candidate.py
[observation-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/backend/preloop/models/crud/discovery_observation.py
[event-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/backend/preloop/services/event_webhooks/emitters.py
[purge-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/backend/preloop/services/discovery_candidate_purge.py
[roles-source]: https://github.com/preloop/preloop/blob/726d71a3899af476b1170d9e45a64f7fd544ce26/backend/preloop/services/service_roles.py
