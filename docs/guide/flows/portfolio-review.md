# Portfolio Review preset (many projects, one repository, one fan out)

The [full-repo review presets](repo-review-presets.md) each review **one
project**. This preset sits one layer above them: it takes a repository
full of independently built projects, discovers what is actually in
there, asks a human which projects are worth a review, **starts one child
execution per selected project per lens**, parks itself while they run,
aggregates what they reported, and asks which follow ups to keep.

It answers the question nobody can answer about an inherited estate:
**which of these things is a liability**. The parent does discovery,
delegation and aggregation, and ends by writing `/workspace/result.json`
(`preloop.review.portfolio/v1`) plus an evidence pack under
`/workspace/evidence/` that opens on the family's one-minute verdict
cover.

| | |
| --- | --- |
| Preset file | `backend/presets/017-portfolio-review.yaml` |
| Flow slug | `portfolio-review` |
| Result schema | `preloop.review.portfolio/v1` |
| Lenses it may start | Docs Currency Review, Repo Code Health Review, Release Security Audit, and nothing else |
| Platform tools | `ask_user` (questions), `run_flow` (start a lens), `get_execution` (read a child back), none of them a write tool |
| Project cap | `max_projects`, default 5, hard cap 12 |
| Child cap | `max_children`, default 20, hard cap 25 |

## What it is not

- **Not a reviewer.** The parent never reads project source code and
  never forms an opinion about it. A project's reputation in a run is
  whatever a child lens reported, plus the facts discovery recorded.
- **Not a security review.** Vulnerability matching, SBOM verification,
  secrets hygiene and CI hardening belong to the
  [security audit presets](security-audit-presets.md), which this preset
  may **start as a child** for a project that has an SBOM. Something
  security-shaped noticed outside a child's report gets one referral
  finding with a `file:line` pointer, never a value.
- **Not a modernisation plan.** Nothing is fixed, upgraded, refactored or
  rewritten, and no pull request is opened. The output is a report and a
  ranked list of follow ups a human approved.
- **Not a filer.** The preset has no write tools, so approving a follow
  up records the approval; it does not open anything.
  `rollup.issues_filed` is always `0` and `follow_ups[].filed` is always
  `false`.
- **Not cross-repository.** One repository per run, like every other
  preset in the family.

## Phase 1: discovery is a walk, not an opinion

Discovery is deterministic: commands over paths, manifests and git
history, with no content reads of source files and no model judgment, so
two runs over the same commit produce the same list in the same order.

A project is **a directory containing at least one manifest or build
descriptor from a closed detector list** (`package.json`,
`pyproject.toml`/`setup.py`/`setup.cfg`/`requirements.txt`, `go.mod`,
`Cargo.toml`, `pom.xml`/`build.gradle`/`build.gradle.kts`, `build.sbt`,
`composer.json`, `Gemfile`/`*.gemspec`, `*.csproj`/`*.fsproj`/`*.sln`,
`CMakeLists.txt`, `pubspec.yaml`, `mix.exs`, `Package.swift`,
`deno.json`/`deno.jsonc`) and nothing else is a project. A detector the
agent invents is a bug.

Three rules keep the list honest, applied in this order:

1. **Named exclusion list.** The walk never descends into `.git`,
   `node_modules`, `vendor`, `third_party`, `dist`, `build`, `target`,
   `.venv`, `site-packages`, `.terraform` and the rest of the list, plus
   anything in payload `exclude_paths`. A vendored `package.json` is not
   a project.
2. **Depth cap.** `max_depth` (default 3) levels below `root_path`. Every
   directory the cap stopped the walk from examining is recorded in
   `discovery.dirs_truncated_at_cap`: a truncated branch is a coverage
   statement, not a silent omission.
3. **Nesting rule.** Once a directory is a project, the walk does not
   descend into it. A `ui-kit/package.json` inside a discovered service
   is part of that service, not another project.

Per project the run records `path`, `stacks`, `manifests`, declared
`runtimes` (read only out of named manifest keys, never guessed),
`last_commit_date`, `commits_12m`, `file_count`, the five booleans
`has_readme`, `has_architecture_doc`, `has_ci`, `has_tests_dir`,
`has_licence`, and the SBOM facts `sbom_paths` / `has_sbom`.

SBOMs are found by name, from a closed list (`*.spdx.json`, `*.cdx.json`,
`*.spdx`, `bom.json`, `sbom*.json`, `sbom*.xml`), **project-local only**:
a sibling project's SBOM is not this project's SBOM, and a
repository-root SBOM belongs to no project. Nothing here generates,
reconstructs or infers an SBOM from a manifest or a lockfile; this family
verifies SBOMs and never writes them. That one boolean decides whether
the security lens can run at all (Phase 4).

## Phase 2: the triage hint is a fact, not a score

Each discovered project carries a triage hint so the human reads rows
instead of paths. It is computed **from the discovery facts and nothing
else**:

| rule | points |
| --- | --- |
| `stale_365` / `stale_180` / `stale_90` (exclusive) | +3 / +2 / +1 |
| `no_commits_12m` | +1 |
| `no_readme` | +2 |
| `no_architecture_doc` | +1 |
| `no_ci` | +1 |
| `no_tests_dir` | +1 |
| `no_licence` | +1 |
| `eol_runtime` | +3 |

Bands: `high` at 6 or more, `medium` at 3 to 5, `low` at 2 or less.
Projects are ranked by (score descending, `last_commit_date` ascending,
path ascending), which is reproducible from the recorded facts alone.

Two consequences are deliberate. **A badly written project that is
fresh, documented, tested and supported scores zero**, because nothing in
this phase has read its code; the hint says "nobody has looked at this in
a year", not "this code is bad". And **end of life is never recalled from
memory**: `eol_runtime` fires only against a payload-delivered
`eol_runtimes` table, matching the declared minimum version parsed out of
the manifest (`">=3.8"` is `3.8`). No table, no `eol_runtime` points.

## Two questions, batched, with safe defaults

Both questions go through the built-in `ask_user` channel as **exactly
one batched call** each, with structured rows and an `input_schema` the
human clicks through, never one call per project and never a request to
type JSON into free text.

### First question: which projects to review

One row per discovered project, in rank order, carrying the triage band
as its severity, and a multi-select whose selectable ids are **exactly
the discovered project paths**:

```json
{"type": "object",
 "properties": {
   "selected": {"type": "array", "title": "Projects to review",
     "description": "Leave empty to review nothing.",
     "items": {"enum": ["services/billing-api", "legacy/inventory-web"]}},
   "author": {"type": "string", "title": "Recorded by", "x-autofill": "author"},
   "date": {"type": "string", "format": "date", "x-autofill": "date"}},
 "required": []}
```

**Below the threshold, nothing is asked.** With fewer discovered
projects than `auto_select_threshold` (default 3) every project is
selected and the selection source is `auto_below_threshold`: a human
answering "yes, both of them" is a question that should not have been
asked. An explicit payload `projects` list also replaces the question
(source `payload`).

### Second question: which follow ups to keep

Follow ups are **candidates only**, each one backed by a lens finding and
its pointer, ranked by (project triage rank, lens severity, project
path), at most five per project in `result.json`. The second question
offers them as rows with an optional note per approval. With zero
candidates it is not asked.

### The window, and what happens when it closes

Both calls pass `timeout_seconds: 259200` (3 days), matching the flow's
`approval_window_seconds`. A window that long **parks** the execution:
the run holds no container and no budget, and resumes when the human
decides or the window closes, reading the answer out of the
`_answers_prompt` block of the resumed prompt rather than waiting for a
tool result that will not come.

Expiry, decline, cancellation, an empty answer and a tool routing failure
all fail closed, in the way each question can afford:

| question | safe default |
| --- | --- |
| selection | **inventory only**: no lens runs, every project is `not_run` / `unknown`, no follow up is ranked, the verdict cannot be `pass`, and the report names the deadline that passed |
| follow ups | **keep nothing**: every candidate stays `unapproved`, and the full portfolio report lands exactly as it would have |

Neither question is ever re-asked, and silence is never read as "review
everything".

## Phase 4: the fan out, one child per project per lens

The callable lenses are exactly these, and a payload cannot add to the
list:

| lens slug | result schema | cost ceiling per child |
| --- | --- | --- |
| `docs-currency-review` | `preloop.review.docscurrency/v1` | 2.0 USD |
| `repo-code-health-review` | `preloop.review.codehealth/v1` | 3.0 USD |
| `release-security-audit` | `preloop.cra.releaseaudit/v1` | 3.0 USD |

Payload `lenses` picks a subset (default `["docs-currency-review"]`). **A
lens absent from that list is refused, never silently skipped**: it is
recorded in `fan_out.lenses_refused` with reason `not on the callable
list` and named in the report. The platform enforces the same rule server
side, so a call to a flow this one may not start comes back as a record
with state `TASK_STATE_REJECTED` and refusal reason `flow_not_callable`.
Same answer, recorded the same way.

The plan is deterministic: for each selected project **in rank order**,
for each chosen lens in declared order, one call. A run that hits a
ceiling has therefore done the worthwhile work first, not a random
prefix of it. Each call is:

```
run_flow(flow: "<lens slug>",
         payload: {"target_repo_path": "...", "project_path": "<the path
                   discovery recorded>", "depth": "<unchanged>"},
         label: "<project path>|<lens slug>",
         max_cost_usd: <max_cost_usd_per_child>,
         timeout_seconds: <child_timeout_seconds>)
```

That payload and nothing else: no model or harness overrides, no extra
instructions, no restatement of the lens. **`wait: true` goes on the last
call only** (a wait per call would serialise a fan out), which parks the
parent on `WAITING_FOR_CHILDREN` with no container and no budget while
the children work. The parent resumes **once** for the fan out, with the
full completion records in its trigger payload, and never starts the same
children again.

### The security lens needs an SBOM

It is planned for a project only where discovery recorded one
(`has_sbom` true). Otherwise **no child is started**: the project's
security row is `lens_status: not_checkable` with the reason `no SBOM
available`. The word is `not_checkable`, never "skipped": a skipped check
reads as a choice and this is a missing input. Such a row is never a
pass, never leaves a project healthy, and becomes exactly one follow up
(below) rather than a blank.

### Ceilings are coverage statements, not failures

| ceiling | value |
| --- | --- |
| direct children of one execution | 25 (`FLOW_DELEGATION_MAX_CHILDREN`) |
| per lens, from this flow's callable list | 12 children |
| delegation depth | a child of this run is depth 1, instance cap 2 |
| cost | `max_cost_usd` per child, clamped by the callable entry, inside `FLOW_DELEGATION_MAX_TREE_USD` (50 USD) |
| parked wait | `FLOW_DELEGATION_CHILD_WAIT_SECONDS` (6 hours), then an expired record per unfinished child |

Planned calls past the project cap or the child cap **are not made**:
they are listed in `fan_out.children_over_cap`, every project that got no
lens run lands in `coverage.not_reviewed` with reason `child cap` or
`project cap`, `coverage.plan_completed` goes false, **and the report is
finished anyway**. The run still completes, still writes every artifact,
and says in the cover which projects it never reached. A refusal is an
answer, not an error: it is recorded on the lens row, never retried and
never worked around.

## Phase 5: aggregation from the children's own envelopes

One row per **discovered** project, each carrying one lens row per lens
planned for it. A lens row is what a child reported, never what the
parent thinks of the project: `lens`, `lens_schema`, `lens_status`,
`reason`, `verdict`, `health`, counts, the child (execution id, state,
`cost_usd`, label) and the result artifact path.

| `lens_status` | meaning |
| --- | --- |
| `ran` | the child finished and its result envelope was read |
| `failed` | the child failed, or its envelope is missing, unreadable or the wrong schema |
| `refused` | the call was refused before anything ran, with the platform's reason |
| `expired` | the child had not finished when the 6 hour wait deadline passed |
| `not_checkable` | the lens could not run for want of an input: the security lens with no SBOM |
| `not_run` | no call was planned or made: not selected, lens not chosen, a cap |

Health is derived, never asserted: a `pass` verdict is `healthy`,
`pass_with_findings` is `findings`, `fail` is `failing`, and **anything
that did not run is `unknown`**. Project status follows in order:
`failing` if any lens is failing, else `unknown` if any planned lens row
is not `ran`, else `findings`, else `healthy`.

**A failed child is reported as failed and never as healthy**, and it
takes its project to `unknown` whatever the other lenses said. **A
project no lens reviewed is never counted as healthy**: it is `unknown`,
it appears in `coverage.not_reviewed` with its reason, and it holds the
verdict below `pass`.

Cost is the child's own record: a project's `cost_usd` is the sum of its
children's recorded cost exactly as the completion records report it, a
refused call contributes nothing, and a cost the records do not carry is
`null` rather than a guess. `rollup.children_cost_usd` is the same sum
over every child, and it is what this run **started**, not what the
orchestrator itself spent.

### The missing SBOM is a follow up, not a blank

Every project whose security row is `not_checkable` emits **exactly
one** follow up: id `portfolio:<project path>:add-sbom-generation`, title
`add SBOM generation to this project's build`, lens
`release-security-audit`, severity medium, with a `file:line` pointer at
that project's own build manifest. One per project, never two, and never
for a project whose SBOM was found.

## Verdict

Computed from lens results and coverage only:

- `fail` if any project's health is `failing`.
- `pass` only when every discovered project was reviewed by a lens that
  ran, `plan_completed` is true, `not_reviewed` is empty, and every
  project is `healthy`.
- everything else, including any `unknown` project and any truncated
  coverage, is `pass_with_findings`.

The family rule holds here too: **the register cannot upgrade the
verdict.** Healthy rows, approved follow ups and a flattering
healthy-to-failing ratio never raise it, an inventory-only run is never a
`pass`, and the number of projects discovered says nothing about the
state of the portfolio.

## Evidence pack layout

```
/workspace/evidence/
  portfolio-report.md          # opens with the one-minute verdict cover
  projects-register.md         # one row per discovered project
  findings.json                # every lens finding and follow up candidate
  inventory.json               # the phase-1 discovery facts
  questions.json               # both questions, their items, schemas, deadlines, answers
  children.json                # one row per planned call: the audit trail of the fan out
  projects/<slug>/<lens>/result.json  # each child's own result envelope, verbatim
```

The cover is the same three-box one-pager the rest of the family uses
(What we checked / What we did not check / What you should do next
week). Here the "what we did not check" box is load bearing: it names the
discovered projects no lens ran on and why (not selected, the selection
question expired at its deadline, the project cap, the child cap), every
child that failed, was refused or expired, every project whose security
row is `not_checkable` for want of an SBOM, the directories the walk
excluded or truncated at the depth cap, and the lenses this run did not
start at all.

`result.json` stays under **200 KB**, with every project row under 4 KB
(lens rows and child records included) and every discovery row under
2 KB, so a 25 project portfolio still fits with detail moved into the
evidence pack.

## Payload knobs

| key | default | meaning |
| --- | --- | --- |
| `target_repo_path` / `repository_url` | - | which repository, exactly one per run |
| `root_path` | `.` | where the walk starts |
| `max_depth` | `3` | how deep it descends, with truncation reported |
| `exclude_paths` | - | extra prefixes to skip, added to the named exclusion list |
| `auto_select_threshold` | `3` | below this many projects, nothing is asked |
| `projects` | - | explicit selection, replaces the first question |
| `lenses` | `["docs-currency-review"]` | which callable lenses to run; a name off the list is refused |
| `max_projects` | `5` | selected projects this run fans out for, hard cap 12 |
| `max_children` | `20` | child executions this run starts, hard cap 25 |
| `max_cost_usd_per_child` | `2.0` | cost asked per child; the callable entry lowers it, never raises it |
| `child_timeout_seconds` | `3600` | window asked per child, clamped to this run's remaining time |
| `depth` | `standard` | passed through to each child unchanged |
| `eol_runtimes` | - | the only source for `eol_runtime` triage points |

## Honest limits

- The triage hint ranks **neglect**, not quality: it has not read a line
  of the code it ranks, and it says so.
- Three lenses are callable here. Architecture conformance and the
  standards walk are not, and the cover names them as unchecked.
- The security lens only reports where an SBOM exists; everywhere else
  the row reads `not_checkable` with its reason, and the run says so
  rather than implying the project is clean.
- Caps are real: 12 selected projects and 25 children per run. Beyond
  them the report names the projects it never reached instead of
  pretending to have covered them.
- The parent reports what the children said. A child that failed,
  expired or was refused leaves its project `unknown`, which is an
  absence of evidence and never a clean bill of health.
- Approval is recorded, never executed: nothing in this preset files an
  issue, opens a pull request or edits a file.
