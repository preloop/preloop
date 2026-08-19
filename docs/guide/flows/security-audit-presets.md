# Security audit presets (SBOM verify, exploit check, release audit)

Three flow presets turn a CI release build into audit-grade security
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
| Release Security Audit | Both of the above in one execution, plus drift vs a previous run's result.json | `preloop.cra.releaseaudit/v1` |

All three follow the Observe/Eval pattern: read-only toolset (no MCP
servers or tools), deterministic checks separated from agent judgment
(`checks[]` vs `assessments[]`), and every artifact carries the line:

> Machine-generated evidence for conformity assessment support. Not a
> conformity assessment, certification, or legal advice.

## Wiring a CI build to a preset

1. Clone the preset into a flow and configure a webhook trigger. Your CI
   job calls `POST /webhooks/flows/{flow_id}/{webhook_secret}` after the
   build; the response carries `execution_id` so the pipeline can poll
   `/flows/executions/{execution_id}/result` and gate the release on the
   verdict.
2. Deliver the SBOM either **inline** via the payload's
   [`workspace_files` seed](../../webhook-triggers.md) (base64, 1 MiB
   encoded cap across files) or **by pointer**: put URLs in the payload
   and the agent downloads them at run start.

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
- If the sandbox has no egress to these sources, the run reports an error
  rather than fabricating findings or their absence.

## result.json schemas

### Common evidence envelope (all three schemas)

```json
{
  "schema": "preloop.cra.sbomaudit/v1 | preloop.cra.vulnscan/v1 | preloop.cra.releaseaudit/v1",
  "flow": "sbom-verify | sbom-exploit-check | release-security-audit",
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
evidence is reported, not papered over.

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

## Evidence pack layout

```
/workspace/evidence/
  audit-report.md      # human-readable audit (all presets)
  findings.json        # full vulnerability findings (exploit check / release audit)
  sbom-findings.json   # full SBOM verification findings (release audit)
  vuln-report.md       # human-readable vuln report (exploit check)
  drift-report.md      # delta vs previous run (release audit, when baseline given)
```

## Honest limits

- Verification is bounded by delivered build evidence: these flows cannot
  prove an SBOM is complete beyond cross-checks against the manifests you
  provide, and cannot guarantee vulnerability absence.
- No Declaration of Conformity, CE marking decision, "compliant" verdict,
  legal product classification, or Article 14 filing. Evidence in, human
  assessment out.
- Evidence files live in the execution workspace; `result.json` is the
  artifact persisted by the platform. Long-horizon retention/export of the
  full evidence pack and artifact signing are open platform questions —
  not claimed by these presets.
