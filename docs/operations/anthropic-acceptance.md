# Anthropic governance acceptance and evidence

Version: 2026-10-10. All Enterprise-hook, self-hosted beta and MDM application
results are currently **unverified**. Protocol fixtures, merged source, local UI
and a reachable endpoint each provide different evidence.

Use the [Enterprise hook guide](anthropic-inference-hooks.md),
[self-hosted runner guide](anthropic-self-hosted-runner.md) and
[managed rollout guide](anthropic-managed-rollout.md) for the applicable
entitlement and control boundary. Never claim that an API key or personal
subscription validates an Enterprise tenant.

Choose a Team-first managed settings/remote MCP or owner-enabled self-hosted beta
pilot when its documented controls meet the intended boundary. Enterprise
provider inference hooks are a separate optional route. Record the plan,
platform and actual subscription/API inference path: local governance and an
owner VM do not by themselves switch usage to API billing. Client-applied Team
settings still need first-fetch outage, approval and alternate-route tests.

## Offline conformance harness

From the source checkout with its documented development dependencies installed:

```bash
export PRELOOP_DISABLE_TELEMETRY=true
python -m unittest discover -s docs/scripts -p test_anthropic_conformance.py
python docs/scripts/anthropic_conformance.py \
  --output /tmp/anthropic-synthetic-evidence.json
```

The default harness validates twelve **explicit simulated protocol outcomes**,
verdict shape, batch side-effect assertions and a strict content-free evidence
contract. It launches no application, sends no network/model requests, reads no
credentials and creates no paid resources. It does **not** run the commercial
adapter or prove its policy implementation; record that adapter's own test suite
separately. A passing fixture report always says `synthetic_protocol` and
`enforcement_state: unverified`, including the uncovered direct route.

Run this from a synthetic HOME when extending it to scripted product runs, with
explicit mock keyring seams. No personal discovery, OS settings changes or
keychain access is part of this harness.

## Live evidence gate

Live mode imports observations already gathered by an authorized operator. It
does not purchase access, activate tenant policy, launch compute or initiate paid
requests. Use a designated test tenant and only harmless disposable fixtures.
Generate a pending template explicitly:

```bash
PRELOOP_DISABLE_TELEMETRY=true python docs/scripts/anthropic_conformance.py \
  --live --template --test-tenant-ref test-tenant-001 --harmless-fixtures \
  --output /tmp/anthropic-pilot.json
```

After executing the reviewed pilot manually, import its content-free record:

```bash
PRELOOP_DISABLE_TELEMETRY=true python docs/scripts/anthropic_conformance.py \
  --live --test-tenant-ref test-tenant-001 --harmless-fixtures \
  --evidence /tmp/anthropic-pilot.json --output /tmp/anthropic-pilot-validated.json
```

Schema validation checks record completeness and rejects mixed synthetic/live
results, duplicate/missing cases and unexpected content fields. It cannot
authenticate an operator's observations or certify their truth. Store referenced
artifacts in the approved evidence repository and have the pilot owner review
them. A `pass` in live mode requires an artifact reference; whole-batch deny also
requires an observed zero side-effect count. Pending cases cannot establish a
verified recorded surface. Unsupported or unknown settings stay unverified.

Use synthetic references such as `artifact-001` and `test-tenant-001`; keep the
restricted reference-to-artifact mapping outside the public record. Fields do
not accept raw prompts, tool arguments, filenames, emails or credentials. Public
screenshots must be reviewed for these too. Download the
[evidence schema](../assets/schemas/anthropic-evidence.schema.json) and
[pending pilot template](../assets/evidence/anthropic-pilot-template.json).

## Required cases

| Case | Application observation required before a live pass |
| --- | --- |
| Allowed prompt | Harmless request succeeds on the named covered client/version |
| Denied prompt | Explicit rejection shown; forbidden continuation absent |
| Denied tool batch | Entire current batch is rejected; no member causes a side effect |
| Unknown identity | Unmapped/invalid actor rejected before authority or execution |
| Unknown source | Unrecognized source cannot gain scope from supplied strings |
| Unknown event | Rejected or explicitly uninspected; never counted as governed |
| Endpoint outage | Real client blocks under effective failure mode; no prohibited side effect |
| Policy change | New snapshot/generation applies; old authority does not retain access |
| Revoked credential | Existing authority rejected; re-exchange cannot silently remint |
| Alternate covered client | Same harmless checks repeated on the separately named surface |
| Direct-route attempt | Resource/network/provider control denies the bypass, or report the gap |
| Rollback | Effective restored state and credential retirement observed |

For self-hosted runners, add separate external-network bearer replay and every
REST/MCP/WebSocket authorization result to the restricted artifacts. For MDM,
add effective managed-source precedence, drift, missing credential and each OS
distribution/build result. A direct gateway probe cannot replace these app
observations. A non-applicable or unsupported case remains pending with an
explicit surface limit, rather than a fabricated pass.

## Deployment and rollback checklist

This checklist prepares an operator review. It authorizes no deployment.

1. Record exact core and commercial backend SHAs, frontend build, CLI/app version,
   migrations, capability defaults and generated OpenAPI version. Verify each
   required source change is merged **and** included in the candidate release.
2. Confirm tenant plan/role/beta enablement, final public HTTPS endpoint,
   independent secret custody, resource isolation and test-device ownership.
   Preserve the original compatible core pin until the new migrations/APIs are
   tested together. Do not infer deployment from source merges.
3. Keep both provider capabilities disabled initially. Review binding,
   tenant/resource mapping, policy snapshot/generation and short expiry. Ensure
   exchange remains closed until restricted authority is enforced everywhere.
4. For an Enterprise-hook pilot, record effective vendor full inspection, empty
   exclusions, tool validation, enforce state, failure mode and timeout. Compare
   declarations with actual tenant settings, then run shadow and enforced cases.
   For a Team settings/resource pilot, record client-applied settings and external
   resource authorization; Enterprise hooks/IP allowlists are separate optional
   controls. Do not require their entitlement to test the Team resource boundary.
5. Collect all relevant application cases and independent network/resource replay
   results. Investigate findings rather than dismissing them as fixture success.
6. Verify rotation, outage recovery and rollback before expansion. Stop sessions
   and revoke active authority when required; never silently choose a direct
   fallback. Preserve unrelated configuration and record the restored state.
7. Have the named operator approve the specific recorded surface/version and
   expiry of evidence. A broader vendor/client/OS claim requires its own results.

## Screenshots

Actual local Preloop components may be captured with Playwright and synthetic
account/API fixtures. Label each as a **local synthetic UI capture** with source
SHA, fixture path/hash, browser version, capture date, viewport, image hash and
descriptive alt text. Such an image proves rendered UI only, not deployed backend
or vendor behavior. Do not generate substitute screenshots or infer live access
from a seeded component.

The [capture manifest](../assets/screenshots/anthropic/manifest.json) records
available captures and gaps. Until a permissioned tenant/device exists, the
following captures remain missing:

- Vendor hook endpoint and effective enforcement/inspection/tool/failure settings.
- Real application's denied batch, outage, recovery and rollback state.
- Self-hosted environment enablement, work-order isolation and external replay.
- Jamf/Fleet/Intune/Kandji delivery and effective app-managed source per platform.

Follow the linked vendor instructions during the live pilot. Keep signing
secrets, tokens, organizational identifiers, prompts and raw arguments out of
frames; use generic labels and review every image before publication.
