# Sample signed evidence pack

Editions: OSS, Cloud, Enterprise.

This is a signed evidence pack you can check yourself, without an account
and without asking us. We pointed our own Release Security Audit at our own
release on Preloop Cloud, in our own account, and published what it wrote.

| | |
| --- | --- |
| Preset | Release Security Audit (`backend/presets/006-release-security-audit.yaml`) |
| Target | Public release [v0.17.1](https://github.com/preloop/preloop/releases/tag/v0.17.1), commit `b46315580acb3b45cfad7590ca6829d0cef9374d` |
| Inputs | The three CycloneDX SBOMs (backend, frontend, CLI) and the frontend OpenVEX document attached to that release, each matching the release `SHA256SUMS`. The flow's setup step checked each SBOM's sha256 before the agent started |
| Run | Execution `122c43aa-cfae-46d5-a96e-d494875fa61a`, 2026-11-04 02:10:14Z to 02:27:15Z UTC (17 minutes), DeepSeek Flash model |
| Signing key | `psk_e6630dfe7a76b604`, the Ed25519 key of the account that ran it |

## What the audit found

The verdict is **fail**, and that is the useful part of the sample. The
severity gate failed on two unwaived findings with CVSS 9.0 or higher in
the v0.17.1 backend: Authlib 1.8.0
([GHSA-6v38-4h7w-rj9h](https://osv.dev/vulnerability/GHSA-6v38-4h7w-rj9h))
and litellm 1.101.0
([GHSA-7hp6-4w63-5g45](https://osv.dev/vulnerability/GHSA-7hp6-4w63-5g45)).
Both are pinned to fixed versions on `main` (Authlib 1.9.1, litellm
1.103.9).

Across 576 components the run reported 71 findings (2 critical, 24 high,
38 medium, 7 low). None is listed in CISA KEV. The OpenVEX document was
applied before the gate and suppressed 25 findings, matched by package and
vulnerability id. Its statement ids still name frontend `0.16.0`, although
the file ships in the v0.17.1 `SHA256SUMS`, so the suppressions rest on a
human confirming the document was meant for this release. The SBOM audit itself (validity and minimum
elements) passed with findings. No repository checkout and no previous
result were attached, so the gap register and drift were skipped, and the
report says so.

## The files

All four sit next to this page:

| File | What it is |
| --- | --- |
| [`evidence.tar.gz`](sample-evidence-pack/evidence.tar.gz) | The pack the audit wrote, 58801 bytes: `manifest.json`, `result.json`, and `evidence/` with the report, findings, the per-source coverage matrix and the setup log |
| [`evidence-signature.json`](sample-evidence-pack/evidence-signature.json) | The signature Preloop made over the pack when it was captured |
| [`account-key.pub`](sample-evidence-pack/account-key.pub) | The account's Ed25519 public key, base64 |
| [`signing-keys.json`](sample-evidence-pack/signing-keys.json) | The same key as `GET /api/v1/signing/keys` served it |

The pack format is described in
[Evidence storage and retention](../guide/flows/evidence-storage.md#what-is-in-a-pack)
and [Signed records](../guide/flows/evidence-storage.md#signed-records).

## Check it

**1. Hash the pack.**

```bash
shasum -a 256 evidence.tar.gz
```

Expected: `e41de880b9e961d07239afe355023f18eec30d12c2b9083964268f7a92876624`.
That is the `archive_sha256` inside the signed payload of
`evidence-signature.json`.

**2. Check the signature, offline.** This needs Python and the
`cryptography` package, and nothing from us:

```python
import base64, hashlib, json
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

doc = json.load(open("evidence-signature.json"))
archive = hashlib.sha256(open("evidence.tar.gz", "rb").read()).hexdigest()
assert doc["payload"]["archive_sha256"] == archive, "pack does not match"
canonical = json.dumps(
    doc["payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
)
digest = hashlib.sha256(canonical.encode()).hexdigest()
assert digest == doc["digest"], "payload does not match"
signed = f"preloop.signature/v1\n{doc['payload_type']}\n{digest}\n{doc['signed_at']}"
key = Ed25519PublicKey.from_public_bytes(
    base64.b64decode(open("account-key.pub").read().strip())
)
key.verify(base64.b64decode(doc["signature"]), signed.encode())  # raises if forged
print("ok", archive, doc["key_id"], doc["signed_at"])
```

Expected output:

```
ok e41de880b9e961d07239afe355023f18eec30d12c2b9083964268f7a92876624 psk_e6630dfe7a76b604 2026-11-04T02:27:08Z
```

Append one byte to the pack and the first assertion fails.

**3. With the CLI, as the account holder.** The `preloop` CLI reads the
pack's signature from the execution's receipt, so this step needs access to
the account that ran it. `--public-key` pins the key instead of trusting
the one the server serves today:

```bash
preloop evidence verify evidence.tar.gz \
  --execution 122c43aa-cfae-46d5-a96e-d494875fa61a \
  --public-key ./account-key.pub
```

```
File:   evidence.tar.gz
sha256: e41de880b9e961d07239afe355023f18eec30d12c2b9083964268f7a92876624

Signature verified with key psk_e6630dfe7a76b604
Signed at 2026-11-04T02:27:08Z

What this shows: this archive is the evidence pack Preloop captured for that execution. Not that its contents were true when captured.
```

The exit status is 0. With one byte appended, the same command prints
`SIGNATURE NOT VERIFIED`, names both digests, and exits 1.

## What this proves and what it does not

A pass shows that these bytes are the ones Preloop signed with this key at
capture and that nothing has changed since. It does not show that the
audit's conclusions are right: read `result.json` and
`evidence/audit-report.md` for that, and see
[What this proves and what it does not](../guide/flows/evidence-storage.md#what-this-proves-and-what-it-does-not).
The key lives on the same platform that wrote the records, so it shows
integrity after signing, not honesty before it.

The audit output is machine-generated evidence for conformity assessment
support. It is not a conformity assessment, certification, or legal advice.
