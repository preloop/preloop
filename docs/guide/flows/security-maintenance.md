# Supported-release vulnerability maintenance

This controller keeps one durable work item per opted-in product, supported
release, and advisory/component pair. It is not a backport factory, not a
conformity assessment, and not an SLA. Finding absence on a later scan is not
resolution. Failed, incomplete, or unknown audits never become a new baseline.

Clone **Automated Issue Implementation** (`011`) for the repair flow and a
read-only CRA audit preset (`004`–`007`, unchanged) for the recheck. Optional
preset `014` is a conservative implementation overlay: isolated publication,
a verification gate, and no agent approval tools. Configure inventory through
the API, not by pasting a model-output URL. Scan ingest must name an existing
tracker issue so implementation receives a real issue body, number, and
repository. The trigger payload carries the pinned build, SBOM bytes or path,
prior baseline result, and published SHA on the existing workspace/trigger
contracts. After human approval, a rebuilt SBOM for that published commit
must be submitted (`POST /items/{id}/build`); the controller does not rebuild
the product. Recheck then checks out the verified published commit. Human
decisions go through the platform approval workflow (owner, escalation, quorum,
expiry); approving the linked request in the console advances the item without
a second maintenance-specific call. Background reconciliation retries dispatch
and expired approvals without requiring a GET. GET returns the stored item.

## Opt in a release

`POST /api/v1/security-maintenance/releases` is the only enrollment path.
Unsupported product or release names fail closed on scan ingest.

```json
{
  "product_key": "example-widget",
  "release_key": "1.2",
  "display_name": "Example Widget 1.2",
  "project_id": "<project-uuid>",
  "pinned_build_ref": "v1.2.3",
  "sbom_input_ref": "sbom/image.spdx.json",
  "audit_flow_id": "<audit-flow-uuid>",
  "implementation_flow_id": "<implementation-flow-uuid>",
  "recheck_flow_id": "<recheck-flow-uuid>",
  "approval_workflow_id": "<approval-workflow-uuid>",
  "approval_owner_user_id": "<user-uuid>",
  "escalation_user_ids": [],
  "escalation_after_seconds": 604800,
  "max_retries": 3,
  "enabled": true
}
```

The implementation flow must use isolated publication and `verification.mode:
gate`. The audit/recheck flow must not publish, and should keep an empty MCP
tool list. Model and input-kind allow lists, when set, are enforced.

## Initial baseline

Schedule the source SBOM audit through the authenticated inventory API, then
accept only a completed bound execution:

```http
POST /api/v1/security-maintenance/releases/{release_id}/baseline/audit
{"sbom_content_base64": "<source CycloneDX or SPDX JSON>"}
```

That request pins the saved audit flow, `pinned_build_ref`, and
`sbom_input_ref`, writes a `PENDING` execution with the controller envelope,
commits, then dispatches through the existing flow trigger path. `GET
/api/v1/security-maintenance/releases/{release_id}` shows
`baseline_audit_execution_id` and `baseline_dispatch_state`. A dispatch
failure leaves the execution `PENDING`; the same `POST` with the same SBOM
bytes retries without creating a second row. `PENDING` or failed executions
cannot be accepted.

After the audit execution is `SUCCEEDED` and evidence is stored:

```http
POST /api/v1/security-maintenance/releases/{release_id}/baseline
{"audit_execution_id": "<execution-uuid>"}
```

Acceptance still requires the envelope `release_id` and the digest of the
supplied SBOM bytes. Filename and pin matches alone are not enough.

## What happens

1. A trusted scan ingest names findings. The same identity updates the existing
   item. A disappeared finding is recorded as unverified and does not close the
   item or open a second pull request.
2. One implementation execution is dispatched through the existing flow worker.
   Completion reads controller publication receipts (`head_sha`) and
   controller-owned verification for that commit. `SUCCEEDED` without a test
   receipt does not pass.
3. Tests failing holds the item. Tests passing opens a platform approval
   request. Agent `result.approved` is ignored. An execution API key cannot
   approve its own repair. Denied or expired approvals hold or escalate; they
   never auto-release. The approval owner must already be on the selected
   workflow; maintenance does not rewrite shared workflow rows.
4. After a human approval, submit a rebuilt SBOM bound to the published SHA
   (`POST /api/v1/security-maintenance/items/{id}/build`). Reusing the original
   SBOM bytes is rejected. The controller parses the submitted CycloneDX or
   SPDX JSON and derives component identities from those bytes; a model
   inventory that omits a still-present target cannot prove removal. Removal
   requires a supported CycloneDX JSON (`specVersion` 1.2–1.6) or SPDX JSON
   (`spdxVersion` SPDX-2.2 or SPDX-2.3) document with an explicit component
   list (`components` or `packages`). Omitted or null lists, unsupported
   versions, and malformed nesting are rejected; an explicit empty list is
   a valid empty inventory. This is inventory integrity, not full SBOM schema
   validation. Malformed, ambiguous, incomplete, or unadvertised SBOM input
   is rejected. A recheck execution then checks out the published SHA against
   that new inventory. CRA results are validated by the contracts layer.
   Missing evidence, unknown `preloop.cra.*` schemas, incomplete scans, and
   unscreened components cannot prove the advisory is gone. Checkout proof is
   the controller `HEAD.txt` in the evidence pack, not `payload.sha`. Initial
   baseline acceptance requires the audit execution scheduled above.
5. Only an accepted recheck writes a new baseline. Prior decisions stay
   append-only. Resume retries without rewriting history.

## What this is not

This is **not** automatic backporting, **not** a Cyber Resilience Act filing,
**not** a certification, and **not** a promise that every supported release
will be patched on a calendar. Operators still choose which products are
opted in, which flows run, and which humans approve a repair.
