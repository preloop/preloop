# Security audit presets (SBOM verify, exploit check, release audit, due diligence)

These flow presets turn a CI release build into audit-grade security
evidence. They **verify** SBOMs emitted by your build toolchain
(Yocto/OpenEmbedded `create-spdx`, AOSP SBOM tooling, CycloneDX build
plugins) — they never generate one. Each run is a single execution that
ends by writing `/workspace/result.json` with a versioned schema, captured
as a first-class execution artifact and retrievable via
`GET /api/v1/flows/executions/{execution_id}/result`, plus a
human-readable evidence pack under `/workspace/evidence/`.

| Preset | What it does | result.json schema |
| --- | --- | --- |
| SBOM Verify | Format validity, NTIA/CRA minimum elements, completeness vs build manifests, license flags | `preloop.cra.sbomaudit/v1` |
| SBOM Exploit Check | Components → CVEs via OSV.dev, known-exploited flags via CISA KEV, severity gate | `preloop.cra.vulnscan/v1` |
| Release Security Audit | Both of the above in one execution, plus drift vs a previous run's result.json, plus [multi-repo evidence storage](#evidence-storage-architecture-multi-repo-products) | `preloop.cra.releaseaudit/v1` |
| [Component Due Diligence Record](#component-due-diligence-record) | Agent legwork on one integrated component; a human carries the risk decision via approval; the record lands in the compliance repo | `preloop.cra.duediligence/v1` |

The audit presets follow the Observe/Eval pattern: no write tools;
deterministic checks separated from agent judgment (`checks[]` vs
`assessments[]`); and every artifact carries the line:

> Machine-generated evidence for conformity assessment support. Not a
> conformity assessment, certification, or legal advice.

## Wiring a CI build to a preset

1. Clone the preset into a flow and configure a webhook trigger. Your CI
   job calls `POST /webhooks/flows/{flow_id}/{webhook_secret}` after the
   build; the response carries `execution_id` so the pipeline can poll
   `/api/v1/flows/executions/{execution_id}/result` and gate the release
   on the verdict.
2. Deliver the SBOM either **inline** via the payload's
   [`workspace_files` seed](../../webhook-triggers.md) (base64, 1 MiB
   encoded cap across files) or **by pointer**: put URLs in the payload
   and the agent downloads them at run start.

> **Trust and egress for URL delivery.** Payload URLs are treated as
> hostile input: anyone holding the webhook secret can inject them, and
> the exec sandbox needs egress to the vulnerability sources, so the
> runner does **not** guarantee egress filtering. The prompts instruct
> the agent to fetch only http(s) URLs and to refuse targets resolving
> to loopback, private-range, link-local, or cloud metadata addresses
> (e.g. `169.254.169.254`), but this is prompt-level hardening, not a
> network policy. Prefer inline `workspace_files` delivery where
> integrity matters; a platform-level egress allowlist is an open
> question.

Example payload (field names are conventions the prompt understands; the
agent also searches `/workspace` for SBOM-shaped files):

```json
{
  "release_ref": "v1.2.3",
  "build": {"image_name": "example-image", "image_hash": "sha256:…", "toolchain": "yocto-5.0"},
  "sbom": {"paths": ["sbom/image.spdx.json"]},
  "manifests": {"license_manifest_path": "manifests/license.manifest"},
  "license_policy_path": "policy/licenses.yaml",
  "gate": {"fail_on_kev": true, "fail_on_cvss_gte": 7.0},
  "previous_result_path": "previous/result.json",
  "workspace_files": [
    {"path": "sbom/image.spdx.json", "content_base64": "…"},
    {"path": "manifests/license.manifest", "content_base64": "…"},
    {"path": "previous/result.json", "content_base64": "…"}
  ]
}
```

Recommended CI job outputs to retain and deliver: the SPDX/CycloneDX
file(s), the license manifest (e.g. Yocto `license.manifest`), image or
package manifests for cross-checks, and the previous audit's
`result.json` if you want drift.

### Scheduled re-audits

CRA vulnerability handling continues through the support period even when
you ship nothing. Attach a schedule trigger (cron/interval) to a cloned
**Release Security Audit** or **SBOM Exploit Check** flow that re-delivers
the *last released* SBOM (plus the previous `result.json` for drift). Each
run produces a dated evidence pack; the run-over-run trail is the
auditable record.

## Vulnerability sources (honest notes)

- **OSV.dev** is the primary source: `POST https://api.osv.dev/v1/querybatch`
  with purl or name+ecosystem+version, no API key required. Results record
  `osv_queried_at`.
- **CISA KEV** catalog supplies known-exploited flags; the catalog release
  date is recorded as `kev_snapshot_date`. KEV hits appear in
  `art14_candidates` as a prioritisation signal for a human — not legal
  advice, and Preloop does not file Article 14 reports.
- **EPSS** scores are best-effort (`api.first.org`); `null` when
  unreachable.
- **NVD** is a fallback only: the public API without a key is rate-limited
  to roughly 5 requests per 30 seconds, so runs do not enumerate NVD for a
  whole SBOM. How (and whether) NVD was used is recorded in
  `db_versions.nvd`.
- Components without a version or a purl/CPE identifier cannot be reliably
  matched; they are counted as `unmatchable` and results never claim
  vulnerability absence for them.
- The Release Security Audit additionally reports `db_resolvable`
  coverage (components in ecosystems the advisory databases actually
  index; `pkg:generic` and `pkg:github` are not db-resolvable) and runs
  a mandatory negative control: a known-vulnerable component queried in
  the same style as the inventory. If the control comes back empty the
  method is blind for that class (`method_blind: true`) and those
  components are reported as "not screenable by this method", never as
  "zero vulnerabilities".
- If the sandbox has no egress to these sources, the run reports an error
  rather than fabricating findings or their absence.

## result.json schemas

### Shared evidence envelope

```json
{
  "schema": "preloop.cra.sbomaudit/v1 | preloop.cra.vulnscan/v1 | preloop.cra.releaseaudit/v1 | preloop.cra.duediligence/v1",
  "flow": "sbom-verify | sbom-exploit-check | release-security-audit | component-due-diligence",
  "run_at": "ISO 8601 UTC",
  "git": {"remote": "…", "commit": "…", "branch": "…", "dirty": false},
  "tool_versions": {"<tool>": "<version or 'unavailable: reason'>"},
  "inputs_declared": {"<input>": "<what was actually delivered>"},
  "runner": {"kind": "hosted | self_hosted | null", "id": null},
  "regime_profile": "cra",
  "checks": [{"name": "…", "passed": true, "skipped": false, "details": "evidence"}],
  "assessments": [{"topic": "…", "judgment": "agent judgment, marked as such"}],
  "artifacts": {"<name>": "<workspace-relative path>"},
  "disclaimer": "Machine-generated evidence for conformity assessment support. Not a conformity assessment, certification, or legal advice."
}
```

`git` is `null` unless a repository was cloned into the workspace.
Unknown envelope fields are `null`, never invented. `checks[]` are
deterministic facts; `assessments[]` are agent judgment. `result.json`
stays under 200 KB; large listings live in `/workspace/evidence/` files
referenced from `artifacts`.

### `preloop.cra.sbomaudit/v1` (SBOM Verify)

Envelope plus:

```json
{
  "source": {"format": "spdx | cyclonedx", "spec_version": "SPDX-2.3", "generator_tool": null, "build_ref": null},
  "valid": true,
  "minimum_elements": {"passed": false, "missing": ["supplier: 12 components"]},
  "coverage": {
    "components": 214,
    "pct_with_version": 99.1,
    "pct_with_license": 92.5,
    "pct_with_identifier": 88.3,
    "unmatched_vs_build": ["kernel-module-foo"]
  },
  "license_flags": [{"component": "…", "license": "…", "flag": "deny | flag | missing"}],
  "delta": null,
  "verdict": "pass | pass_with_findings | fail"
}
```

Verdict semantics: `fail` = invalid SBOM or minimum elements absent;
`pass_with_findings` = valid but findings exist (coverage gaps, license
flags, skipped cross-checks); `pass` = clean. Build cross-checks are
marked `skipped` when no build manifests were delivered — absence of
evidence is reported, not papered over. `delta` is **always `null`** in
this standalone preset: the field is reserved for drift-capable runs
(only the Release Security Audit preset computes drift).

### `preloop.cra.vulnscan/v1` (SBOM Exploit Check)

Envelope plus:

```json
{
  "source_sbom": {"path": "sbom/image.spdx.json", "format": "spdx", "spec_version": "SPDX-2.3", "build_ref": null},
  "db_versions": {"osv_queried_at": "…", "kev_snapshot_date": "…", "epss_queried_at": null, "nvd": "not used"},
  "inventory": {"components": 214, "matchable": 189, "unmatchable": 25},
  "findings": [
    {"id": "CVE-…", "pkg": "…", "version": "…", "severity": "high", "cvss": 8.1,
     "epss": 0.42, "kev": true, "fix_version": "…", "vex_status": null}
  ],
  "counts_by_severity": {"critical": 0, "high": 1, "medium": 3, "low": 2, "unknown": 1},
  "art14_candidates": ["CVE-…"],
  "gate": {"policy": "fail on KEV or CVSS >= 9.0 (default)", "passed": false},
  "new_since_last_run": null
}
```

Default gate when the payload provides none: fail on any KEV-listed
finding or CVSS ≥ 9.0. VEX suppressions (OpenVEX or CycloneDX VEX) are
applied to the gate but always echoed in `findings` with their
`vex_status` — auditors ask what was suppressed and why.
`new_since_last_run` is **always `null`** in this standalone preset: the
field is reserved for drift-capable runs (only the Release Security
Audit preset computes drift).

### `preloop.cra.releaseaudit/v1` (Release Security Audit)

Envelope plus `sbom_audit` (the `sbomaudit` body above, minus envelope),
`vuln_scan` (the `vulnscan` body above, minus envelope), and:

```json
{
  "drift": {
    "baseline": {"schema": "preloop.cra.releaseaudit/v1", "run_at": "…", "build_ref": "…"},
    "sbom_changes": {"added": [], "removed": [], "upgraded": [], "license_changes": []},
    "new_vulns": ["CVE-…"],
    "resolved_vulns": [],
    "new_kev": [],
    "gate_transitions": ["gate: pass -> fail"],
    "alert": true
  },
  "verdict": "pass | pass_with_findings | fail"
}
```

`drift` is `null` when no previous `result.json` was delivered. Overall
verdict: `fail` if the SBOM audit failed or the severity gate failed;
`pass_with_findings` if gated-clean but findings or skipped checks exist.
If `sbom_audit.verdict` is `fail`, `result.verdict` must be `fail`.

When a repository is attached, the run also fills `gap_register`
(`null` when no checkout / `repository_url`). Items are
`met | gap | partial | declared`. `not_checkable` is required.
`secrets_findings_count` must equal the SHA+path finding row count — a
gitleaks count of 0 is not "met". A previous run's SHA+path set is a
freeze floor: dropping a row without `resolved` plus a reason fails.
Gap items appear in `checks[]` as `passed: false` and may move a clean
run to `pass_with_findings`; they never flip the severity gate or
`sbom_audit.verdict`.

The gap-register phase carries a full, genericized procedure in the
prompt: pickaxe keyword-family sweeps over git history (`git log -S`
on credential-shaped config names plus `--grep` sweeps), a
forbidden-to-dismiss rule (every pickaxe hit is classified as finding
or not-a-finding with a reason; dismissing hits as "keyword changes
only" is the documented failure mode), one SHA+path row per finding,
junk-at-HEAD checks via `git ls-files`, key-filename citation rules,
and default-credential citations by setting name at `file:line`.
Secret values never appear in any artifact: findings are reported as
commit SHA plus path only, and `git log -p` / `git show` of secret
blobs are forbidden.

### Scanner execution happens in the sandbox

The Release Security Audit preset instructs the agent to install and
run **gitleaks** (recommended pin 8.24.3, git mode, full history,
`--redact`) and **zizmor** (recommended pin 1.16.0, GitHub Actions
workflows) inside the execution sandbox, the same way the SBOM phase
installs `spdx-tools` and `ntia-conformance-checker`. Untrusted-repo
work runs in the governed sandbox, never on the Preloop API server;
the platform keeps only deterministic result validation (the
gap-register freeze comparator in `preloop.security.gap_register`).
Resolved scanner versions are recorded in `tool_versions`, or
`unavailable: reason` when an install fails. A gitleaks finding count
of 0 still does not make secrets hygiene met; that rule lives in
gap-register / `result.json` validation. If the checkout has no
`.github/workflows`, zizmor is recorded as not applicable.

Components with no purl/CPE can be given a generic VCS PURL
(`pkg:generic/<name>@<version>?vcs_url=git+…@<commit>`) when the
repository URL and commit are known, then queried against OSV. This is
a hook, not a full SBOM rewrite.

## Evidence pack layout

```
/workspace/evidence/
  audit-report.md      # human-readable audit (all presets)
  findings.json        # full vulnerability findings (exploit check / release audit)
  sbom-findings.json   # full SBOM verification findings (release audit)
  vuln-report.md       # human-readable vuln report (exploit check)
  drift-report.md      # delta vs previous run (release audit, when baseline given)
  gap-register.md      # file-presence / hygiene register (release audit, when a repo is attached)
  dossier.md           # due-diligence dossier (component due diligence)
  facts.json           # machine-readable due-diligence facts (component due diligence)
```

In product mode the Release Security Audit additionally copies
`result.json` and `evidence/` into the compliance repo and writes
per-repo stubs — see
[Evidence storage architecture](#evidence-storage-architecture-multi-repo-products).

## Evidence storage architecture (multi-repo products)

Authorities and auditors think in **products**, but a product usually
spans several code repositories — firmware, companion app, cloud
backend. Preloop flows already attach any number of git repositories
(`git_clone_config.repositories[]`, each with its own `clone_path`), and
the Release Security Audit preset uses that to store evidence in a
**hybrid** layout:

1. **Per-repo stub** — each audited code repo receives one small
   (< 2 KB), diffable, dated file at
   `.preloop/evidence/<UTC timestamp>-release-security-audit.json`
   (`preloop.cra.repostub/v1`): run date, verdict, gate outcome,
   severity counts, the repo's own HEAD commit SHA, and a pointer to the
   product-level pack. It rides the same PR discipline as code, so the
   evidence trail is tamper-evident with the code history — and it
   deliberately carries **no findings detail**, so code repos never
   accrue artifact bloat.
2. **Product-level compliance repo** — one dedicated repository per
   product receives the full pack under
   `products/<product>/audits/<UTC timestamp>-<release_ref>/`: a copy of
   `result.json`, the whole `evidence/` directory, and a
   `manifest.json` (`preloop.cra.evidencepack/v1`) listing every
   constituent code repo with its remote and **HEAD commit SHA**. The
   manifest SHAs and the stub SHAs must agree — that cross-reference is
   the spine of the audit trail.

Why hybrid, rather than everything in the code repos or everything in a
database:

- **Authorities think product-level.** One repo answers "show me the
  evidence for this product", across firmware/app/cloud, in one place.
- **Access control.** Auditors and legal can be granted the compliance
  repo without any source access.
- **Retention outlives repo churn.** CRA-style retention runs for years
  after release; code repos get renamed, split, and archived. The
  compliance repo persists, and its records reference code repos by
  commit SHA, which survives renames.
- **No artifact bloat in code repos**, while each repo still carries a
  tamper-evident, diffable trace of every audit that covered it.

### Configuring product mode

Clone the Release Security Audit preset into a flow and attach the
product's repositories plus the compliance repo. The flow config names
the compliance repo by the `clone_path: compliance` convention (a
payload field `compliance_repo_path` can override the path per run):

```json
{
  "enabled": true,
  "repositories": [
    {"repository_url": "https://git.example.com/example-product/firmware.git", "clone_path": "firmware"},
    {"repository_url": "https://git.example.com/example-product/companion-app.git", "clone_path": "companion-app"},
    {"repository_url": "https://git.example.com/example-product/product-compliance.git", "clone_path": "compliance"}
  ],
  "create_pull_request": true
}
```

The agent writes the stubs and the pack and **commits locally** on the
branch the platform prepared; pushing and PR/MR creation happen in the
platform's post-execution step, per repository, gated by this flow
config — the agent never runs `git push`. With no repositories attached
the phase is skipped and the preset behaves exactly as before
(artifact-only); `result.json` stays `preloop.cra.releaseaudit/v1` and
gains only an additive, nullable `evidence_storage` section describing
what was written where and whether each commit succeeded.

### The same pattern for SBOM Verify and Exploit Check (spec)

The standalone presets remain artifact-only for now. When they adopt
product mode they will follow the identical pattern, changing only the
flow slug in the stub filename
(`…-sbom-verify.json` / `…-sbom-exploit-check.json`), the `result_schema`
field, and the pack directory (`products/<product>/audits/…` with the
per-preset artifact set). The compliance-repo convention
(`clone_path: compliance`), the stub/manifest schemas
(`preloop.cra.repostub/v1`, `preloop.cra.evidencepack/v1`), the SHA
cross-reference rule, and the commit discipline are shared — one
storage architecture, three producers.

### Honest limits of the storage design

- Tamper evidence comes from git history (and whatever branch
  protection/signing you enforce on the compliance repo) — records are
  not independently signed or timestamped by Preloop.
- The SHA cross-reference proves which code the audit *saw checked
  out*; it does not prove the delivered SBOM was built from those SHAs.
  That link is only as strong as the build metadata your CI delivers.
- Retention is your repo's retention: the design assumes you keep the
  compliance repo for the support period; Preloop does not enforce it.

## Component Due Diligence Record

CRA-style due diligence applies to **every integrated component**,
commercial and open source — and the decisions must be *stored*, not
just made: expect to answer how you decided a component was appropriate,
what documentation you checked, and what was known at the time. This
preset splits the work honestly:

- **Agent legwork (facts, sources cited):** documentation actually
  delivered or fetched; CVE history via OSV.dev with CISA KEV
  cross-check; maintenance signals (release cadence, activity,
  deprecation notices) from cited public sources; **presence** of a
  supplier CE declaration document (never its authenticity —
  `authenticity_verified` is always `false`); declared license; and an
  explicit *open unknowns* list.
- **Human risk decision:** the agent calls the builtin
  `request_approval` tool once with a neutral dossier summary — it
  never recommends an outcome. Approval granted → `accepted`, denied →
  `rejected`, tool unavailable → `pending` (and the run reports
  `error`). Reviewer identity and the decision timestamp live in
  Preloop's approval audit trail; the record references the approval
  and never invents a name.
- **Stored record:** `result.json` (`preloop.cra.duediligence/v1`)
  plus, when the flow attaches a compliance repo (same
  `clone_path: compliance` convention), a dated pair committed under
  `products/<product>/components/<component>/` —
  `<UTC timestamp>-due-diligence.json` and the human-readable dossier
  beside it.

Trigger it manually or by webhook, one component per run:

```json
{
  "component": {"name": "libexample", "version": "1.4.2", "purl": "pkg:generic/libexample@1.4.2", "supplier": "Example Components Ltd"},
  "product": "example-product",
  "usage_context": "TLS transport in the firmware update client",
  "workspace_files": [
    {"path": "docs/security-policy.pdf", "content_base64": "…"},
    {"path": "docs/ce-declaration.pdf", "content_base64": "…"}
  ]
}
```

A `rejected` decision is still a successfully **recorded** decision —
the point is the trail. A granted approval means one reviewer accepted
the component's risk for this product at this time; it is not a
certification, and the record says so.

## Honest limits

- Verification is bounded by delivered build evidence: these flows cannot
  prove an SBOM is complete beyond cross-checks against the manifests you
  provide, and cannot guarantee vulnerability absence.
- No Declaration of Conformity, CE marking decision, "compliant" verdict,
  legal product classification, or Article 14 filing. Evidence in, human
  assessment out.
- `result.json` is persisted by the platform, and the evidence pack is
  captured as a size-capped tar.gz served by
  `GET /api/v1/flows/executions/{id}/evidence`. Long-horizon retention of
  oversized packs and artifact signing are open platform questions — not
  claimed by these presets.
- Validators/scanners are installed at run time, so the toolchain is not
  bit-for-bit fixed across runs. Every run records the exact resolved
  tool versions (`tool_versions`) and source snapshot dates
  (`db_versions`), and the payload can pin versions (e.g.
  `"tools": {"spdx-tools": "0.8.2"}`) which the prompt honors. Shipping
  pinned tools in the runner image is the stronger fix and an open
  platform question. Note that for vulnerability results, database churn
  — not tool versions — is the dominant source of run-to-run variance,
  which is why snapshot dates are always recorded.
