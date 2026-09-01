# Security audit presets (CRA evidence packs)

These Apache presets turn a CI-emitted SBOM and optional build evidence
into a versioned `result.json` plus a human-readable evidence pack. They
shipped as `backend/presets/004` through `006` in 0.15.0; `007`
(Component Due Diligence) and the gap register / evidence storage /
waiver fields landed in a later release. Clone a preset into a flow,
attach a webhook (or a schedule), and retain the pack. The YAML prompts
are the contract; this page describes that contract.

## What this is not

This is **not** a conformity assessment, **not** a certification,
**not** legal advice, **not** an SBOM generator, and **not** an Article
14 filing.

The Cyber Resilience Act is Regulation (EU) 2024/2847. Article 14
reporting duties apply from 11 Sep 2026. The full CRA applies from
11 Dec 2027. Preloop does not file CRA Article 14 reports. Preloop does
not issue a Declaration of Conformity, a CE marking decision, or a
"compliant" verdict. These presets also do not constitute an EU AI Act
conformity assessment.

They produce machine-generated evidence for a human assessor. A
manufacturer (or the notified body or assessor that manufacturer
appoints) still has to do the assessment.

Every `result.json` and every markdown evidence file carries this line:

> Machine-generated evidence for conformity assessment support. Not a
> conformity assessment, certification, or legal advice.

## What you get

| Preset | What it does | result.json schema |
| --- | --- | --- |
| SBOM Verify | Format validity, NTIA / CRA Annex I Part II minimum elements, completeness vs delivered build manifests, license flags | `preloop.cra.sbomaudit/v1` |
| SBOM Exploit Check | Components to CVEs via OSV.dev, known-exploited flags via CISA KEV, per-source screening matrix, severity gate | `preloop.cra.vulnscan/v1` |
| Release Security Audit | Both of the above in one execution, plus drift vs a previous run's `result.json`, optional [gap register](#preloopcrareleaseauditv1-release-security-audit) and [multi-repo evidence storage](#evidence-storage-architecture-multi-repo-products) | `preloop.cra.releaseaudit/v1` |
| [Component Due Diligence Record](#component-due-diligence-record) | Agent legwork on one integrated component; a human carries the risk decision via approval; the record can land in a compliance repo | `preloop.cra.duediligence/v1` |

The audit presets follow the Observe / Eval pattern: no write tools
(except `ask_user` on the release audit for optional interactive waivers,
and `request_approval` on due diligence); deterministic checks separated
from agent judgment (`checks[]` vs `assessments[]`); and the disclaimer
above on every artifact.

**One-minute verdict cover.** Human-readable audit and review reports
lead with a one-page cover so nobody has to write a verdict summary by
hand. The Release Security Audit requires this at the top of
`audit-report.md`. The [full-repo review presets](repo-review-presets.md)
require the same cover on their report artifacts. The cover is a verdict
sentence first, then three labelled boxes in this order: What we checked /
What we did not check / What you should do next week. It is strictly one
page. The cover may only summarize findings already present in the body;
the "What we did not check" box is mandatory and may not be empty when
anything was out of scope. The machine `result.json` contract is
unchanged.

Retrieve the structured result with
`GET /api/v1/flows/executions/{execution_id}/result`. Download the
captured evidence tarball with
`GET /api/v1/flows/executions/{id}/evidence`.

## result.json contract

The agent writes `/workspace/result.json` as its final action. The
runner captures that file as a first-class execution artifact. The
`GET .../result` response is:

```json
{
  "execution_id": "<uuid>",
  "status": "SUCCEEDED | FAILED | STOPPED | TIMEOUT | CANCELLED | ...",
  "result": { }
}
```

`result` is the body below. Unknown envelope fields are `null`, never
invented. `checks[]` are deterministic facts. `assessments[]` are agent
judgment. `result.json` stays under 200 KB; large listings live in
`/workspace/evidence/` files referenced from `artifacts`.

`git` is `null` unless a repository was cloned into the workspace. On
the Release Security Audit, fill `git` only when exactly one repository
is checked out; with multiple checkouts set it to `null` (per-repo
identity lives in `evidence_storage.code_repos`).

### Shared evidence envelope

Every schema includes:

```json
{
  "schema": "preloop.cra.sbomaudit/v1 | preloop.cra.vulnscan/v1 | preloop.cra.releaseaudit/v1 | preloop.cra.duediligence/v1",
  "flow": "sbom-verify | sbom-exploit-check | release-security-audit | component-due-diligence",
  "run_at": "ISO 8601 UTC",
  "git": {"remote": "...", "commit": "...", "branch": "...", "dirty": false},
  "tool_versions": {"<tool>": "<version or 'unavailable: reason'>"},
  "inputs_declared": {"<input>": "<what was actually delivered>"},
  "runner": {"kind": "hosted | self_hosted | null", "id": null},
  "regime_profile": "cra",
  "checks": [{"name": "...", "passed": true, "skipped": false, "details": "evidence"}],
  "assessments": [{"topic": "...", "judgment": "agent judgment, marked as such"}],
  "artifacts": {"<name>": "<workspace-relative path>"},
  "disclaimer": "Machine-generated evidence for conformity assessment support. Not a conformity assessment, certification, or legal advice."
}
```

### `preloop.cra.sbomaudit/v1` (SBOM Verify)

Envelope plus:

```json
{
  "source": {
    "format": "spdx | cyclonedx",
    "spec_version": "SPDX-2.3",
    "generator_tool": null,
    "build_ref": null
  },
  "valid": true,
  "minimum_elements": {"passed": false, "missing": ["supplier: 12 components"]},
  "coverage": {
    "components": 214,
    "pct_with_version": 99.1,
    "pct_with_license": 92.5,
    "pct_with_identifier": 88.3,
    "unmatched_vs_build": ["kernel-module-foo"]
  },
  "license_flags": [
    {"component": "...", "license": "...", "flag": "deny | flag | missing"}
  ],
  "delta": null,
  "verdict": "pass | pass_with_findings | fail"
}
```

Verdict: `fail` = invalid SBOM or minimum elements absent (missing inputs
entirely also yields `fail`); `pass_with_findings` = valid but findings
exist (coverage gaps, license flags, skipped cross-checks); `pass` =
clean. Build cross-checks are marked `skipped` when no build manifests
were delivered. `delta` is **always `null`** in this standalone preset;
only the Release Security Audit computes drift.

This schema has no top-level `status` field. Completion is the
`verdict`. Artifacts: `audit_report` (`evidence/audit-report.md`),
`findings` (`evidence/findings.json`).

### `preloop.cra.vulnscan/v1` (SBOM Exploit Check)

Envelope plus:

```json
{
  "status": "success | error",
  "source_sbom": {
    "path": "sbom/image.spdx.json",
    "format": "spdx",
    "spec_version": "SPDX-2.3",
    "build_ref": null
  },
  "db_versions": {
    "osv_queried_at": "...",
    "kev_snapshot_date": "...",
    "epss_queried_at": null,
    "nvd": "not used"
  },
  "inventory": {
    "components": 214,
    "matchable": 189,
    "unmatchable": 25,
    "source_matrix": {
      "osv_purl": {
        "kind": "database",
        "screenable": 0,
        "blind": 0,
        "negative_control": {
          "query": "<verbatim>",
          "result": "<what came back>",
          "method_blind": false
        }
      },
      "osv_git": {
        "kind": "database",
        "screenable": 0,
        "blind": 0,
        "negative_control": {"query": "...", "result": "...", "method_blind": false}
      },
      "nvd_cpe": {
        "kind": "heuristic",
        "screenable": 0,
        "blind": 0,
        "negative_control": {"query": "...", "result": "...", "method_blind": false}
      },
      "osv_distro": {
        "kind": "heuristic",
        "screenable": 0,
        "blind": 0,
        "negative_control": {"query": "...", "result": "...", "method_blind": false}
      },
      "screened_by_no_source": 0
    }
  },
  "findings": [
    {
      "id": "CVE-...",
      "pkg": "...",
      "version": "...",
      "severity": "critical | high | medium | low | unknown",
      "cvss": 8.1,
      "epss": 0.42,
      "kev": true,
      "fix_version": "...",
      "vex_status": null,
      "sources": ["osv_purl | osv_git | nvd_cpe | osv_distro"],
      "match_kind": "database | heuristic",
      "aliases": ["<other ids for the same advisory>"]
    }
  ],
  "counts_by_severity": {
    "critical": 0,
    "high": 1,
    "medium": 3,
    "low": 2,
    "unknown": 1
  },
  "art14_candidates": ["CVE-..."],
  "gate": {"policy": "fail on KEV or CVSS >= 9.0 (default)", "passed": false},
  "new_since_last_run": null
}
```

`status` is the **required** flow completion signal Preloop reads:
`success` when the scan completed and the report was written (regardless
of gate outcome or findings); `error` (plus a `reason` field) when the
scan could not be completed. A failed gate on a completed scan is still
`"status": "success"`.

`matchable` means screenable by at least one **database** source
(`osv_purl` or `osv_git`). Heuristic-only coverage does not make a
component matchable. Heuristic-only findings (`nvd_cpe` / `osv_distro`
with no database-source confirmation) are reported and labeled but do
**not** enter the severity gate.

Default gate when the payload provides none: fail on any KEV-listed
finding or CVSS ≥ 9.0. VEX suppressions (OpenVEX or CycloneDX VEX) are
applied to the gate but always echoed in `findings` with `vex_status`.
`new_since_last_run` is **always `null`** in this standalone preset.

`art14_candidates` lists KEV-listed CVE ids as a prioritisation signal
for a human. It is not a report, and Preloop does not file Article 14
notifications.

If a source's negative control comes back empty, that source is blind
(`method_blind: true`). Empty results from a blind source mean nothing.
A component every source is blind to is "not screenable by any
method", never "zero vulnerabilities".

Artifacts: `findings` (`evidence/findings.json`), `source_matrix`
(`evidence/source-matrix.json`), `report` (`evidence/vuln-report.md`).

### `preloop.cra.releaseaudit/v1` (Release Security Audit)

Envelope plus `sbom_audit` (SBOM body, not a copy of the standalone
envelope), `vuln_scan` (vuln body, not a copy of the standalone
envelope), and the fields below. The nested objects **diverge** from
the standalone presets: extra coverage counters, a per-source matrix,
and waiver fields on the gate.

```json
{
  "sbom_audit": {
    "source": {
      "format": "spdx | cyclonedx",
      "spec_version": "...",
      "generator_tool": null,
      "build_ref": null
    },
    "valid": true,
    "minimum_elements": {"passed": true, "missing": []},
    "coverage": {
      "components": 214,
      "pct_with_version": 99.1,
      "pct_with_license": 92.5,
      "pct_with_license_concluded": 80.0,
      "pct_with_license_declared": 92.5,
      "pct_with_identifier": 88.3,
      "db_resolvable": 40,
      "not_db_resolvable": 174,
      "unmatched_vs_build": null
    },
    "license_flags": [
      {"component": "...", "license": "...", "flag": "deny | flag | missing"}
    ],
    "verdict": "pass | pass_with_findings | fail"
  },
  "vuln_scan": {
    "db_versions": {
      "osv_queried_at": "...",
      "kev_snapshot_date": null,
      "epss_queried_at": null,
      "nvd": "not used"
    },
    "inventory": {
      "components": 214,
      "matchable": 40,
      "unmatchable": 174,
      "db_resolvable": 40,
      "not_db_resolvable": 174,
      "by_ecosystem": {"generic": 174, "pypi": 10},
      "negative_control": {
        "query": "<control query as sent>",
        "result": "<what came back>",
        "method_blind": false
      },
      "source_matrix": {
        "osv_purl": {
          "kind": "database",
          "screenable": 0,
          "blind": 0,
          "negative_control": {
            "query": "<verbatim>",
            "result": "...",
            "method_blind": false
          }
        },
        "osv_git": {
          "kind": "database",
          "screenable": 0,
          "blind": 0,
          "negative_control": {"query": "...", "result": "...", "method_blind": false}
        },
        "nvd_cpe": {
          "kind": "heuristic",
          "screenable": 0,
          "blind": 0,
          "negative_control": {"query": "...", "result": "...", "method_blind": false}
        },
        "osv_distro": {
          "kind": "heuristic",
          "screenable": 0,
          "blind": 0,
          "negative_control": {"query": "...", "result": "...", "method_blind": false}
        },
        "screened_by_no_source": 0
      }
    },
    "findings": [
      {
        "id": "...",
        "pkg": "...",
        "version": "...",
        "severity": "...",
        "cvss": null,
        "epss": null,
        "kev": false,
        "fix_version": null,
        "vex_status": null,
        "sources": ["osv_purl"],
        "match_kind": "database | heuristic",
        "waived": false,
        "aliases": null
      }
    ],
    "counts_by_severity": {
      "critical": 0,
      "high": 0,
      "medium": 0,
      "low": 0,
      "unknown": 0
    },
    "art14_candidates": [],
    "gate": {
      "policy": "<the policy applied>",
      "passed": true,
      "passed_before_waivers": true,
      "waivers_applied": [
        {
          "id": "...",
          "reason": "<verbatim>",
          "author": "...",
          "date": "...",
          "approval_id": null
        }
      ],
      "unwaived_failures": [],
      "waivers_invalid": [],
      "waivers_unmatched": []
    }
  },
  "drift": {
    "baseline": {
      "schema": "preloop.cra.releaseaudit/v1",
      "run_at": "...",
      "build_ref": "..."
    },
    "sbom_changes": {
      "added": [],
      "removed": [],
      "upgraded": [],
      "license_changes": []
    },
    "new_vulns": ["CVE-..."],
    "resolved_vulns": [],
    "new_kev": [],
    "gate_transitions": ["gate: pass -> fail"],
    "alert": true
  },
  "verdict": "pass | pass_with_findings | fail",
  "gap_register": {
    "ran": true,
    "repo": {"remote": "...", "commit": "<40 hex>", "branch": "..."},
    "items": [
      {
        "id": "cvd_policy",
        "title": "...",
        "status": "met | gap | partial | declared",
        "evidence": "<file:line or commit SHA>"
      }
    ],
    "secrets_findings": [
      {
        "sha": "<40 hex>",
        "path": "...",
        "subject": "...",
        "term": "...",
        "kind": "...",
        "status": "finding | not_a_finding",
        "reason": "<required when not_a_finding>"
      }
    ],
    "secrets_findings_count": 0,
    "not_checkable": ["<what the repository could not show>"],
    "resolved": [{"sha": "...", "path": "...", "reason": "..."}],
    "ready": false
  },
  "evidence_storage": {
    "mode": "hybrid",
    "product": "<product name>",
    "compliance_repo": {
      "remote": "...",
      "commit": "<HEAD SHA at clone>",
      "evidence_path": "products/<product>/audits/<run>/",
      "committed": true
    },
    "code_repos": [
      {
        "remote": "...",
        "commit": "<HEAD SHA>",
        "branch": "...",
        "stub_path": ".preloop/evidence/<stub filename>",
        "committed": true
      }
    ]
  }
}
```

`drift` is `null` when no previous `result.json` was delivered.
`gap_register` is `null` when no repository was attached. `evidence_storage`
is `null` when product mode was skipped (no checkouts).

Overall `verdict`: `fail` if the SBOM audit failed **or** the severity
gate failed after deterministic waiver application; `pass_with_findings`
if everything gated passed but findings, waived failures, or skipped
cross-checks exist; `pass` only when clean. If `sbom_audit.verdict` is
`fail`, `result.verdict` must be `fail`. A run with any applied waiver
can never end better than `pass_with_findings`.

Gap-register items appear in `checks[]` as `passed: false` and may
move a clean run to `pass_with_findings`. They never flip the severity
gate or `sbom_audit.verdict`. `not_checkable` is required.
`secrets_findings_count` must equal the SHA+path **finding** row count
(not the gitleaks count). A previous run's SHA+path set is a freeze
floor: dropping a row without `resolved` plus a reason fails platform
validation. `gap_register.ready` is true only when no item is gap or
partial and `secrets_findings_count` is 0. Stable item ids:
`cvd_policy`, `security_contact`, `support_window`,
`article14_runbooks`, `update_and_signed_ota`, `secrets_hygiene`,
`default_credentials_provisioning`, `repo_hygiene`, `key_management`,
`ci_secret_scanning`, `ci_sbom_job`, `debug_leakage`.

Waivers are human-authored inputs (`waivers.json` / `waivers.yaml` in
the seed, or payload `waivers`). The agent never authors a waiver. An
entry missing `id`, `reason`, `author`, or `date` is invalid and waives
nothing. Interactive collection (`waiver_collection: "interactive"`)
uses the built-in `ask_user` channel once, batched; timeout fails closed.

Heuristic sources stay labeled and never enter the severity gate.
`pkg:generic` and `pkg:github` are not db-resolvable by purl; they may
still be screenable on `osv_git` when a `vcs_url` qualifier is present.

When a repository is attached, the gap-register phase installs and
runs **gitleaks** (recommended pin 8.24.3, git mode, full history,
`--redact`) and **zizmor** (recommended pin 1.16.0, GitHub Actions
workflows) inside the execution sandbox. Untrusted-repo work runs in
the governed sandbox, never on the Preloop API server. Secret values
never appear in any artifact: findings are commit SHA plus path only.

Artifacts include `audit_report`, `findings`, `source_matrix`,
`waivers` (or `null`), `sbom_findings`, `gap_register` (or `null`),
`drift_report`.

### `preloop.cra.duediligence/v1` (Component Due Diligence)

Envelope plus:

```json
{
  "component": {
    "name": "...",
    "version": "...",
    "purl": null,
    "supplier": null,
    "homepage": null
  },
  "product": null,
  "usage_context": null,
  "evidence": {
    "docs_examined": [
      {
        "title": "...",
        "source": "<path or URL>",
        "retrieved_at": "<ISO or null>",
        "covers": "..."
      }
    ],
    "cve_history": {
      "osv_queried_at": null,
      "kev_snapshot_date": null,
      "matchable": true,
      "count": 0,
      "kev_ids": [],
      "notable": [{"id": "...", "severity": "...", "note": "..."}]
    },
    "maintenance": {
      "latest_release": null,
      "latest_release_date": null,
      "signals": [
        {"signal": "...", "source": "...", "retrieved_at": "..."}
      ]
    },
    "ce_declaration": {
      "present": false,
      "document": null,
      "issuer": null,
      "date": null,
      "authenticity_verified": false
    },
    "license": {"declared": null, "flags": []},
    "open_unknowns": ["<what could not be determined and why>"]
  },
  "decision": {
    "outcome": "accepted | rejected | pending",
    "decided_via": "preloop_approval",
    "approval_operation": "<exact operation string sent to request_approval>",
    "reviewer": null,
    "note": "Reviewer identity and decision timestamp are in the Preloop approval audit trail for this execution."
  },
  "record": {
    "compliance_repo": null,
    "repo_commit": null,
    "path": null,
    "committed": false
  },
  "status": "success | error",
  "verdict": "recorded | error"
}
```

`status` is the required flow completion signal. `verdict` is
`recorded` only when the legwork completed **and** a human decision
(`accepted` or `rejected`) was captured; otherwise `error`. A
`rejected` decision is still a successfully **recorded** decision. A
granted approval means one reviewer accepted the component's risk for
this product at this time; it is not a certification.

`authenticity_verified` is always `false`: the preset reports presence
of a supplier CE declaration document, never its authenticity.

`reviewer` is always `null` in the record. Reviewer identity lives in
Preloop's approval audit trail.

## CI runbook

One copy-paste path: clone the preset, fire the webhook after the build
with inline `workspace_files`, poll `/result`, gate on the verdict,
retain the evidence tarball.

### 1. Clone the preset and attach a webhook

In the console, clone **Release Security Audit** (or SBOM Verify / SBOM
Exploit Check). Presets ship without a bound model; clone fails closed
with a 422 if the account has no usable model for that agent. Enable a
webhook trigger and copy the webhook URL. The inbound path is:

```
POST /api/v1/webhooks/flows/{flow_id}/{webhook_secret}
Content-Type: application/json
```

The webhook itself is unauthenticated; the secret in the URL is the
credential. The JSON body becomes the trigger payload (see
[webhook triggers](../../webhook-triggers.md)). A 200 response includes
`execution_id` so CI can poll. `execution_url` is the console page, not
the API.

`GET /api/v1/flows/executions/{execution_id}` and
`GET /api/v1/flows/executions/{execution_id}/result` require an account
API token (`Authorization: Bearer`, same `PRELOOP_TOKEN` as
[trigger a flow from CI](ci-trigger.md)). The token needs `view_flows`.

### 2. Deliver the SBOM inline

Prefer **inline** [`workspace_files`](../../webhook-triggers.md)
(base64, 1 MiB encoded cap across files, 50 files). Payload field
names are conventions the prompt understands; the agent also searches
`/workspace` for SBOM-shaped files.

URL delivery is allowed (`sbom.urls` and similar) but is hostile input:
anyone holding the webhook secret can inject them, and the exec
sandbox needs egress to the vulnerability sources, so the runner does
**not** guarantee egress filtering. The prompts instruct the agent to
fetch only http(s) URLs and to refuse targets resolving to loopback,
private-range, link-local, or cloud metadata addresses (e.g.
`169.254.169.254`), but this is prompt-level hardening, not a network
policy. Prefer inline `workspace_files` where integrity matters; a
platform-level egress allowlist is an open question.

Example payload (Release Security Audit):

```json
{
  "release_ref": "v1.2.3",
  "product": "example-product",
  "build": {
    "image_name": "example-image",
    "image_hash": "sha256:...",
    "toolchain": "yocto-5.0"
  },
  "sbom": {"paths": ["sbom/image.spdx.json"]},
  "manifests": {"license_manifest_path": "manifests/license.manifest"},
  "license_policy_path": "policy/licenses.yaml",
  "gate": {"fail_on_kev": true, "fail_on_cvss_gte": 7.0},
  "previous_result_path": "previous/result.json",
  "workspace_files": [
    {"path": "sbom/image.spdx.json", "content_base64": "..."},
    {"path": "manifests/license.manifest", "content_base64": "..."},
    {"path": "previous/result.json", "content_base64": "..."}
  ]
}
```

Recommended CI job outputs to retain and deliver: the SPDX / CycloneDX
file(s), the license manifest (e.g. Yocto `license.manifest`), image or
package manifests for cross-checks, and the previous audit's
`result.json` if you want drift. Optional: VEX file, license policy,
human-authored `waivers.json`.

If the encoded `workspace_files` would exceed 1 MiB, do not silently
truncate. Fail the job or switch to URL delivery with the trust warning
above in mind.

### 3. GitHub Actions (curl)

Store `PRELOOP_URL` (API origin, no trailing slash), the full webhook URL
(including the secret) as `PRELOOP_CRA_WEBHOOK_URL`, and
`PRELOOP_TOKEN` (account API token with `view_flows`) as repository
secrets. This job assumes a prior step wrote `sbom/image.spdx.json`.

```yaml
# .github/workflows/cra-evidence.yml
name: CRA evidence pack
on:
  push:
    tags: ["v*"]
jobs:
  evidence:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # Replace with your real SBOM-producing build, or download it
      # from the build job's artifacts.
      - name: Trigger Release Security Audit
        env:
          PRELOOP_URL: ${{ secrets.PRELOOP_URL }}
          PRELOOP_CRA_WEBHOOK_URL: ${{ secrets.PRELOOP_CRA_WEBHOOK_URL }}
          PRELOOP_TOKEN: ${{ secrets.PRELOOP_TOKEN }}
        run: |
          set -euo pipefail
          test -f sbom/image.spdx.json
          python3 - <<'PY' > /tmp/payload.json
          import base64, json, os, pathlib, sys
          files = []
          for rel in (
              "sbom/image.spdx.json",
              "manifests/license.manifest",
              "previous/result.json",
          ):
              p = pathlib.Path(rel)
              if p.is_file():
                  files.append({
                      "path": rel,
                      "content_base64": base64.b64encode(p.read_bytes()).decode(),
                  })
          encoded = sum(len(f["content_base64"]) for f in files)
          if encoded > 1024 * 1024:
              raise SystemExit(
                  f"workspace_files encoded size {encoded} exceeds 1 MiB cap"
              )
          json.dump({
              "release_ref": os.environ.get("GITHUB_REF_NAME", ""),
              "sbom": {"paths": ["sbom/image.spdx.json"]},
              "workspace_files": files,
          }, sys.stdout)
          PY

          RESP=$(curl -fsS -X POST "$PRELOOP_CRA_WEBHOOK_URL" \
            -H "Content-Type: application/json" \
            --data-binary @/tmp/payload.json)
          EXEC_ID=$(printf '%s' "$RESP" | jq -r '.execution_id')
          test -n "$EXEC_ID" && test "$EXEC_ID" != "null"
          echo "Triggered $EXEC_ID"

          for _ in $(seq 1 180); do
            HTTP=$(curl -sS -o /tmp/exec.json -w '%{http_code}' \
              -H "Authorization: Bearer $PRELOOP_TOKEN" \
              "$PRELOOP_URL/api/v1/flows/executions/$EXEC_ID")
            STATUS=$(jq -r '.status // empty' /tmp/exec.json)
            case "$STATUS" in
              SUCCEEDED|FAILED|STOPPED|TIMEOUT|CANCELLED) break ;;
            esac
            sleep 10
          done
          echo "execution status=$STATUS http=$HTTP"
          case "$STATUS" in
            SUCCEEDED) ;;
            *) echo "execution did not succeed: $STATUS"; exit 1 ;;
          esac

          curl -fsS -H "Authorization: Bearer $PRELOOP_TOKEN" \
            "$PRELOOP_URL/api/v1/flows/executions/$EXEC_ID/result" \
            > result-wrap.json
          jq '.result' result-wrap.json > result.json
          VERDICT=$(jq -r '.result.verdict // empty' result-wrap.json)
          echo "verdict=$VERDICT"

          curl -fsS -H "Authorization: Bearer $PRELOOP_TOKEN" \
            "$PRELOOP_URL/api/v1/flows/executions/$EXEC_ID/evidence" \
            -o "evidence-${EXEC_ID}.tar.gz" || echo "no evidence archive"

          mkdir -p artifacts
          cp result.json "artifacts/result.json"
          if [ -f "evidence-${EXEC_ID}.tar.gz" ]; then
            cp "evidence-${EXEC_ID}.tar.gz" artifacts/
          fi

          if [ -z "$VERDICT" ]; then
            echo "missing result.verdict"
            exit 1
          fi
          if [ "$VERDICT" = "fail" ]; then
            echo "audit verdict is fail"
            exit 1
          fi
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: cra-evidence
          path: |
            result.json
            artifacts/
```

Generic curl (same contract, no Actions):

```sh
# 1. POST the webhook. Response carries execution_id.
RESP=$(curl -fsS -X POST "$PRELOOP_CRA_WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  --data-binary @payload.json)
EXEC_ID=$(printf '%s' "$RESP" | jq -r .execution_id)

# 2. Poll until terminal, then GET /result.
curl -fsS -H "Authorization: Bearer $PRELOOP_TOKEN" \
  "$PRELOOP_URL/api/v1/flows/executions/$EXEC_ID/result" \
  | jq '.result.verdict'

# 3. Retain the evidence tarball.
curl -fsS -H "Authorization: Bearer $PRELOOP_TOKEN" \
  "$PRELOOP_URL/api/v1/flows/executions/$EXEC_ID/evidence" \
  -o "evidence-${EXEC_ID}.tar.gz"
```

Gating notes:

- Execution status `SUCCEEDED` means the flow completed. A `fail`
  verdict is still a completed audit; CI must read `result.verdict`
  (Release Security Audit / SBOM Verify) or `result.gate.passed` plus
  `result.status` (SBOM Exploit Check). Do not treat a green execution
  as a clean pack.
- This example fails the job on `verdict: fail` and on a non-SUCCEEDED
  execution. `pass_with_findings` is a completed audit with findings;
  tighten the gate if your release policy requires a clean pack.
- For SBOM Exploit Check, gate on `result.status == "success"` and
  `result.gate.passed`. Do not treat `"status": "success"` as a passed
  severity gate.
- Retain `result.json` and the evidence tarball even when the gate
  fails. The pack is the record.

### Scheduled re-audits

CRA vulnerability handling continues through the support period even
when you ship nothing. Attach a schedule trigger (cron / interval) to a
cloned **Release Security Audit** or **SBOM Exploit Check** flow that
re-delivers the *last released* SBOM (plus the previous `result.json`
for drift). Each run produces a dated evidence pack; the run-over-run
trail is the auditable record.

## Honest limits

- Verification is bounded by delivered build evidence: these flows
  cannot prove an SBOM is complete beyond cross-checks against the
  manifests you provide, and cannot guarantee vulnerability absence.
- No Declaration of Conformity, CE marking decision, "compliant"
  verdict, legal product classification, or Article 14 filing. Evidence
  in, human assessment out. Preloop does not file CRA Article 14
  reports.
- `result.json` is persisted by the platform, and the evidence pack is
  captured as a size-capped tar.gz served by
  `GET /api/v1/flows/executions/{id}/evidence`. Long-horizon retention
  of oversized packs and artifact signing are open platform questions,
  not claimed by these presets.
- Validators / scanners are installed at run time, so the toolchain is
  not bit-for-bit fixed across runs. Every run records the exact resolved
  tool versions (`tool_versions`) and source snapshot dates
  (`db_versions`), and the payload can pin versions (e.g.
  `"tools": {"spdx-tools": "0.8.2"}`) which the prompt honors. Shipping
  pinned tools in the runner image is the stronger fix and an open
  platform question. For vulnerability results, database churn, not tool
  versions, is the dominant source of run-to-run variance, which is why
  snapshot dates are always recorded.
- These presets verify SBOMs. They never generate one. SBOM creation
  belongs to the build toolchain (Yocto / OpenEmbedded `create-spdx`,
  AOSP SBOM tooling, CycloneDX build plugins). If no SBOM was
  delivered, the run errors rather than inventing one.

## Vulnerability sources (honest notes)

- **OSV.dev** is the primary source: `POST https://api.osv.dev/v1/querybatch`
  with purl or name+ecosystem+version, no API key required. Results record
  `osv_queried_at`. Query form matters: one component per query object;
  strip purl qualifiers; Maven is `group:artifact` with a colon. A
  malformed query returns an empty set that looks like a clean
  component, which is why each source has a negative control.
- **CISA KEV** catalog supplies known-exploited flags; the catalog
  release date is recorded as `kev_snapshot_date`. If cisa.gov returns
  403, the release-audit prompt falls back to the official
  `cisagov/kev-data` GitHub mirror and records the URL actually used. A
  failed primary fetch is not "zero KEV hits". KEV hits appear in
  `art14_candidates` as a prioritisation signal for a human, not legal
  advice, and Preloop does not file Article 14 reports.
- **EPSS** scores are best-effort (`api.first.org`); `null` when
  unreachable.
- **NVD** is a fallback only: the public API without a key is
  rate-limited to roughly 5 requests per 30 seconds, so runs do not
  enumerate NVD for a whole SBOM. How (and whether) NVD was used is
  recorded in `db_versions.nvd`. NVD CPE name+version search is a
  **labeled heuristic**, never presented as a database match.
- Components without a version or a purl / CPE identifier cannot be
  reliably matched; they are counted as `unmatchable` and results
  never claim vulnerability absence for them.
- The Release Security Audit additionally reports `db_resolvable`
  coverage (components in ecosystems the advisory databases actually
  index; `pkg:generic` and `pkg:github` are not db-resolvable by purl)
  and runs a mandatory negative control: a known-vulnerable component
  queried in the same style as the inventory. If the control comes back
  empty the method is blind for that class (`method_blind: true`) and
  those components are reported as "not screenable by this method",
  never as "zero vulnerabilities".
- If the sandbox has no egress to these sources, the run reports an
  error rather than fabricating findings or their absence.

## Evidence pack layout

```
/workspace/evidence/
  audit-report.md      # human-readable audit (all presets); Release Security Audit opens with the one-minute cover
  findings.json        # full vulnerability findings (exploit check / release audit)
  source-matrix.json   # component x source screening matrix (exploit check / release audit)
  waivers.json          # waiver entries seen (release audit, when any existed)
  sbom-findings.json   # full SBOM verification findings (release audit)
  vuln-report.md       # human-readable vuln report (exploit check)
  drift-report.md      # delta vs previous run (release audit, when baseline given)
  gap-register.md      # file-presence / hygiene register (release audit, when a repo is attached)
  dossier.md           # due-diligence dossier (component due diligence)
  facts.json           # machine-readable due-diligence facts (component due diligence)
```

In product mode the Release Security Audit additionally copies
`result.json` and `evidence/` into the compliance repo and writes
per-repo stubs. See
[Evidence storage architecture](#evidence-storage-architecture-multi-repo-products).

## Evidence storage architecture (multi-repo products)

Authorities and auditors think in **products**, but a product usually
spans several code repositories (firmware, companion app, cloud
backend). Preloop flows already attach any number of git repositories
(`git_clone_config.repositories[]`, each with its own `clone_path`), and
the Release Security Audit preset uses that to store evidence in a
**hybrid** layout:

1. **Per-repo stub.** Each audited code repo receives one small
   (< 2 KB), diffable, dated file at
   `.preloop/evidence/<UTC timestamp>-release-security-audit.json`
   (`preloop.cra.repostub/v1`): run date, verdict, gate outcome,
   severity counts, the repo's own HEAD commit SHA, and a pointer to the
   product-level pack. It rides the same PR discipline as code, so the
   evidence trail is tamper-evident with the code history, and it
   deliberately carries **no findings detail**, so code repos never
   accrue artifact bloat.
2. **Product-level compliance repo.** One dedicated repository per
   product receives the full pack under
   `products/<product>/audits/<UTC timestamp>-<release_ref>/`: a copy of
   `result.json`, the whole `evidence/` directory, and a
   `manifest.json` (`preloop.cra.evidencepack/v1`) listing every
   constituent code repo with its remote and **HEAD commit SHA**. The
   manifest SHAs and the stub SHAs must agree. That cross-reference is
   the spine of the audit trail.

Why hybrid, rather than everything in the code repos or everything in a
database:

- **Authorities think product-level.** One repo answers "show me the
  evidence for this product", across firmware / app / cloud, in one
  place.
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
    {
      "repository_url": "https://git.example.com/example-product/firmware.git",
      "clone_path": "firmware"
    },
    {
      "repository_url": "https://git.example.com/example-product/companion-app.git",
      "clone_path": "companion-app"
    },
    {
      "repository_url": "https://git.example.com/example-product/product-compliance.git",
      "clone_path": "compliance"
    }
  ],
  "create_pull_request": true
}
```

The agent writes the stubs and the pack and **commits locally** on the
branch the platform prepared; pushing and PR / MR creation happen in the
platform's post-execution step, per repository, gated by this flow
config. The agent never runs `git push`. With no repositories attached
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
(`clone_path: compliance`), the stub / manifest schemas
(`preloop.cra.repostub/v1`, `preloop.cra.evidencepack/v1`), the SHA
cross-reference rule, and the commit discipline are shared: one storage
architecture, three producers.

### Honest limits of the storage design

- Tamper evidence comes from git history (and whatever branch
  protection / signing you enforce on the compliance repo). Records are
  not independently signed or timestamped by Preloop.
- The SHA cross-reference proves which code the audit *saw checked
  out*; it does not prove the delivered SBOM was built from those SHAs.
  That link is only as strong as the build metadata your CI delivers.
- Retention is your repo's retention: the design assumes you keep the
  compliance repo for the support period; Preloop does not enforce it.

## Component Due Diligence Record

CRA-style due diligence applies to **every integrated component**,
commercial and open source, and the decisions must be *stored*, not
just made: expect to answer how you decided a component was appropriate,
what documentation you checked, and what was known at the time. This
preset splits the work honestly:

- **Agent legwork (facts, sources cited):** documentation actually
  delivered or fetched; CVE history via OSV.dev with CISA KEV
  cross-check; maintenance signals (release cadence, activity,
  deprecation notices) from cited public sources; **presence** of a
  supplier CE declaration document (never its authenticity:
  `authenticity_verified` is always `false`); declared license; and an
  explicit *open unknowns* list.
- **Human risk decision:** the agent calls the builtin
  `request_approval` tool once with a neutral dossier summary. It never
  recommends an outcome. Approval granted → `accepted`, denied →
  `rejected`, tool unavailable → `pending` (and the run reports
  `error`). Reviewer identity and the decision timestamp live in
  Preloop's approval audit trail; the record references the approval
  and never invents a name.
- **Stored record:** `result.json` (`preloop.cra.duediligence/v1`)
  plus, when the flow attaches a compliance repo (same
  `clone_path: compliance` convention), a dated pair committed under
  `products/<product>/components/<component>/`:
  `<UTC timestamp>-due-diligence.json` and the human-readable dossier
  beside it.

Trigger it manually or by webhook, one component per run:

```json
{
  "component": {
    "name": "libexample",
    "version": "1.4.2",
    "purl": "pkg:generic/libexample@1.4.2",
    "supplier": "Example Components Ltd"
  },
  "product": "example-product",
  "usage_context": "TLS transport in the firmware update client",
  "workspace_files": [
    {"path": "docs/security-policy.pdf", "content_base64": "..."},
    {"path": "docs/ce-declaration.pdf", "content_base64": "..."}
  ]
}
```
