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

This overlay supplies only `hooks.PreToolUse`. It does not configure model
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
