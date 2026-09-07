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
- each mapped SHA equals the immutable commit the control plane pinned
  and later observed in a frozen checkout bundle. A moving branch tip
  is never a verified checkout.
- the mapped SBOM digest equals the sha256 of the supplied artifact bytes

Ambiguous, duplicate, or mismatched mappings fail the execution. A
repository outside the manifest or account is never published.

## True provenance limits

These are the claims this mapping **can** support:

- "This audit ran against these remotes at these SHAs, which matched
  frozen checkout bundles (or, before freeze, a controller-resolved pin
  recorded as `pin_matched`, not `verified`)."
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
- Legal hold, object-lock, or WORM retention of evidence blobs. The
  dossier copies a server-owned `kind=evidence` receipt (`sha256`,
  status, retention hours, `integrity_verified`) from
  `inspect_evidence` / `load_evidence`. Availability polls are not
  download integrity. `retained` is true only after a verified receipt.
- A successful **product** publication when any one repository failed
  to push or open its pull request. Local commits are not success.
  Partial remote receipts stay on the execution; the run is failed.
  Retry is idempotent: a remote already at the verified head is not
  published again, and a remote that is not in the manifest cannot be
  added on retry.
- Automatic merge. Existing branch and pull-request approval gates
  still apply. Isolated publication never merges.

## Isolated multi-repo publication

Hosted and private isolated publication export one git bundle per
authorized checkout (`evidence/repos/<clone_path>/branch.bundle`),
verify each bundle, then publish with a repository-scoped GitHub App
lease. Write credentials never enter the agent environment. The private
runner protocol repeats freeze/verify/publish per target; credentials
stay on the controller. Partial remote failure keeps the per-repo
receipt. A later execution resume reuses each original
branch/base/expected head/history. Already-published remotes are not
opened as duplicate pull requests. Adding, removing, or remapping
constituent repositories on resume is refused.

When `git_clone_config.publication_approval` is `true`, `required`, or
`require`, a human platform approval must cover every candidate that is
about to receive a writer lease: repository URL, destination branch,
base branch, and the frozen head SHA that will be pushed. An approval
bound only to the original source base does not authorize a later
candidate head. Unpaired repository and commit lists, swapped
repo-to-SHA pairings, expired rows, declined rows, and AI-decided rows
do not authorize. A human decision has no `auto_approved_reason`; an
empty or whitespace reason is not human. If the saved execution, flow,
or clone config cannot be read, the writer lease is refused. Default
flows omit this field and keep existing publication behaviour.

### Supported tool: `request_approval`

Call the builtin `request_approval` tool with optional
`publication_candidates`. That parameter is the only publication
authority. Text or JSON in `context` is not. Ordinary
`request_approval` callers that omit the parameter are unchanged and
cannot authorize a writer lease.

Runnable payload (synthetic remotes and SHAs):

```json
{
  "operation": "publish isolated product repositories",
  "context": "frozen checkouts are ready to receive writer leases",
  "reasoning": "human review of destinations and commits before mint",
  "publication_candidates": [
    {
      "repository_url": "https://github.com/example/firmware.git",
      "branch": "preloop/change",
      "base": "main",
      "head_sha": "1111111111111111111111111111111111111111"
    },
    {
      "repository_url": "https://github.com/example/companion-app.git",
      "branch": "preloop/change",
      "base": "main",
      "head_sha": "2222222222222222222222222222222222222222"
    }
  ]
}
```

The tool stores `action: isolated_publication` plus those exact
`(repository_url, branch, base, head_sha)` tuples on a normal pending
`ApprovalRequest` for the current execution. Invalid tuples return an
error and do not create a row.

**Human workflow:** a reviewer opens the pending request in the console
(`/console/approval/<id>`) or the in-session notice, confirms the listed
destinations and frozen SHAs, and approves through the ordinary
approval surface. Auto-approved and AI-decided rows still cannot
satisfy publication. After approval, isolated publication compares the
saved tuples to the candidates about to be minted; a modified SHA, a
swapped pairing, a source-base tuple, or a row from another execution
is refused.

Per-repo receipts include the remote URL, PR URL, number, branch, base,
records, and head SHA. `trusted_publication.complete` is true only when
every authorized repository published.

## Dossier manifest

The control plane writes `dossier_manifest`
(`preloop.cra.dossier_manifest/v1`) only when the run has an explicit
product mapping, isolated publication, a CRA result schema, or a caller
that supplied product-evidence context. Ordinary flows keep their
existing result objects and do not load approval or evidence records
for a dossier.

The dossier records separate digests for the raw agent result and the
annotated control-plane result (mapping and publication receipts).
Sensitive fields are redacted. The dossier does not hash itself.
Evidence fields come from a `kind=evidence` receipt; missing or
unverified evidence is reported as not retained.
