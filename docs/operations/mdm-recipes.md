# Staged MDM deployment recipes

Use existing endpoint tooling to deliver a reviewed Claude Code hook overlay,
then verify the actual application before expanding the pilot. These recipes
use plain file/script delivery. They do not require a Preloop signed PKG or MSI;
those artifacts are not supplied here ([packaging work](https://github.com/preloop/preloop/issues/1343)).
No OS, MDM vendor or Claude plan is assumed for an organization.

Before selecting a rollout population, review the
[per-OS coverage boundaries](../guide/endpoint-coverage.md#platform-scope) and
[collector fields, hashing and retention](../guide/endpoint-coverage.md#data-minimization-and-retention).
Include scheduler output and downstream recipients in the deployment's DPIA;
the server's discovery purge does not rotate MDM logs or local inventory files.

## Scope and prerequisites

Source contracts reviewed on 2026-10-15 at Preloop commit
`1f64f12fd8eaae54e562ec4a00b08a0c9a71cb2c`. The latest published release checked
was v0.16.0: the examples require a reviewed candidate build containing these
commands. Check `preloop agents managed-config claude-code --help` and
`preloop agents discover --help` on the exact binary you distribute. Record its
version and SHA256 and the server version. Source availability does not mean
that a published installer includes the command.

| Recipe | OS/app scope | Required tooling and version check | Local checks | Tenant completion |
| --- | --- | --- | --- | --- |
| Jamf Pro file/script | macOS; Claude Code | Jamf Pro script/package permissions; pinned macOS, Code and CLI versions; readable system managed file | Shell/export fixtures | Pending |
| Fleet GitOps/script | macOS; Linux best effort; Claude Code | Fleet script execution enabled and team configuration privileges; pinned Fleet/orbit and OS versions | Shell/export fixtures; YAML parse | Pending |
| Intune macOS | macOS; Claude Code | Supported Intune macOS enrollment and management agent; selected execution identity | Shell/export fixtures | Pending |
| Intune Windows | Windows; Claude Code | Supported Intune enrollment/management extension; local CLI `.exe`; PowerShell hook field supported by pinned Code | Windows export JSON only; PowerShell execution unverified | Pending |
| Kandji script | macOS; Claude Code | Supported enrolled device and Custom Script library item; pinned agent/OS versions | Shell/export fixtures | Pending |

Record the exact app version in the pilot record; no minimum app version or
vendor entitlement is certified by these fixtures. Review the vendor's
[managed settings](https://code.claude.com/docs/en/managed-settings) and
[hook reference](https://code.claude.com/docs/en/hooks) against that version.
Another managed source can supersede the delivered file. A root-only unreadable
file is not an effective policy: the app user must be able to read it.

The `claude-code` hook overlay supplies only `hooks.PreToolUse`. The separate
[extension restrictions](#extension-restrictions) recipe exports a second drop-in. It does not configure model
routing, MCP allowlists, OTel, enrollment, Codex or Desktop. For a Desktop pilot,
use the separate [Desktop adapter guide](../guide/clients/claude-desktop.md),
record its effective configuration and run the acceptance checklist below;
do not deliver this Code file to Desktop. Broader verified adapters remain
subject to [application verification](https://github.com/preloop/preloop/issues/1182).

## Ownership and credentials

| Item | Owner / execution context | Boundary |
| --- | --- | --- |
| CLI and system managed file | Endpoint administrator; protected from standard-user writes | Delivery is not runtime enforcement or local-admin resistance |
| Existing managed keys and higher-priority policy | Organization/vendor policy owner | Preserve their keys; reconcile conflicts before installation |
| Hook credential | Each app user/device through approved onboarding | Separate from discovery and model-provider authority |
| Discovery report credential | Provisioner distributes a distinct least-privilege credential per device | `report_discovery` only; no model or tool authority |
| Single-use enrollment authority | Trusted provisioning service, never a static package | Selected MCP apps only; not the hook credential |
| Server resource credentials and rules | Preloop/resource administrator | Protect only requests that reach that controlled route |

Never embed an admin token, shared fleet secret or personal OAuth bundle in a
script, package, GitOps file or vendor output. Issue credentials off-device in
the approved provisioning system, deliver them through a protected secret
channel, and rotate/revoke them independently. Keep admin authority there.
See [report fields and scoped permissions](../guide/agent-discovery-reporting.md).
Do not copy the guide's administrative key-creation step onto endpoints.

Provision hook credentials separately using the approved
`preloop agents onboard "Claude Code" --approvals` workflow, with the user's
credential-custody choices reviewed before rollout. This attended workflow is
not a mass OAuth-import recipe. Missing credentials deny under the hook adapter
contract; test how the real app handles errors and timeouts.

The [unattended enrollment command](../guide/cli.md#unattended-enrollment)
already supports explicitly selected Claude Code, Codex and Cursor MCP configs.
It does not provision this hook overlay, model routing or Desktop. No active
user means pending, not successful enrollment. All-profile discovery does not
authorize enrolling every user. Live Jamf-root and Intune-SYSTEM enrollment
acceptance is still unverified.

## Prepare one reviewed artifact

Download the [offline exporter wrapper](../assets/mdm-recipes/export-overlay.sh)
and use your approved candidate binary. This wrapper never installs anything:

```sh
sh export-overlay.sh /path/to/reviewed/preloop ./review-bundle darwin /opt/preloop/bin/preloop
```

The output parent must exist; an existing bundle is refused. For Windows,
export with `windows` and target path `'C:\Program Files\Preloop\preloop.exe'`.
For Linux, use `linux`. Inspect all three output files and record their hashes.
The generated 300-second timeout must exceed the separately configured approval
wait budget plus process headroom; change the reviewed wrapper if necessary.

| OS | Reviewed file destination |
| --- | --- |
| macOS | `/Library/Application Support/ClaudeCode/managed-settings.json` |
| Windows | `C:\Program Files\ClaudeCode\managed-settings.json` |
| Linux / WSL | `/etc/claude-code/managed-settings.json` |

Before delivery, export the existing effective policy through its owner and
record its source, hash, permissions and backup. If any managed file already
exists, **do not overwrite it with the exported overlay**. Have the policy owner
merge only the approved hook entries into that source, preserving existing
hooks and unrelated keys. Review the whole resulting document and its rollback
diff. If policy cannot be reconciled, stop. Templates deliberately do not
implement an automatic merge or broad managed-directory deletion.

Deliver the approved binary separately using existing software distribution;
verify architecture/hash and the executable path. Deliver the reviewed JSON as
an administrator-owned file readable by app users (typically root:wheel 0644 on
macOS, root:root 0644 on Linux, administrator/SYSTEM write and user read on
Windows). Restrict parent directories against user replacement. No secrets
belong in this world-readable file. Restart Code and inspect `/status` for the
effective managed source before testing actions.

## Jamf Pro: plain delivery first

1. In a pilot-scoped Jamf policy, use the organization's existing package or
   [script delivery](https://learn.jamf.com/en-US/bundle/jamf-pro-documentation-current/page/Scripts.html)
   to install the approved CLI and reviewed file. Record policy frequency and
   receipt; don't infer success from policy assignment alone.
2. Run privileged file delivery separately from user credential provisioning.
   For recurring user inventory, deploy an organization-reviewed per-user
   LaunchAgent invoking the [user inventory script](../assets/mdm-recipes/inventory-user.sh)
   at the chosen interval and login. It must run in each intended app user's
   session. The script refuses root instead of silently scanning root's home.
3. Keep inventory JSON in a restricted local destination or an approved report
   pipeline; validate completeness before treating absence as meaningful.
4. Restart the app, complete the gate below, then enlarge the pilot scope.

Optional native Jamf AI Governance delivery is a different integration boundary.
Confirm the product's entitlement, supported OS/app versions, policy schema,
API access and effective-source precedence with Jamf for the target tenant.
This recipe includes no native API client and does not assert that ordinary
Jamf Pro licensing includes that service. The linked Jamf reference requires
JavaScript; its live procedure must be checked in the operator's browser before
execution. Plain file delivery does not depend on this optional service.

- [ ] Jamf tenant pilot, restart, allow/deny/outage and rollback recorded.

## Fleet: GitOps and script context

Review Fleet's [scripts guide](https://fleetdm.com/guides/scripts) and
[GitOps YAML schema](https://fleetdm.com/docs/configuration/yaml-files).
Scripts run as root on macOS/Linux by default. Enable scripts according to the
fleet policy; do not change enrollment-wide settings just for this example.

Use an existing approved software/package flow for the binary and reviewed
managed file. Merge the downloadable [controls fragment](../assets/mdm-recipes/fleet-controls.yaml)
into the intended team's GitOps file alongside the inventory script. Review
existing controls before applying your full team config. The fragment registers
a script; it does not schedule it or switch execution identity. Running the
user script directly through the root script runner intentionally fails.

For periodic current-user collection, distribute an approved per-user LaunchAgent
(macOS) or user timer (Linux) that invokes the script after login. Validate one
user's HOME and output permissions before rollout. A system-level all-profile
job is a distinct scope, described below, not a substitute for this user job.
Record Fleet execution status separately from application verification.

- [ ] Fleet tenant pilot and app-user scheduler validated; Linux distro recorded if used.

## Intune macOS

Follow [Intune macOS shell scripts](https://learn.microsoft.com/en-us/intune/device-management/tools/run-shell-scripts-macos).
Assign a small pilot group. Use a root-context delivery script or existing
package distribution for the approved binary and merged managed file. Use a
**separate** inventory script policy with **Run script as signed-in user** set
to Yes, a deliberate frequency and bounded retries. A signed-in user is required.
Never run the hook credential provisioning workflow as root's user profile.

Upload the user inventory script after reviewing the fixed CLI path. Retain
safe output only in the approved collection destination; Intune execution
success alone does not report evidence to Preloop. Restart Code and inspect
its effective source after delivery. Record missing-session and retry behavior.

- [ ] Intune macOS tenant delivery, signed-in collection and rollback validated.

## Intune Windows

Follow [Intune PowerShell scripts](https://learn.microsoft.com/en-us/intune/device-management/tools/run-powershell-scripts-windows).
Use your existing Win32/software delivery mechanism for the approved executable
and reviewed managed file under Program Files. Preserve existing ACLs and other
managed policy; this guide supplies no MSI or signed package.

For [user inventory](../assets/mdm-recipes/inventory-user.ps1), select **Run this
script using the logged on credentials: Yes** and **Run script in 64-bit
PowerShell host: Yes**. Keep the organization's script-signature policy; do not
disable it to use this sample. The template refuses SYSTEM. The Code managed
hook also requires the pinned app to support `shell=powershell`; test quoting
and execution under the real app.

Intune platform scripts are not a recurring collector: they normally run on
assignment/change, not at every sign-in. If recurring inventory is required,
use a separately reviewed user Scheduled Task or an appropriately licensed
recurring mechanism with explicit frequency, retention and user-session rules.
A SYSTEM job for file delivery is separate from that user task. Restart Code
and verify the actual managed source, allow/deny, missing user and rollback.

- [ ] Intune Windows PowerShell syntax/runtime and tenant acceptance validated.

## Kandji

Use [Custom Scripts](https://support.kandji.io/kb/custom-scripts-overview) or the
organization's approved custom software distribution. Custom Scripts execute as
root, so use them only for reviewed binary/file delivery and installation of
an approved per-user LaunchAgent. Do not execute the user inventory script
directly from the root library item. Record blueprint/scope, check-in frequency,
retry behavior and exact agent version before pilot assignment.

Select a bounded install/repair policy that compares the reviewed managed file
with its recorded hash. Unexpected drift stops repair for operator review;
never repeatedly overwrite a policy another owner has changed. Keep the
per-user collector schedule independent of the file delivery library item.

- [ ] Kandji tenant delivery, per-user scheduler, drift and rollback validated.

## Inventory and reporting schedule

The supplied user scripts run `agents discover --inventory`: safe offline JSON,
no auth, upload, onboarding or usage observation. Pin the CLI path; collect in
the actual app user's session. A scheduler should set an explicit frequency,
restrict retained output and replace/rotate old files. Record schema, collection
window, scope, completeness and errors, not just the process exit code.

Where authorized, an elevated **separate** job can run
`preloop agents discover --all-users --inventory`. It enumerates bounded local
profiles, not directory-service or every possible profile. Limits and unreadable
files remain partial/failed; this is not automatic all-user enrollment.

Opt-in reporting uses `agents discover --report --json --no-onboard-prompt`
with separately supplied `report_discovery` authority; all-profile reports use
explicit `--all-users --report`. Review the [reporting contract](../guide/agent-discovery-reporting.md)
for scope limits, evidence-service prerequisites and credential custody. Never
add a static report token to these public templates. Metadata inventory does
not prove activity or verified compliance.

Once reports arrive, [Device fleet](../guide/device-fleet.md) shows each
device and can alert on quiet agents, missing expected controls and stale
inventory, with per-source report freshness.

## Stage, expand and roll back

Keep one acceptance record per OS/app/MDM version and effective policy hash:

- [ ] Clean delivery and unchanged rerun preserve unrelated managed keys.
- [ ] Correct app user can read the file, execute the CLI and access its own hook credential.
- [ ] Restarted app reports the intended effective managed source.
- [ ] A harmless allowed action succeeds; a denied action has no downstream side effect.
- [ ] Approval-gated action waits, then follows approval/denial with attributable audit evidence.
- [ ] Missing/revoked credentials and gateway outage produce recorded actual app behavior.
- [ ] Changed file, higher-priority policy, alternate client and direct route attempts have recorded outcomes; any escape stays an explicit gap.
- [ ] Inventory reports scope/completeness truthfully; no-session and stopped collection remain pending/stale.
- [ ] Rollback restores only the owned policy delta and correct permissions; app restart verifies the restored source.

On rollback, remove the rollout assignment/schedule first, then restore its
reviewed policy delta only if the current hash matches the deployed revision.
If it differs, reconcile with the policy owner instead of overwriting intervening
changes. Revoke retired device/user/app credentials separately. Disable collection
independently of tool/model controls. Never restore personal OAuth state from a
package. An unsuccessful deny, drift or rollback test stops expansion.

See [what each signal proves](../guide/endpoint-coverage.md#what-each-signal-proves)
and the [threat model](../guide/endpoint-coverage.md#threat-model) for what these
results establish. Fixture checks cannot check a vendor tenant, verify a live
app or certify an enterprise plan.

## Reproduce local checks

From a repository checkout with a Go toolchain, Python 3, ShellCheck and the docs
environment, build a temporary CLI and run:

```sh
export PRELOOP_DISABLE_TELEMETRY=true
(cd cli && go build -o ../build/preloop ./cmd/preloop)
sh docs/scripts/test_mdm_recipes.sh "$PWD/build/preloop"
shellcheck docs/assets/mdm-recipes/*.sh docs/scripts/test_mdm_recipes.sh
mkdocs build --strict
```

Tests use temporary bundles and stub collectors, inspect actual exported artifacts for all three
platforms, check refusal to overwrite a bundle and refusal to collect as root,
and check collector flags against the compiled command. They never install a
managed file, use a real credential store or contact a tenant. PowerShell and
native OS scheduling still require their own Windows/macOS acceptance records.

Extension inventory uses the larger `preloop.inventory.v4` envelope (one record
per extension, up to 200 per app context). Allow for the larger report when
sizing MDM output capture. Existing v1/v2 reports remain accepted; a v3 collector
retries without extensions only when the server rejects the schema version.

## Extension compliance check

Download the account policy from the console Extensions tab (**Download policy
file**) or `GET /api/v1/agents/extension-approvals/export` (`view_agents`). The
file lists approval names, version constraints, and content hashes. It has no
credentials and no device data. Deliver it with the same reviewed file channel
as other artifacts, then run this in the user session being checked:

```bash
preloop agents extensions check --policy-file /path/to/policy.json
```

| Exit | Script result |
| --- | --- |
| 0 | Compliant. Nothing at or above the failure threshold. |
| 3 | Non-compliant. At least one unapproved extension. |
| 4 | Non-compliant when the script passed `--fail-on unknown`. An extension has no matching decision and is not builtin. Exit 3 wins when both are present. |
| 2 | Error. The scan was incomplete, or the policy file was unreadable or invalid. Do not treat this as compliant. |
| 1 | Error. Any other failure, including `--online` transport or authentication problems. |
| 64 | Error. The command named both `--policy-file` and `--online`, or neither. |

`--fail-on` defaults to `unapproved`, so an extension with no policy entry exits
0. Pass `--fail-on unknown` when absence from the policy must fail the job.

The check proves that extension files are present or absent in the locations
the collector reads. It does not prove what the agent loaded or executed.

## Extension restrictions

The **Extensions → Enforcement** card renders an account's explicit marketplace
policy and Unapproved marketplace plugin names into a separate drop-in. It does
not infer source URLs from hashed inventory. Content pins and version constraints
remain inventory/check evidence: this overlay cannot enforce them. Approved and
Unknown plugins are not force-enabled or disabled.

Reading, editing and rendering this policy require `manage_agents`: marketplace
headers may contain credentials. After a concurrent edit rejects a save, use
**Reload saved policy (discard edits)** to load the latest revision and bundle
registrations before editing again.

1. If **Managed hooks only** is selected, deploy and register the `claude-code`
   hook bundle for the target platform first. The renderer requires that active
   registration, but registration alone does not prove the hook is installed.
2. Edit the six options and explicit marketplace sources. Include every allowed
   source, including `anthropics/claude-plugins-official` if it should remain
   available. Preview refuses a restricted policy with no allowed marketplace:
   `strictKnownMarketplaces: []` blocks all sources, including official ones.
   Pattern sources, `skills-dir` and GitHub owner wildcards are policy matchers;
   they cannot be registered on devices.
3. Select macOS, Linux or Windows and preview. Read the omitted restrictions,
   rendered JSON and the last registered revision. **Out of date** compares the
   current rendered hash with the active extension bundle for that platform.
4. Download the renderer response and review it offline:

   ```sh
   preloop agents managed-config claude-code-extensions \
     --from-file preloop-extension-overlay.json --platform darwin \
     --output ./extension-bundle --dry-run
   ```

   Remove `--dry-run` to write `50-preloop-extensions.json`, `manifest.json` and
   `preview.txt`. The CLI verifies the hash of the exact UTF-8 settings bytes.
   `--from-file` never connects to Preloop and cannot be combined with
   `--register`. An explicitly supplied `--platform` must match the response.
   Without `--from-file`, the command authenticates to the account renderer;
   add `--register` to record its manifest. The old hook export remains offline.
   `--overwrite` replaces only regular files in a separate review directory.
   The command refuses managed system directories, including their drop-ins.
5. Use endpoint tooling to place **only** the reviewed settings artifact at the
   target below. Do not put `manifest.json` in the drop-in directory: Claude Code
   reads every non-hidden `*.json` there. Refuse a pre-existing target; reconcile
   its owner and effective policy before replacing anything. Inspect the main
   managed file and other drop-ins for conflicting keys before rollout.

   | Platform | Drop-in target |
   | --- | --- |
   | macOS | `/Library/Application Support/ClaudeCode/managed-settings.d/50-preloop-extensions.json` |
   | Linux | `/etc/claude-code/managed-settings.d/50-preloop-extensions.json` |
   | Windows | `C:\Program Files\ClaudeCode\managed-settings.d\50-preloop-extensions.json` |

   Claude Code merges the main file first, then drop-ins in alphabetical order.
   Restart and verify the effective source, a refused plugin, a refused
   marketplace, and the Preloop hook before expanding the pilot.

### Supported keys and versions

The verified table in `backend/preloop/services/extension_policy_keys.py` records
the shape, source URL, check date (2026-11-25) and minimum supported version for
every emitted key. Removing an entry omits that key as `shape unverified`.
The current supported floor is **Claude Code 2.1.332**, including drop-in support.
This is a conservative tested floor, not a claim about each key's introduction:
the vendor reference does not date every key. The preview reports the highest
floor among emitted keys and drop-in support. Older clients, or clients without
drop-in directory support, ignore the file or unsupported keys. There is no
runtime version detection in this release.

Verified shapes: `enabledPlugins` maps plugin IDs to `false`;
`strictKnownMarketplaces` and `blockedMarketplaces` contain source objects;
`extraKnownMarketplaces` maps names to `{source: {...}}`;
`allowManagedHooksOnly`, `disableSkillShellExecution` and `disableSideloadFlags`
are booleans; `strictPluginOnlyCustomization` is emitted only as `["skills"]`.
The overlay never locks `mcp` or `hooks` through that array.
See the vendor's [settings reference](https://code.claude.com/docs/en/settings-reference),
[plugin policy](https://code.claude.com/docs/en/plugins/org),
[hooks](https://code.claude.com/docs/en/hooks), [skills](https://code.claude.com/docs/en/skills),
and [managed sources](https://code.claude.com/docs/en/managed-settings).

### Vendor admin console: unverified for Preloop routes

The same JSON can be copied from the card's explicitly labelled **Vendor admin
console (unverified)** section for review and pasting into **Organization settings
→ Claude Code → Managed settings**. `--delivery vendor_console` records the
operator's intended channel in the manifest; it does not change the JSON or
verify delivery. The default is `--delivery endpoint_file`.

**Delivery is unverified for agents routed through Preloop with a non-default
`ANTHROPIC_BASE_URL`.** The Team/Enterprise organisation experiment in #2320
criterion 11a has not been performed. Do not assume that onboarding through
Preloop receives admin-console policy. Prefer the endpoint file for Preloop
routes, personal plans, API keys and cloud-provider routes. For eligible managed
plans, the vendor console can simplify rollout only after delivery is verified
in that environment. The vendor describes client-side control that an unmanaged
device can bypass, a cache that can persist for 24 hours, and delivery exclusions
for cloud-provider sessions. See [server-managed settings](https://code.claude.com/docs/en/server-managed-settings).

### Limits and rollback

- no allow-list-only mode for plugins exists in Claude Code
- restrictions stop loading and do not delete files on disk
- a higher-precedence managed source (server-managed settings, MDM plist, registry) supersedes the file
- export and registration do not prove installation

The source-precedence statement describes the vendor's default selection.
Newer clients also offer explicit managed-source composition; this overlay does
not emit that setting. Endpoint files still depend on the client honoring them
and on protecting the system directory from standard-user writes.

To roll back delivery, have the endpoint policy owner remove only
`50-preloop-extensions.json` and restart/verify Claude Code. Preserve the hook
bundle and other managed sources. Reverting Preloop or downgrading the policy
migration does not remove files already deployed to devices. Registered extension
bundle revisions then remain inert metadata.


## Daily reporting from an installed runner

A resident runner now supplies recurring discovery without a second MDM task.
It reports agents and extensions with its own reporting capability after a short
random startup delay and then daily with jitter. An elevated service uses the
all-users collector; the supplied per-user runner installations scan only their
own user. Partial profile coverage stays explicit in each report.

Deploy these keys in the service identity's Preloop config to change the device
interval or disable the automatic report:

```yaml
runner:
  discovery:
    enabled: true
    interval: 24h
```

Set `enabled: false` to opt out. An account administrator can also use
`PUT /api/v1/account/runner-discovery` with
`{"enabled": false, "interval_seconds": null}` to disable automatic reporting
across the account. A non-null interval is in seconds, from 3600 to 604800, and
supersedes the device interval. Device opt-out always wins.

Check `preloop runner status` under the same service identity to see discovery's
last attempt, result and last success. No extra report token is required. Reports
never enroll agents or modify application files; installation and enrollment stay
separate MDM actions. See the [automatic reporting contract](../guide/agent-discovery-reporting.md#automatic-runner-reports)
for retries, disclosure and privacy controls.


Automatic reports also follow the extension content privacy controls:
`discovery.extension_content: false` or `PRELOOP_NO_EXTENSION_CONTENT=1` turns
content off on the device. Per-extension marker files and exclusion patterns
apply across scanned profiles. The account setting **Collect extension content
for review** is enforced before collection and again on upload. The first
content upload prints its disclosure in the service log; it never prompts.
