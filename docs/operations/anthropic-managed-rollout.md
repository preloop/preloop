# Managed Claude endpoint rollout

Editions: Preloop, Preloop Cloud and Preloop Enterprise; provider-side services
have their own entitlements. Reviewed source: 2026-10-10. MDM tenant rollout and
real application enforcement remain unverified on macOS, Windows and Linux.

## Versions and control ownership

A Team-first pilot can use the vendor's
[server-managed settings](https://code.claude.com/docs/en/server-managed-settings)
and [remote MCP configuration](https://code.claude.com/docs/en/mcp), subject to
the current platform and plan prerequisites. Server-managed settings are
available on Team and Enterprise and can distribute hooks,
`allowManagedPermissionRulesOnly` and `managedMcpServers` (remote HTTP/SSE in
Claude Code v2.1.259 or later). Review **Platform availability** in the live
vendor instructions before selecting devices or publishing commands.

These settings are applied by the client. They are not provider-side inference
enforcement: an initial settings fetch can fail open, user approval may be
required, and a custom provider or base URL can skip fetching. Test effective
settings, first-fetch outage and alternate routes on the pinned application.
Enterprise inference hooks remain an optional separate control requiring their
own organization entitlement, not a prerequisite for every managed Code pilot.

| Pilot choice | Entitlement and benefit | Remaining gate |
| --- | --- | --- |
| Team managed Code settings and approved remote MCP | Team/Enterprise settings/connector eligibility; reuse organization Claude Code usage | Client-applied settings, resource/network boundaries and real app acceptance |
| Team self-hosted beta runner | Owner-enabled Team/Enterprise beta; same organization Claude Code usage as hosted | Isolated runner, restricted authority, replay and revocation tests |
| Enterprise provider inference hook | Eligible Enterprise organization and Owner configuration | Full inspection/tool/failure settings and covered-surface pilot |
| Console/API-key or API federation route | Separate API entitlement and billing | Does not establish subscription or Enterprise-hook coverage |

Do not infer API-rate billing merely from Preloop tool governance or an owner VM.
Verify the actual inference route and organization allowance/rate limits with the
vendor. A changed model base URL can change both settings behavior and billing.

The secret-free Claude Code managed-hook exporter merged in
[PR1439](https://github.com/preloop/preloop/pull/1439) on 2026-10-09. Its command
below is a **merged-source build example**, not a command verified in the latest
published release, v0.16.0. Pin a build containing merge
`2bf56d4eeb22ebd021c271980a41779f6e066f77` or a later reviewed descendant.
Verify published binaries and release notes before using a fleet recipe.
The [Desktop routing guide](../guide/clients/claude-desktop.md) remains the
authoritative Desktop path; the Code overlay does not configure Desktop.

| Setting or credential | Owner and purpose | What it does not establish |
| --- | --- | --- |
| Secret-free managed Code file | MDM/admin delivers reviewed PreToolUse overlay | Enrollment, model route, credential custody or live enforcement |
| Per-user/device hook credential | Separately approved Preloop onboarding/custody | Report-only identity or inference provider authority |
| Report-only collector credential | Discovery reporting only | Tool execution, model access or an enforcement result |
| Inference credential/helper | Dedicated gateway/model path | Device attestation or every local/cloud tool path |
| Provider-side Enterprise hook | Anthropic organization owner | Unmanaged/personal subscription or unrelated API traffic |
| Device app/network/resource policy | Endpoint and resource administrator | Unconditional coverage of browser or remote cloud sessions |

Keep these credentials separate. Never distribute an account-wide admin token,
subscription OAuth bundle or signing secret in an MDM package. A report row means
installed/configured/observed metadata as specified by that signal; it cannot be
promoted to governed or enforced merely because collection succeeded.

## Export and review offline

From a pinned source-build CLI, with an existing output parent:

```bash
PRELOOP_DISABLE_TELEMETRY=true preloop agents managed-config claude-code \
  --output ./review-bundle --platform darwin \
  --cli-path /opt/preloop/bin/preloop --timeout 300
```

Review `managed-settings.json`, `manifest.json` and `preview.txt`. The exporter
does not install, enroll, authenticate or read existing credentials. It emits
only `hooks.PreToolUse`, matcher `*`, using the existing permission-hook adapter.
Use `--platform windows` with a reviewed absolute local `.exe` path for Windows,
or `--platform linux` for Linux/WSL. The hook timeout must exceed the independently
configured approval wait plus process headroom. Follow the
[Code overlay details](../guide/clients/claude-code.md#export-a-managed-hook-overlay).

Do not install an overlay until each intended app user has separately provisioned
the hook credential through the approved onboarding policy. Missing credentials
deny under the hook contract. Exported file semantics are fixture-tested;
honoring those semantics in a real app is still unverified.

## Staged MDM recipes

These are operator delivery recipes, not verified vendor-tenant integrations.
Detailed staged templates and tenant validation are tracked in
[#1181](https://github.com/preloop/preloop/issues/1181); application-level pilot
verification is tracked in [#1182](https://github.com/preloop/preloop/issues/1182)
and coverage/threat-model documentation in
[#1347](https://github.com/preloop/preloop/issues/1347). This guide coordinates
those scopes and does not close their acceptance gates.

| Platform and delivery | Practical staging action | Required tenant/app evidence |
| --- | --- | --- |
| Jamf Pro macOS | Deliver signed CLI/package plus root-owned reviewed managed file using supported package/script mechanisms; separate app-user credential provisioning | Effective source, ownership, restart and harmless allow/deny/outage/rollback on a pinned macOS/app version |
| Fleet macOS/Linux | Use supported GitOps/package/script delivery for the reviewed file; schedule collection in the intended app user's context | Actual Fleet tenant delivery and scoped user inventory; Linux distribution explicitly recorded |
| Intune macOS | Use supported macOS package/script delivery for Code file; use Desktop's documented preference payload for Desktop | Effective managed source and app-user helper/credential access |
| Intune Windows | Deploy CLI and reviewed Code file using a supported Win32 app/script; Desktop uses its documented machine registry policy | Local `.exe` path, PowerShell hook support, execution context and app restart |
| Kandji macOS | Use supported custom package/script delivery with reviewed file ownership and separate user custody | Tenant entitlement, idempotent delivery, source precedence and rollback |
| Linux/WSL owner-managed host | Admin installs only the reviewed root-owned Code file and schedules app-user collection separately | Distribution, desktop/runtime package and actual app result; none currently verified |

Jamf managed-file delivery is distinct from optional native AI Governance policy
delivery. Check that product's entitlement, platform and API prerequisites before
selecting it; this page implements no native Jamf API client. Do not assume every
Jamf Pro tenant includes it. Likewise, platform names do not imply completed
vendor integrations.

Use the vendor's [managed settings reference](https://code.claude.com/docs/en/managed-settings)
to confirm effective source and precedence in the pinned application. Code file
targets are:

| OS | Code target |
| --- | --- |
| macOS | `/Library/Application Support/ClaudeCode/managed-settings.json` |
| Windows | `C:\Program Files\ClaudeCode\managed-settings.json` |
| Linux/WSL | `/etc/claude-code/managed-settings.json` |

Preserve unrelated organization/vendor keys and use protected ownership and
permissions. Lower user/project settings cannot remove managed hooks, but another
managed source can supersede the file. Restart the app and inspect the effective
source before running the pilot. Do not turn an exporter into an unattended
all-user onboarding job.

For discovery scheduling, use the separate
[report-only token and data-minimization contract](../guide/agent-discovery-reporting.md).
Run in the intended app user's context; root-only inventory does not prove that
user's configuration was inspected. Record incomplete scans truthfully and
avoid printing credentials. Endpoint custody work under
[#1183](https://github.com/preloop/preloop/issues/1183) is unpublished and is not
a supported release recipe here.

## Pilot gate and coverage limits

Begin with one synthetic test device and one harmless action. Record OS,
distribution/build, app, CLI, server and MDM versions, overlay hash and effective
source. Verify allow, explicit deny, whole-batch deny without side effects,
unknown identity, missing/revoked credential, outage, changed policy, alternate
covered client, direct-route attempt, drift and rollback. Link results to the
[acceptance matrix](anthropic-acceptance.md). Expand only after the named owner
reviews the real application results.

Local administrators can alter local software. Managed defaults are not device
attestation or admin resistance; externally controlled resource/network/provider
services supply that boundary. Browser personal accounts, unmanaged devices,
remote cloud sessions and API-to-API traffic need their own controls and
verification. The Code overlay exports no Codex adapter, Desktop bootstrap,
model route, MCP allowlist, enrollment or OTel policy. Linux/WSL paths are
generated; **no Linux distribution has a verified rollout result** in this guide.

## Rollback

Before deployment record the old managed source/hash, ownership, app version,
credential ownership and independent resource controls. Remove or restore only
this rollout's managed keys, using the MDM's reviewed rollback mechanism.
Restore the approved app-user backup and restart the app; do not overwrite
unrelated organization policy or import personal OAuth state. Revoke retired
device/hook credentials without replacing revoked authority automatically.
Report-only collection can be disabled separately from inference/tool control.
Verify the rollback with harmless app actions and capture the effective restored
source. Failed drift or rollback tests stop expansion.

## Screenshots

MDM and app-setting captures are pending authorized test tenants/devices. Capture
each delivery policy, effective managed source, permission/credential health,
denied harmless app action and rollback. Record the exact OS/app/MDM versions.
Actual Preloop UI captures, when available, are labelled local synthetic fixtures
and do not validate a vendor tenant. See the [screenshot provenance checklist](anthropic-acceptance.md#screenshots).
