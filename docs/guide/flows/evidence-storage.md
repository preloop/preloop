# Evidence storage and retention

Audit-style flows write a human-readable pack under `/workspace/evidence/`
plus `/workspace/result.json`. This page is the operator runbook for how
that pack is transported, stored, retrieved and retained. It does **not**
claim WORM, object-lock, certification, or CRA Article 14 filing. It does
now carry a legal hold, which is a Preloop-level control and not a storage
guarantee: see [Retention and legal hold](#retention-and-legal-hold) for
exactly what that does and does not mean. Cross-link the
[security audit presets](security-audit-presets.md) guide for the JSON
contracts themselves.

## Transports

**Legacy log channel (default).** `FLOW_ARTIFACT_DIRECT_UPLOAD` is off and
`FLOW_EVIDENCE_LOG_PLAINTEXT` is on (the default). Hosted Docker copies the
directory through the engine API and does not put the pack on container
logs, so the plaintext switch does not change Docker capture. Kubernetes
still emits a size-capped base64 block on the pod log channel
(`MAX_EVIDENCE_ARCHIVE_BYTES`, 2 MiB compressed) for `result.json`, the
evidence pack, and the workspace snapshot. Base64 is not encryption. Anyone
who can read retained pod logs can read those bytes. The control plane
stores the evidence bytes on `flow_execution.evidence_archive`. Failed
persist is visible as `evidence-status: failed` or `missing`; it must not
look like a successful receipt. Existing downloads keep working.

**Plaintext log channel off.** Set `FLOW_EVIDENCE_LOG_PLAINTEXT=false`
(`flow_evidence_log_plaintext`) when a deployment must not put evidence in
pod logs. Use it together with direct upload. The Kubernetes job receives
`PRELOOP_EVIDENCE_LOG_PLAINTEXT=0`. If that job also has an upload token,
the wrapper follows the direct path and still does not fall back to
plaintext when the upload fails. If the token is absent, the wrapper fails
closed: it does not base64 `result.json`, the evidence pack, or the
workspace snapshot. The pod log gets three markers instead:
`result unavailable plaintext_disabled`, `evidence unavailable
plaintext_disabled`, and `workspace skipped plaintext_disabled`. The
control plane records an evidence receipt with status `failed` and error
`plaintext_disabled`, and it treats the result as missing (the same outcome
as no `result.json`). It does not decode a payload that someone injects
into the log. **Unavailable: plaintext disabled** means the pack was
refused by policy, not that the agent forgot to write `/workspace/evidence`.
Turning the switch off without direct upload makes evidence unavailable by
design. Encrypted log transport (per-execution keys) is a separate decision
tracked in issue #268 and is not this switch.

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

`/api/v1/flows/executions/{id}/artifacts` is that transport and nothing
else. An account bearer token, however privileged, is refused with `401`
and a body naming the operator endpoints instead
(`.../evidence`, `.../evidence-status`), and the pair is deliberately absent
from `openapi.yaml`: it is not a read path for people or SDKs. Undocumented
is not disabled, the runner still calls it.

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
| `FLOW_EVIDENCE_LOG_PLAINTEXT` | true | Kubernetes pod-log base64 channel. Default keeps today's emission. False refuses it |
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
availability from `flow_execution.evidence_receipt` (account-scoped).
A poll on a direct-transport pack does not decrypt anything, so it is not
an integrity proof for that pack.

Both endpoints carry three integrity fields:

| Field | Meaning |
| --- | --- |
| `integrity` | `verified`, `not_checked`, or `failed` |
| `integrity_note` | The same thing in one plain sentence |
| `integrity_verified` | Legacy boolean, true only for `verified` |

`not_checked` is the answer for the direct transport: nobody read the
ciphertext, and the digest is confirmed on download. It does not mean the
pack is suspect. `failed` means the archive was read and its sha256 did not
match the recorded digest; the status flips to `failed` and `observed_sha256`
carries what was actually found.

On the legacy transport the compressed archive sits in the same row as the
receipt, so the poll hashes it and answers `verified` or `failed` rather than
declining to look. Before this, a legacy pack whose download returned
`X-Preloop-Evidence-Integrity: verified` was reported by the status endpoint
as `integrity_verified: false`, which reads as a corrupt pack.

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

`object_lock` is always `false` (see
[Retention and legal hold](#retention-and-legal-hold)). `legal_hold` is
`true` while a hold covers the pack or its execution, and false otherwise.
A held pack is not reported `expired` and its ciphertext is not cleared,
so `available` on a held pack means the bytes are still there. Do not treat a passing
CRA `result.json` as proof the pack is available: check the receipt, then
download. Fail-result runs retain evidence the same way as pass runs.

`GET /api/v1/flows/executions/{id}/evidence` decrypts, re-checks the
digest, and returns `X-Preloop-Evidence-SHA256`,
`X-Preloop-Evidence-Kind: evidence`, and
`X-Preloop-Evidence-Integrity: verified`, plus
`X-Preloop-Evidence-Integrity-State` carrying the same three-state word as
the status endpoint. Legacy column bytes are still served when no durable
artifact exists. Other accounts and executions are refused.

A local `/tmp/preloop-evidence-reference.json` marker is not proof of
upload. The server verifies capability scope (account, flow, thread,
execution, `kind=evidence`) and the archive digest on PUT and GET.
Direct-upload failure emits `evidence error` / `result error` markers
and a `failed` or `missing` receipt — never cleartext pack bytes on the
log channel.

## Retention and legal hold

Two different clocks, and confusing them is the mistake this section exists
to prevent.

**The evidence payload window** is `FLOW_EVIDENCE_RETENTION_HOURS` (720, 30
days). It governs the encrypted bytes and it is an operational review window,
sized for the people who read packs, not for an archive.

**Record retention** is per account and per record class, in days, with a
**floor of 183 days (six months)** and a default of 365. It governs the
records: audit rows, approval requests, evidence pack rows (manifest, digest,
receipt metadata), runtime sessions and usage rows. The floor exists because
AI Act Art. 26(6) asks a deployer to keep automatically generated logs for at
least six months and DORA asks for comparable record keeping. Nothing can be
set below it. A deployment may raise the floor with `RETENTION_FLOOR_DAYS`; it
cannot lower it.

So an evidence pack row survives for the record retention while the encrypted
pack itself is cleared after the payload window. That is deliberate. Keeping
every pack for six months by default would multiply stored bytes against the
per-account artifact quota on upgrade, without anybody asking for it. If a
specific pack has to survive, place a legal hold on it or export the period.

| Record class | Covers |
| --- | --- |
| `audit` | Audit log rows |
| `approvals` | Approval requests and their events |
| `evidence` | Evidence pack records (manifest and digest), not the payload |
| `runtime_sessions` | Runtime sessions, session activity, session artifacts (removed with the session), and the session search chunks derived from them |
| `usage` | API and gateway usage rows, and the search chunks quoting them |

```
GET  /api/v1/retention/settings        # resolved days per class, plus the floor
PUT  /api/v1/retention/settings        # {"classes": {"audit": 400}}; below the floor is a 422
GET  /api/v1/retention/purge-preview   # what today's purge would remove, per class
```

### In the console

Settings > Records edits days per class, never below the floor, and previews
what a purge would remove. Purge itself stays a deployment setting. The page
says when the sweeper is off, so a stated policy is not mistaken for a
deletion that already happened.

### The purge

Records past retention are deleted by a background sweeper, never on a
request. It is **off by default**: set `RETENTION_PURGE_ENABLED=true` to turn
it on. An upgrade must not silently start deleting audit history, so until an
operator enables it, retention is a stated policy that nothing enforces, and
`GET /api/v1/retention/settings` says so in `purge_enabled`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `RETENTION_PURGE_ENABLED` | `false` | Nothing is deleted while this is false |
| `RETENTION_PURGE_DRY_RUN` | `false` | Count and audit, delete nothing |
| `RETENTION_PURGE_WINDOW_UTC` | `1-5` | Off-peak UTC hours; empty means any hour |
| `RETENTION_PURGE_INTERVAL_SECONDS` | `3600` | Time between passes |
| `RETENTION_PURGE_BATCH_SIZE` | `1000` | Rows per DELETE |
| `RETENTION_PURGE_MAX_BATCHES` | `50` | Batch ceiling per class per pass |
| `RETENTION_PURGE_MAX_SECONDS` | `300` | Wall-clock budget per pass |

A pass that hits a bound stops and resumes next time rather than running long.
Every pass that removed anything writes an audit row per record class with the
cutoff and the count, so the deletion of records is itself a record. The audit
row also carries `derived_deleted`: rows removed from tables that quote the
records, counted separately so a report says how many sessions went without
inflating the number by their search chunks. A runtime-session purge also
names `runtime_session_artifact`, the artifact rows the session delete
cascades.

### Legal hold

A hold freezes one execution, approval request, evidence pack or runtime
session. While it is in force the purge skips the row and the janitor leaves
the ciphertext alone past `expires_at`, so a held pack stays downloadable. A
reason is mandatory, the actor is recorded, and both placing and releasing
write audit rows.

```
GET  /api/v1/retention/holds
POST /api/v1/retention/holds                  # {"resource_type": "execution", "resource_id": "...", "reason": "..."}
POST /api/v1/retention/holds/{id}/release     # {"reason": "..."}
```

### In the console

Settings > Records lists legal holds, and the same page is where retention
days are edited. A flow execution, an approval, and a runtime session each
offer place and release for that one record. A hold is still not object lock:
the execution page shows `object_lock` as false.

A hold on an execution also covers that execution's evidence packs. Holds
overlap safely: releasing an execution hold does not unfreeze a pack that
carries its own hold. A hold on a runtime session covers that session's
activity rows, which the purge only ever removes with the session itself, and
the session reads back with `legal_hold: true` so a frozen session looks
frozen wherever it is listed. The same hold flags the session's artifacts,
including artifacts stored after the hold is placed, and the expiry janitor
leaves their ciphertext alone past `expires_at`.

**What a legal hold is not.** It is a Preloop control, enforced by Preloop
code against the Preloop database. It is not WORM, and it is not S3 Object
Lock. `object_lock` stays `false` on every receipt because Preloop cannot
verify a property of the storage layer beneath it: an operator with database
access can still delete a held row, and a backup restore can still reintroduce
a purged one. If your obligation requires immutability that survives a
platform administrator, put that control in the storage layer and use the
period export to hold the record somewhere Preloop cannot reach.

### What search can reach after a purge

Session content is indexed into a search corpus as it is written: chunks of
gateway interactions, transcript messages, tool calls, operator notes and
session summaries, each one account scoped and session scoped. That corpus is
a second copy of the records, so it follows the same rules rather than rules
of its own.

**A purge takes the chunks with the record, in the same transaction.** When
the `runtime_sessions` pass deletes a session, its chunks go in the same
batch, not on a later sweep. When the `usage` pass deletes a usage row, the
gateway chunks quoting it go with it. So after a purge, search cannot quote a
record the account was told was deleted, and there is no window in which it
still can.

**A hold preserves the chunks too.** A held session keeps its chunks exactly
as it keeps its activity rows. This holds across classes: a held session's
gateway chunks survive the `usage` pass even when the usage row itself is past
its cutoff, so such a chunk can outlive the row it quotes for as long as the
hold lasts. That is the intended direction. A hold is an instruction to
preserve the record and a retention cutoff on a different class does not
overrule it. Release the hold and the next usage pass takes the chunk.

**Redaction reaches the copy.** A source redacted after it was indexed is
either re-indexed, so the chunks hold the redacted text, or dropped. Where
neither applies, the stored text is cleared in place and the chunks are marked
`withheld`: the rows remain, so search still knows that content existed, when,
and in which session, but the text is gone from the database and the read path
returns none for them. Text is returned only for the redaction states named
returnable, which is a whitelist, so a state added later withholds text until
somebody decides otherwise.

**The invariant is checkable, not assumed.**
`preloop.services.session_search_retention.orphan_chunk_report` counts chunks
whose session or usage row no longer exists, excluding chunks a legal hold
deliberately kept after their usage row was purged. The answer after any
purge pass is zero except chunks preserved by a hold;
`assert_no_orphan_chunks` is the same check as an assertion for tests and
for an operator running it against a real database after a pass.

What this does not cover: a backup restored from before a purge reintroduces
the chunks along with the records, exactly as it reintroduces everything else,
and an operator with database access can write to the corpus directly. Both
are the same limit as the rest of this section, for the same reason.

### Period export

`POST /api/v1/retention/exports?start=YYYY-MM-DD&end=YYYY-MM-DD` returns a
`tar.gz` of one period (start inclusive, end exclusive, so consecutive
periods tile without double counting).

```
manifest.json                    schema preloop.retention.period_export_manifest/v1
approvals/approval_request.jsonl
audit/audit_log.jsonl
evidence/receipts.jsonl          receipts, not payloads: artifact id and digest
holds/legal_hold.jsonl
```

`manifest.json` carries `members` with a `sha256` and `size_bytes` per member
and a `members_digest` over that list, the same shape and the same computation
an evidence pack manifest uses, so one verifier covers both. The response
headers repeat the digests (`X-Preloop-Archive-Sha256`,
`X-Preloop-Members-Digest`, `X-Preloop-Manifest-Sha256`). Every export writes
an audit row naming the period, the counts, the archive digest and the key
that signed it.

The bundle also carries `signature.json`: a detached Ed25519 signature over
the sha256 of `manifest.json` as packed, made with the account's signing key
(see [Signed records](#signed-records)). Verify it with
`preloop evidence verify <archive>`, or by hand: digest the manifest bytes,
rebuild the signed bytes, check them against the published public key. The
signature member is not listed in `members`, because it cannot be: it covers
the manifest that would have to list it.

Exports are capped at `RETENTION_EXPORT_MAX_ROWS` (100000) per record class
and 366 days per archive, and going over is an error asking for a narrower
period rather than a truncated archive somebody later mistakes for the whole
period.

Exported audit rows carry their chain position (`chain_seq`, `prev_hash`,
`row_hash`), so a bundle taken today can be checked against a checkpoint kept
years ago without asking Preloop for anything.

## Tamper-evident audit trail

Audit rows are chained per account. A background pass seals rows in timestamp
order: each sealed row gets a `chain_seq`, the `row_hash` of the row before it
as `prev_hash`, and its own `row_hash` over a canonical serialisation of the
record. Editing a sealed row, deleting one from the middle, or reordering two
breaks every hash from that point on.

```
GET  /api/v1/audit/chain/status        head, purge floor, sealing lag, newest checkpoint
GET  /api/v1/audit/chain/verify        a server-side walk over a range
GET  /api/v1/audit/chain/segment       canonical payloads and stored hashes, for your own walk
GET  /api/v1/audit/chain/checkpoints   signed anchors over the chain head
```

`preloop audit verify` uses the segment endpoint rather than the verdict: it
recomputes every hash on your machine, checks the checkpoint signatures, and
reports the first break with its sequence and row id. Exit status is 1 on a
break, so CI can gate on it. When Preloop's verdict and the local walk
disagree, the CLI prints both and tells you to trust the walk.

```
preloop audit verify
preloop audit verify --start-seq 1000 --end-seq 2000 --json
```

Two ranges are outside any result, and both are stated in the output rather
than glossed over. Rows below `pruned_below_seq` were removed by the retention
purge under a stated policy: the purge raises that floor as it deletes, so
enforcing retention does not read as tampering. Rows written since the last
sealing pass are not chained yet (`unsealed_rows`).

### In the console

Settings > Records, under Audit integrity, shows sealed and unsealed counts,
the seal lag, and the latest checkpoint. Verify chain runs on the server.
Verify offline shows `preloop audit verify` and the account key id, with a
download of the public key. The audit timeline links there. A clean result
shows the rows were not reordered, removed or edited after sealing; not that
they were true when written.

Every `AUDIT_CHAIN_CHECKPOINT_INTERVAL` sealed rows, Preloop signs a
checkpoint over the chain head. A checkpoint you copied off the platform is
the one artifact here that a rewritten chain cannot reproduce, because it was
signed before the rewrite and it names the head at that sequence. Fetch and
keep them.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AUDIT_CHAIN_ENABLED` | `true` | Seal rows into the chain. Adds hashes, removes nothing |
| `AUDIT_CHAIN_SEAL_INTERVAL_SECONDS` | `60` | Time between sealing passes |
| `AUDIT_CHAIN_SEAL_LAG_SECONDS` | `60` | How far behind now the sealer stays |
| `AUDIT_CHAIN_CHECKPOINT_INTERVAL` | `1000` | Sealed rows between signed checkpoints |
| `AUDIT_CHAIN_VERIFY_MAX_ROWS` | `50000` | Rows one verify request walks before truncating |

## Signed records

Each account has an Ed25519 signing key. The private half is stored encrypted
with `SECURITY__ENCRYPTION_KEY`, like every other secret; the public half is
served to anyone with `view_audit_logs`.

```
GET  /api/v1/signing/keys          every key the account has held, public halves
POST /api/v1/signing/keys/rotate   retire the active key, mint its replacement
```

Rotation keeps old keys listed and old signatures valid. Invalidating them
would revoke the customer's own evidence, which is the opposite of the point.
Every signature names the `key_id` that made it.

A signature covers these bytes and nothing else:

```
preloop.signature/v1\n<payload_type>\n<digest>\n<signed_at>
```

`payload_type` is in there so a signature over a period export manifest cannot
be presented as a signature over an evidence pack. `digest` is the sha256 of
the canonical JSON of the signed payload (sorted keys, no insignificant
whitespace, UTF-8), or of the manifest bytes for a period export.

Evidence packs are signed at capture, not at download, so re-serving a pack
cannot change what was signed. The signature lives beside the pack rather than
inside it: an evidence archive is content addressed the moment it is stored,
and appending a member would change the digest the receipt already promised.
`GET .../evidence-status` and the evidence download return the signature and
`signing_key_id`, and the download repeats them in `X-Preloop-Signature`,
`X-Preloop-Signing-Key-Id` and `X-Preloop-Signed-At`.

```
preloop evidence verify export.tar.gz
preloop evidence verify evidence.tar.gz --execution <execution-id>
preloop evidence verify export.tar.gz --public-key ./account-key.pub
```

`--public-key` is the version worth running. A public key fetched from us at
verification time only shows that the bundle matches whatever key we serve you
today; a key you copied when the bundle was issued does not depend on us at
all. `preloop audit keys` prints them for that purpose.

### In the console

Settings > Records lists the active key and retired keys, and can rotate a
key. Period exports are on the same page: the download names the signing key
from the response headers and shows `preloop evidence verify` with that key
file. The execution page downloads an evidence pack and shows the integrity
header from that download.

Packs captured before signing existed, and accounts whose key could not be
minted, have no signature. The receipt says `signature: null` rather than
pretending, and signing is never a precondition for storing evidence: bytes
that cannot be re-captured outweigh a signature that can be added later.

## What this proves and what it does not

Being precise here matters more than sounding strong, so the limits come
first.

**A compromised server can forge anything before it is signed.** The signing
key lives on the same platform that writes the records. Anyone who can write
an audit row can write a false one, and it will be sealed into the chain and
signed like any other. Nothing in this feature makes Preloop's own claims
trustworthy; it makes them *fixed*. Signing and chaining defend against
changing history after the fact, not against writing it wrong the first time.

**The chain proves order and non-deletion within a range.** A clean walk over
sequences 1000 to 2000 shows that those rows are in the order they were sealed
in, that none was removed from between them, and that none was edited after
sealing. It does not extend past the range: rows below the purge floor are
gone, and rows not yet sealed are outside the chain. It says nothing at all
about whether a row's contents were true.

**A signature proves origin and integrity, not truth.** A verified period
export is the bundle Preloop built, unchanged since. Whether the approvals
inside it reflect what really happened is a question about the platform, not
about the signature.

**A checkpoint is only as good as where you keep it.** Its value comes from
being outside our reach. A checkpoint we hold and a chain we hold prove
consistency between two things under the same control. Copy checkpoints and
public keys somewhere Preloop cannot write.

**None of this is WORM.** An operator with database access can still delete
rows. The difference is that after this change, deleting sealed rows leaves a
gap the next verification names, instead of leaving nothing at all. A gap in
the chain is evidence; it is not prevention. If your obligation needs
immutability that survives a platform administrator, that control belongs in
the storage layer.

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
   legacy log channel (`FLOW_ARTIFACT_DIRECT_UPLOAD=false` and
   `FLOW_EVIDENCE_LOG_PLAINTEXT=true`). To keep evidence bytes out of pod
   logs, set `FLOW_EVIDENCE_LOG_PLAINTEXT=false` and enable direct upload.
   `evidence-status` `failed` with error `plaintext_disabled` means the log
   channel was refused and no upload token was available. That is not a
   successful empty pack.
6. This switch only governs the artifact wrapper. It does not hide the pod
   spec (environment and tokens) from someone who can read the Job, and it
   does not stop the agent from printing sensitive prose on ordinary stdout.
7. Decide record retention per class and set `RETENTION_PURGE_ENABLED`
   deliberately. Until it is on, nothing is deleted and the stated retention
   is not enforced. Run `GET /api/v1/retention/purge-preview`, or Preview
   purge on Settings > Records, before the first enabled pass.
8. Place a legal hold before an incident review starts, not after the
   payload window has closed. A hold pins bytes that are still there; it
   cannot bring back bytes already cleared. Place it from Settings > Records
   or from the execution, approval, or session page, or with
   `POST /api/v1/retention/holds`.
9. Copy signed checkpoints (`GET /api/v1/audit/chain/checkpoints`, or the
   table on Settings > Records) and the public keys (`preloop audit keys`,
   or Download public key on that page) somewhere Preloop cannot write. Held
   only here, they prove consistency between two things under the same
   control. Run `preloop audit verify` on a schedule and treat a break as an
   incident. The console Verify chain button is the server's own walk, not a
   substitute for that command.

### Cloud analytics history and stored records

Cloud plans can limit the age of reports, session replay events, and derived
optimization records visible in the product. With the new pricing ladder
activated, Free includes 183 days, Pro 365 days, and Team and Business 730 days.
Legacy and custom subscriptions retain longer agreed terms. Self-hosted OSS
has no cloud analytics cutoff. An upgrade cannot restore records already
removed; the advertised period is a maximum available window, not a promise
of historical backfill.

This reporting window does not gate the gateway, firewall, approvals, budgets,
session controls, or access to retained audit/evidence exports. A long-lived
session remains usable and exposes its in-window events even if it began
before the cutoff. Direct event IDs obey the same reporting limit.

Usage and session deletion follows the longest of the account/deployment
retention setting, current subscription promise, and any previously preserved
longer promise. Subscription transitions retain that promise atomically in
account metadata. Standalone purge workers also consult stored plan features
through the CRUD layer. Retention settings and purge previews report the
resulting physical retention; `days: -1` denotes an unlimited promise. Audit,
approval, and evidence record classes retain their existing policy, six-month
minimum, and legal-hold behavior. Encrypted evidence payload lifetime remains
a separate setting as described above.

The account's dedicated `subscription_history_retention_days` column preserves
longer physical history promises independently of general account metadata. Its
metadata mirror remains for compatibility. Purge workers take a fresh account
lock and recompute policy in each deletion transaction, skipping busy accounts;
an upgrade committed between batches therefore protects subsequent records.

For rollout, apply the additive account policy migration before starting the new
application. Stop or replace every old purge worker before activating new plans
or recording new promises. Old binaries only understand the metadata mirror and
are not safe purgers after an unrelated metadata replacement. Keep the dedicated
columns and upgraded purge worker during an application rollback; do not reverse
this migration after new promises or billing repair intents have been written.

The migration waits at most five seconds to acquire a busy database lock. Its
backfill only updates accounts with legacy policy/billing metadata. The
500-row batches bound memory, not lock duration: PostgreSQL holds the account
DDL lock until commit. For large account tables, rehearse the migration against
a recent restored snapshot; split additive DDL and an operational backfill if
needed, completing and verifying both before starting the new application or
enabling the pricing flag.
