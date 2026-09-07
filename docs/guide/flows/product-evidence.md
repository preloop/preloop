# Product evidence mapping and provenance limits

This runbook is for **product-mode** CRA audits: one supported release
built from several code repositories plus a dedicated compliance
repository. It does not replace
[security-audit-presets.md](security-audit-presets.md). That guide still
owns the four preset contracts. This page is the mapping and publication
contract those presets rely on when the unit of analysis is a product.

## What the mapping is

An optional trigger payload field `product_provenance`
(`preloop.cra.product_provenance/v1`) names:

- the **product**
- the **supported-release** identifier
- the **build** identity your CI already has (id, optional URL)
- the **SBOM digest** (`sha256:` plus 64 hex) and the workspace path of
  the supplied SBOM artifact
- every **constituent repository** with its credential-free HTTPS remote,
  exact 40-hex SHA, clone path, and role (`code` or `compliance`)

When the field is absent the flow keeps legacy behaviour: one bound
repository, or an artifact-only audit with no git checkouts.

## Example (synthetic)

Two code repositories and one compliance repository for
`example-product` release `1.4.2`:

```json
{
  "product_provenance": {
    "schema": "preloop.cra.product_provenance/v1",
    "product": { "name": "example-product" },
    "release": { "identifier": "1.4.2", "channel": "supported" },
    "build": { "id": "build-2026-09-07.14" },
    "sbom": {
      "digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "path": "sbom/image.spdx.json"
    },
    "repositories": [
      {
        "remote": "https://github.com/example/firmware.git",
        "sha": "1111111111111111111111111111111111111111",
        "clone_path": "firmware",
        "role": "code"
      },
      {
        "remote": "https://github.com/example/companion-app.git",
        "sha": "2222222222222222222222222222222222222222",
        "clone_path": "companion-app",
        "role": "code"
      },
      {
        "remote": "https://github.com/example/product-compliance.git",
        "sha": "3333333333333333333333333333333333333333",
        "clone_path": "compliance",
        "role": "compliance"
      }
    ]
  },
  "workspace_files": [
    {
      "path": "sbom/image.spdx.json",
      "content_base64": "<base64 of the CI-emitted SBOM>"
    }
  ]
}
```

Attach the same remotes on the flow's `git_clone_config.repositories`
(with `clone_path: compliance` for the compliance repo) and isolated
publication mode. The platform checks:

- every mapped remote is an authorized repository on this account/flow
- clone paths are unique and match the flow config
- each mapped SHA equals the trusted checkout SHA (the source-branch
  commit the control plane resolved before the agent ran)
- the mapped SBOM digest equals the sha256 of the supplied artifact bytes

Ambiguous, duplicate, or mismatched mappings fail the execution. A
repository outside the manifest or account is never published.

## True provenance limits

These are the claims this mapping **can** support:

- "This audit ran against these remotes at these SHAs, which matched
  the trusted clone."
- "This SBOM file, with this digest, was the artifact supplied to the
  run."
- "These platform approval ids, reviewers (user ids), times, and
  decisions are copied from Preloop approval rows."
- "These remote commits / pull requests were published by the isolated
  publisher, with a receipt per repository."

These are claims it **cannot** support, and must not be written as if
it could:

- Cryptographic **build** attestation that the SBOM was produced from
  those SHAs. That link is only as strong as the build metadata your CI
  delivers. An agent-written SHA in `result.json` or an evidence stub is
  a **declaration**, recorded as `declared_unverified` unless the
  platform matched it against a trusted fact.
- Human approval invented from model output. Reviewer names and
  timestamps come from the approval audit trail, or they are omitted.
- Certification, CE marking, or a completed conformity assessment.
- Legal hold, object-lock, or WORM retention of evidence blobs. Blob
  storage and availability receipts are the evidence workstream; the
  dossier manifest reports interoperable digests and
  `evidence_integration` fields for that binding.
- A successful **product** publication when any one repository failed
  to push or open its pull request. Local commits are not success.
  Partial remote receipts stay on the execution; the run is failed.
  Retry is idempotent: a remote already at the verified head is not
  published again, and a remote that is not in the manifest cannot be
  added on retry.
- Automatic merge. Existing branch and pull-request approval gates
  still apply. Isolated publication never merges.

## Isolated multi-repo publication

Hosted isolated publication exports one git bundle per authorized
checkout, verifies each bundle against the trusted profile, then
publishes with a repository-scoped GitHub App lease. Write credentials
never enter the agent environment. The private-runner isolated protocol
remains single-repository; product topology uses hosted isolated
publication (or legacy in-container publication, which is a different
trust boundary).

Per-repo receipts include the remote URL, PR URL, number, branch, and
head SHA. `trusted_publication.complete` is true only when every
authorized repository published.

## Dossier manifest

After a run the control plane writes `dossier_manifest`
(`preloop.cra.dossier_manifest/v1`) onto the execution result. It
hashes the redacted result, the verified mapping, artifact refs, and
platform approvals. Sensitive fields are redacted. Evidence workers
should bind blob storage to `evidence_integration.manifest_digest` and
`content_digest`; this workstream does not store the bytes.
