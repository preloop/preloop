# Full-repo review presets (architecture-strategy, code health, standards walk)

These flow presets review a **whole repository** rather than a diff. They
complement the diff-scoped Pull Request Reviewer preset: PR review runs
on every change and stays cheap; these run rarely (per release, on a
schedule, or on demand), sample the repository deterministically, and
declare exactly what they covered. Each run is a single execution that
ends by writing `/workspace/result.json` with a versioned schema —
captured as a first-class execution artifact and retrievable via
`GET /api/v1/flows/executions/{execution_id}/result` — plus a
human-readable evidence pack under `/workspace/evidence/`.

| Preset | Lens | result.json schema |
| --- | --- | --- |
| Architecture and Strategy Conformance Review | Declared intent vs observed structure: conformance register over the repo's own architecture/mission/ADR declarations, drift findings (responsibility drift, dependency direction violations, undeclared load-bearing components, dead declared components, technology drift, non-goal violations) | `preloop.review.arch/v1` |
| Full Repo Code Health Review | Correctness risk, quality, performance hotspots, dead code, and test coverage **shape** over a sampled whole-repo pass, with a per-module health register | `preloop.review.codehealth/v1` |
| Standards Compliance Walk | Payload-named standards normalized into a requirement register (`met | gap | partial | declared` plus mandatory `not_checkable`) | `preloop.review.standards/v1` |

They are a **family sharing one skeleton**, not one parameterized preset:
the three lenses have different required inputs, different failure modes
when inputs are missing, different result schemas, and different run
cadences — and the layered preset loader lets an installation override or
disable one lens without touching the others.

**Security posture is not re-reviewed here.** SBOM verification,
vulnerability matching, secrets hygiene, and CI hardening belong to the
[security audit presets](security-audit-presets.md) (referenced, not
duplicated). If a review pass trips over something security-shaped, it
files one referral finding pointing at that family — a `file:line`
pointer only, never a secret value — and moves on. The Standards
Compliance Walk marks security rows of a named standard
`covered_elsewhere: release-security-audit` instead of re-checking them.

## The shared skeleton

All three presets follow the same guarantees:

- **Strictly read-only.** No issue creation, no comments, no commits, no
  pushes, no external mutation. `allowed_mcp_servers` and
  `allowed_mcp_tools` are both empty: a repo walk needs only the checkout
  and the sandbox, and every deliverable leaves through the artifact
  channel.
- **One repository per run.** Checkouts from the flow's git clone config
  live under `/workspace` (`target_repo_path` disambiguates when several
  exist), or the payload supplies `repository_url` for an anonymous
  read-only clone. Every `file:line` pointer refers to the recorded HEAD
  commit SHA.
- **Phased for cost.** A command-only inventory first (`git ls-files`,
  size/extension buckets, churn from `git log` name-only output), then a
  **deterministic sampling plan** (entry points, module boundary files,
  top-by-size and top-by-churn per module, everything under
  `focus_paths`, nothing under `exclude_paths`) — no random sampling, so
  consecutive runs stay comparable.
- **Declared coverage.** `result.json` carries a `coverage` block
  (`files_total`, `files_opened`, `plan_completed`, per-module sampling
  basis (`full_repo_searches` in the standards walk, which does not
  sample), `not_reviewed`). Absence claims are valid only for opened files
  or recorded full-repo searches, and `pass` requires the plan to have
  completed.
- **Budget knobs** (payload, all optional): `depth`
  (`quick | standard | deep` → 60 / 150 / 400 files opened),
  `max_files_opened`, `max_file_kb`, `focus_paths` / `exclude_paths`,
  plus a per-finding verification budget (at most 2 greps and 2 file
  reads) inherited from the Pull Request Reviewer.
- **Freeze-floor drift.** Deliver a previous run's `result.json`
  (`previous_result_path` in the seed, or a hygiene-checked URL) and the
  run classifies everything as new / persisting / resolved. Previous open
  items are a floor: each must reappear re-verified against the current
  checkout or be resolved with a reason and evidence; silently dropping
  one fails the `freeze_floor` check. No baseline delivered → `drift` is
  `null`; a baseline is never guessed.
- **Verdict honesty.** Verdicts (`pass | pass_with_findings | fail`) are
  computed only from open findings, open gap/partial rows, coverage, and
  the freeze floor. **The register can never upgrade a verdict**: `met`
  rows, `declared` rows, resolved items, and positive prose never raise
  it, and `declared` (a stated commitment the files cannot verify) is
  not a pass. `not_checkable` is required, never empty by assumption.
- **Evidence envelope.** Same `checks[]` (deterministic facts) vs
  `assessments[]` (marked judgment) split as the Observe/Eval and
  security presets; every artifact carries the line:

> Machine-generated review evidence. Not a certification, audit opinion,
> or legal advice.

Payload URLs are treated as hostile input, with the same hygiene rule as
the security presets: http(s) only, refusing loopback, private-range,
link-local, and cloud-metadata targets, with refused URLs recorded as
skipped inputs.

## Input contracts per lens

### Architecture and Strategy Conformance Review

Declared intent is attached or discovered — in order of precedence:

1. Payload `intent_docs`: workspace-relative paths (deliverable inline
   via the standard [`workspace_files` seed](../../webhook-triggers.md))
   or hygiene-checked URLs.
2. Repository conventions: `ARCHITECTURE.md`, `docs/architecture*`,
   `README.md` (head), `MISSION.md`, `STRATEGY.md`, `VISION.md`,
   `ROADMAP.md`, accepted ADRs under `docs/adr/` or `docs/decisions/`,
   `CONTRIBUTING.md` (head), and agent instruction files (`AGENTS.md`,
   `CLAUDE.md`).

Every declaration entering the conformance register carries a
`file:line` source pointer — a declaration the agent cannot point to
does not exist. **No intent docs is not a failure**: the run records "no
declared intent" as a gap row, marks conformance rows `not_checkable`,
still reports the observed architecture, and caps the verdict at
`pass_with_findings`. Purpose/fit commentary (does the code serve the
declared mission?) appears only in `assessments`, marked as judgment.

### Full Repo Code Health Review

Needs nothing beyond the checkout. It reads the project's own
conventions first (agent instruction files, README head, lint/formatter
configs) and judges the code by those, not generic taste. Five lenses:
correctness risk, quality, performance hotspots, dead code, and test
**shape** — the test map is derived from file layout (test-to-source
ratios, untested entry points) and is never presented as measured
coverage. Output includes a per-module health register (one row per
lens) and a findings ledger with stable ids
(`health:<lens>:<path>:<slug>`).

### Standards Compliance Walk

The payload must name the standards — the preset **refuses to run
without one** (guessing which standard applies would contaminate the
register):

```json
{
  "standards": [
    {"id": "styleguide", "name": "Example in-house style guide", "source": "docs/styleguide.md"}
  ],
  "depth": "standard",
  "previous_result_path": "previous/result.json"
}
```

`source` is inline text, a seeded workspace path, or a hygiene-checked
URL. Alternatively `"repo_declared": true` walks only the standards the
repository itself declares (lint configs, CONTRIBUTING rules, referenced
style guides, agent instruction files). With neither, the run ends with
an `error` verdict naming the missing input. Standards are normalized
into atomic requirements (`<standard>:R<n>`) with obligation levels
(`mandatory` vs `recommended`) taken from the standard's own wording; a
`gap` is an absence claim and must be backed by a recorded full-repo
search pattern, and requirements a repository cannot evidence
(organizational process, runtime behavior, personnel, hosted
infrastructure) land in `not_checkable`, never faked. Any `mandatory`
gap fails the run.

## Evidence pack layout

```
/workspace/evidence/
  inventory.json               # phase-1 command-only inventory (all three)
  findings.json                # machine-readable findings/register ledger (all three)
  drift-report.md              # only when a baseline was delivered (all three)
  architecture-review.md       # human-readable review (architecture-strategy)
  conformance-register.md      # declaration register (architecture-strategy)
  code-health-report.md        # human-readable review (code health)
  health-register.md           # per-module register (code health)
  standards-report.md          # human-readable walk (standards walk)
  requirements-register.md     # requirement register (standards walk)
```

`result.json` stays under 200 KB; long listings live in the pack and are
referenced from `artifacts`.

## Honest limits

- Coverage is sampled and declared, not total: a clean register row
  means "clean in the opened sample", and the `coverage` block is the
  scope of every claim.
- These are engineering reviews, not conformity assessments: no
  regime profile, no certification, and the standards walk checks only
  what a repository can show.
- Freeze-floor enforcement is reported by the run and owned by
  downstream validation; the agent never self-grades the floor.
