# Sample signed evidence pack

Editions: OSS, Cloud, Enterprise.

This is a signed evidence pack you can check yourself, without an account
and without asking us. It was produced by the Release Security Audit preset
(`backend/presets/006-release-security-audit.yaml`) on a local Preloop
0.17.1 stack, auditing the public release
[v0.17.1](https://github.com/preloop/preloop/releases/tag/v0.17.1) of this
repository (commit `b46315580acb3b45cfad7590ca6829d0cef9374d`). The inputs
were the three CycloneDX SBOMs and the frontend OpenVEX document attached to
that release, each matching the release `SHA256SUMS`.

The account that signed it was created for this sample. Its public key is
published here, next to this page:

- [`account-key.pub`](sample-evidence-pack/account-key.pub): the Ed25519
  public key, base64
- [`signing-keys.json`](sample-evidence-pack/signing-keys.json): the same key
  as `GET /api/v1/signing/keys` served it, with key id
  `psk_05756458866da20e`

## What is in the bundle

| File | What it is |
| --- | --- |
| `evidence.tar.gz` | The evidence pack the audit wrote: `manifest.json`, `result.json`, and `evidence/` with the report, findings, source matrix, waivers and gap register |
| `evidence-signature.json` | The detached signature Preloop made over the pack when it was captured |
| `period-export.tar.gz` | A signed period export of the same account: audit rows, approvals, and the evidence receipts that name the pack's sha256. It carries its own `manifest.json` and `signature.json` |
| `account-key.pub` | The public key above |

The format of both archives is described in
[Evidence storage and retention](../guide/flows/evidence-storage.md#what-is-in-a-pack)
and [Signed records](../guide/flows/evidence-storage.md#signed-records).

## Check it

**1. The period export, offline, with the published key.** This needs the
`preloop` CLI and nothing else. `--offline` makes sure it never contacts a
server.

```bash
preloop evidence verify period-export.tar.gz --public-key ./account-key.pub --offline
```

Expected output:

```
File:   period-export.tar.gz
sha256: fbc5a3f5ad91374634e07444cca666c5c90768a2c833e475e6357166ad650c10
  approvals/approval_request.jsonl                0 bytes  ok
  artifacts/manifest.json                         2 bytes  ok
  audit/audit_log.jsonl                      261919 bytes  ok
  evidence/receipts.jsonl                      2476 bytes  ok
  holds/legal_hold.jsonl                          0 bytes  ok
  member list digest: ok

Signature verified with key psk_05756458866da20e
Signed at 2026-11-03T22:17:34Z

What this shows: the archive is the one Preloop built and nothing in it has moved since. Not that the records were true when written.
```

The exit status is 0. Change one byte of any member and the same command
names the member as `ALTERED` and exits 1.

**2. The pack is the one the signed export names.** The receipts in the
verified export list the sha256 of every evidence pack captured in the
period. Hash the pack and find it there:

```bash
shasum -a 256 evidence.tar.gz
tar xzf period-export.tar.gz -O evidence/receipts.jsonl | grep "$(shasum -a 256 evidence.tar.gz | cut -d' ' -f1)"
```

**3. The pack's own signature, by hand.** `preloop evidence verify
--execution` reads the pack signature from the API, so it cannot run
offline. The signature document in the bundle can be checked with a few
lines of Python and the `cryptography` package:

```python
import base64, hashlib, json, sys
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

doc = json.load(open("evidence-signature.json"))
archive = hashlib.sha256(open("evidence.tar.gz", "rb").read()).hexdigest()
assert doc["payload"]["archive_sha256"] == archive, "pack does not match"
canonical = json.dumps(doc["payload"], sort_keys=True, separators=(",", ":"))
digest = hashlib.sha256(canonical.encode()).hexdigest()
assert digest == doc["digest"], "payload does not match"
signed = f"preloop.signature/v1\n{doc['payload_type']}\n{digest}\n{doc['signed_at']}"
key = Ed25519PublicKey.from_public_bytes(
    base64.b64decode(open("account-key.pub").read().strip())
)
key.verify(base64.b64decode(doc["signature"]), signed.encode())  # raises if forged
print("ok", archive, doc["key_id"], doc["signed_at"])
```

## What this proves and what it does not

A pass shows that these bytes are the ones Preloop signed with this key and
that nothing has changed since. It does not show that the audit's
conclusions are right: read `result.json` and the report for that, and see
[What this proves and what it does not](../guide/flows/evidence-storage.md#what-this-proves-and-what-it-does-not).
The key was made on the same stack that wrote the records, so it shows
integrity after signing, not honesty before it.

The audit output is machine-generated evidence for conformity assessment
support. It is not a conformity assessment, certification, or legal advice.
