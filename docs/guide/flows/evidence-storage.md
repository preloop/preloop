# Evidence storage and retention

Audit-style flows write a human-readable pack under `/workspace/evidence/`
plus `/workspace/result.json`. This page is the operator runbook for how
that pack is transported, stored, retrieved and retained. It does **not**
claim legal hold, WORM, object-lock, certification, or CRA Article 14
filing. Cross-link the [security audit presets](security-audit-presets.md)
guide for the JSON contracts themselves.

## Two transports

**Legacy (default).** `FLOW_ARTIFACT_DIRECT_UPLOAD` is off. Hosted Docker
copies the directory through the engine API. Kubernetes still emits a
size-capped base64 block on the pod log channel
(`MAX_EVIDENCE_ARCHIVE_BYTES`, 2 MiB compressed). The control plane stores
the bytes on `flow_execution.evidence_archive`. Failed persist is visible as
`evidence-status: failed` or `missing`; it must not look like a successful
receipt. Existing downloads keep working.

**Direct upload (configured path).** Set `FLOW_ARTIFACT_DIRECT_UPLOAD=true`
when the runner can reach `PRELOOP_URL`. Hosted containers and private
Docker runners receive an execution-bound JWT (`aud=flow-artifact`,
`kind=evidence`, `operation=put`). They tar the evidence directory (and
`result.json` when present) and PUT it to
`/api/v1/flows/executions/{id}/artifacts`. Kubernetes logs then carry only
`PRELOOP_ARTIFACT_*` status markers and `PRELOOP_EVIDENCE committed|failed|absent`
lines — never the pack bytes. Hosted Docker uses the same EXIT-trap PUT;
after exit the control plane reads those `PRELOOP_EVIDENCE` lines and binds
the stored artifact instead of copying `/workspace/evidence` a second time.
Workspace checkpoints stay on the separate `workspace` / `native_session`
kinds; private runners still do not receive hosted workspace checkpoint
capabilities. Checkpoint restore reads up to
`PRELOOP_CHECKPOINT_MAX_BYTES` even when the smaller evidence cap is set.

The capability names one account, flow, thread, execution, kind and
operation. It is not a storage credential. Agent containers never receive
`SECURITY__ENCRYPTION_KEY`.

Private Docker completions report the final evidence PUT as top-level
`evidence_upload` (`uploaded`, `failed`, or `absent`) next to `result`.
That field is runner bootstrap metadata and is emitted even when
`result.json` is missing or invalid; agent `result` JSON cannot set it.
A failed or missing final PUT is stored as `failed`/`missing` even
when an earlier trap artifact exists.

## What is in a pack

A pack is a gzip tar holding the agent's files under `evidence/`,
`result.json` when the flow writes one, and `manifest.json`
(`preloop.cra.evidence_manifest/v1`) at the archive root:

```json
{
  "schema": "preloop.cra.evidence_manifest/v1",
  "execution_id": "0b0f...",
  "generated_at": "2026-09-08T10:15:00Z",
  "members": [
    {"name": "evidence/audit-report.md", "size_bytes": 8412, "sha256": "9f2c..."},
    {"name": "result.json", "size_bytes": 5120, "sha256": "1a77..."}
  ],
  "members_digest": "4d51...",
  "inputs": [{"path": "sbom.json", "size_bytes": 91233, "sha256": "aa10..."}],
  "source": {"status": "declared", "repositories": [{"remote": "...", "commit": "..."}]}
}
```

`members` covers every file in the archive except the manifest itself.
`inputs` digests the `workspace_files` seeds as delivered to the run, so a
reader can check that the SBOM in the pack is the SBOM that was audited.
`source` repeats the commits the caller declared in `product_provenance`:
it is a declaration, not an attestation, and the verified form lives in
`dossier_manifest` on the execution result.

The container writes the manifest on the direct path. On the legacy path
the control plane adds it when the pack arrives, before the archive is
stored and before its receipt is minted, so the digest in the receipt is
the digest of the bytes that are kept. Packs captured before this existed
have no manifest and still download and verify by receipt digest.

`python -m preloop.cra.ci` checks the manifest whenever one is present: a member
whose bytes do not match, a listed member that is gone, and a packed member
that nothing lists are all failures.

## Validation, encryption, quota

The shared artifact service (`preloop.services.flow_artifacts`) validates
compressed size, expanded size, tar member count, paths (no absolute
paths, `..`, or backslashes), and file kinds (regular files and
directories only; no links or devices). It encrypts the payload with the
configured Fernet key and commits the immutable manifest (digest, byte
counts, expiry) atomically with the ciphertext.

| Setting | Default | Role |
| --- | --- | --- |
| `FLOW_EVIDENCE_MAX_BYTES` | 32 MiB | Compressed evidence cap on the direct path |
| `FLOW_ARTIFACT_EXPANDED_MAX_BYTES` | 2 GiB | Extraction bomb limit (shared) |
| `FLOW_ARTIFACT_ACCOUNT_QUOTA_BYTES` | 4 GiB | Retained encrypted payload per account |
| `FLOW_EVIDENCE_RETENTION_HOURS` | 720 (30 days) | Evidence expiry; `0` expires on the next janitor pass |
| `WORKSPACE_SNAPSHOT_TTL_HOURS` | 24 | Workspace checkpoints only |
| `FLOW_NATIVE_SESSION_RETENTION_HOURS` | 168 | Native session artifacts only |

Evidence retention is independent of workspace checkpoint TTL. Cleanup
nulls ciphertext after expiry once any restore/download lease has lapsed,
and records `availability=expired`. It does not cross account rows.

## Receipts and retrieval

`GET /api/v1/flows/executions/{id}/evidence-status` and the `evidence`
object on `GET /api/v1/flows/executions/{id}/result` report **persisted**
availability from `flow_execution.evidence_receipt` (account-scoped,
no decrypt, no archive hash). Status polls are not an integrity proof.
`integrity_verified` is always false on those endpoints.

| `status` | HTTP on download | Meaning |
| --- | --- | --- |
| `available` | 200 | Bytes present; digest is verified only on download |
| `missing` | 404 `evidence_missing` | No pack was captured |
| `expired` | 410 `evidence_expired` | Retention elapsed; ciphertext removed |
| `failed` | 409 `evidence_failed` | Transport, integrity, or persist failed |

Receipt fields include `kind=evidence`, `artifact_id`, `sha256`/`digest`,
`execution_id`, and `status`. Release consumers should treat `available: true`
from a poll as insufficient; they must use the stored artifact id and digest,
then confirm on download.

`object_lock` and `legal_hold` are always `false`. Do not treat a passing
CRA `result.json` as proof the pack is available: check the receipt, then
download. Fail-result runs retain evidence the same way as pass runs.

`GET /api/v1/flows/executions/{id}/evidence` decrypts, re-checks the
digest, and returns `X-Preloop-Evidence-SHA256`,
`X-Preloop-Evidence-Kind: evidence`, and
`X-Preloop-Evidence-Integrity: verified`. Legacy column bytes are still
served when no durable artifact exists. Other accounts and executions
are refused.

A local `/tmp/preloop-evidence-reference.json` marker is not proof of
upload. The server verifies capability scope (account, flow, thread,
execution, `kind=evidence`) and the archive digest on PUT and GET.
Direct-upload failure emits `evidence error` / `result error` markers
and a `failed` or `missing` receipt — never cleartext pack bytes on the
log channel.

## Operator checklist

1. Enable `FLOW_ARTIFACT_DIRECT_UPLOAD` only after runners can reach the
   API (`PRELOOP_URL`).
2. Set `FLOW_EVIDENCE_RETENTION_HOURS` to the review window you actually
   keep. This is operational retention, not a compliance archive.
3. Protect `SECURITY__ENCRYPTION_KEY` separately from the database and
   retain it across restarts; rotation must still decrypt old artifacts.
4. Confirm `GET .../evidence-status` is `available` before a release
   consumer accepts a pack. A 404/409/410 is a release blocker, not a
   skippable warning.
5. Keep Kubernetes RBAC for pod logs tight on clusters that still run the
   legacy log channel (`FLOW_ARTIFACT_DIRECT_UPLOAD=false`).
