# Portfolio Review preset (many projects, one repository, one execution)

The [full-repo review presets](repo-review-presets.md) each review **one
project**. This preset sits one layer above them: it takes a repository
full of independently built projects, discovers what is actually in
there, asks a human which projects are worth a review, runs the [Docs
Currency Review](repo-review-presets.md#docs-currency-review) lens inline
for the selected ones, aggregates the results, and asks which follow ups
to keep.

It answers the question nobody can answer about an inherited estate:
**which of these things is a liability**. Everything runs inline in one
execution and ends by writing `/workspace/result.json`
(`preloop.review.portfolio/v1`) plus an evidence pack under
`/workspace/evidence/` that opens on the family's one-minute verdict
cover.

| | |
| --- | --- |
| Preset file | `backend/presets/017-portfolio-review.yaml` |
| Flow slug | `portfolio-review` |
| Result schema | `preloop.review.portfolio/v1` |
| Lens it runs | Docs Currency Review (`preloop.review.docscurrency/v1`), inline, unchanged |
| Write tools | none, apart from the built-in `ask_user` question channel |
| Report publication | platform step after the agent exits: `PORTFOLIO.md` on `preloop/report/portfolio`, as a pull request |
| Inline project cap | `max_inline_projects`, default 5, hard cap 8 |

## What it is not

- **Not a delegator.** There are no child executions and no fan out: one
  execution does discovery, the lens runs and the aggregation. Above the
  inline cap the report says plainly that delegation is required and the
  remaining projects land in `coverage.not_reviewed` with reason
  `inline cap`, rather than being reviewed badly.
- **Not a security review.** SBOM, vulnerability matching, secrets
  hygiene and CI hardening stay with the
  [security audit presets](security-audit-presets.md); something
  security-shaped gets one referral finding with a `file:line` pointer,
  never a value.
- **Not a modernisation plan.** Nothing is fixed, upgraded, refactored or
  rewritten. The output is a report and a ranked list of follow ups a
  human approved. The one pull request a run can produce contains that
  report and nothing else, and the agent does not open it (see
  [Where the report lands](#where-the-report-lands)).
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
`last_commit_date`, `commits_12m`, `file_count`, and the five booleans
`has_readme`, `has_architecture_doc`, `has_ci`, `has_tests_dir`,
`has_licence`.

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

## Phases 4 and 5: the lens runs unchanged, health is derived

For each selected project in rank order the run executes the Docs
Currency Review lens **exactly as `016-docs-currency-review.yaml`
defines it**, with `project_path` set to that project and `depth` passed
through. The lens is reused, not restated: its five claim types, its
recorded searches, its prose-quality ban and its verdict rules are the
definition. Each per-project result lands at
`evidence/projects/<slug>/result.json`.

Aggregation keeps one row per **discovered** project, not per reviewed
project, and health is derived from the lens verdict:

| lens | health |
| --- | --- |
| ran, `pass` | `healthy` |
| ran, `pass_with_findings` | `findings` |
| ran, `fail` | `failing` |
| did not run | `unknown` |

**A project whose lens did not run is never counted as healthy.** It is
`unknown`, it appears in `coverage.not_reviewed` with its reason, and it
holds the portfolio verdict below `pass`.

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
  projects/<slug>/result.json  # the per-project lens result
```

The cover is the same three-box one-pager the rest of the family uses
(What we checked / What we did not check / What you should do next
week). Here the "what we did not check" box is load bearing: it names the
discovered projects no lens ran on and why (not selected, the selection
question expired at its deadline, the inline cap, a lens that could not
run), the directories the walk excluded or truncated at the depth cap,
and the lenses this preset does not run.

`result.json` stays under **200 KB**, with every project row under 4 KB
and every discovery row under 2 KB, so a 25 project portfolio still fits
with detail moved into the evidence pack.

## Where the report lands

A report nobody opens is a report nobody reads. So the run does not stop
at the evidence pack: `evidence/portfolio-report.md` is also offered to
the repository as a pull request, where a portfolio owner reviews it the
way they review everything else, and merges it (or does not).

The agent has nothing to do with that. It has no write tools, no git
credentials in its tool surface and no provider it can call. Publication
is a **platform step that runs after the agent process has exited**,
using the flow's existing git clone and pull request configuration. The
agent's whole contribution is the file on disk: whatever
`evidence/portfolio-report.md` contains when the agent finishes is what
gets published.

```yaml
git_clone_config:
  create_pull_request: true          # required; a direct commit is never attempted
  report_publication:
    enabled: true
    source_path: evidence/portfolio-report.md   # workspace relative
    destination_path: PORTFOLIO.md              # repository relative
    commit_message: Update the portfolio review report
    # branch: reports/portfolio                 # optional override
```

### The branch naming rule

The branch is derived from the destination document, never from the
execution: `preloop/report/` followed by the destination path lowercased
with its extension dropped and every run of non-alphanumeric characters
turned into a single `-`.

| `destination_path` | branch |
| --- | --- |
| `PORTFOLIO.md` | `preloop/report/portfolio` |
| `docs/reviews/portfolio.md` | `preloop/report/docs-reviews-portfolio` |

Because the name has nothing run-specific in it, every run of the same
flow pushes to the same branch, and the open pull request tracking that
branch **updates in place**. You get one pull request per document that
keeps being refreshed, not one per run. Two documents in one repository
get two branches and therefore two independent pull requests. Set
`report_publication.branch` if your repository has its own convention;
the rule above then does not apply, but the stability requirement still
does: a branch that changes between runs would open a second pull
request.

### What it will not do

- **It will not commit to your default branch.** The default branch is
  only ever read, as the start point of the report branch on the first
  run, and used as the pull request base. A protected default branch is
  the expected case, not an obstacle: there is no direct commit for it
  to refuse.
- **It will not carry anything but the report.** The commit is built in
  a throwaway worktree and staged with a single pathspec, so it contains
  exactly one changed file. A checkout the run left dirty (it read many
  projects it does not trust) cannot contribute a byte.
- **It will not republish an unchanged report.** If the regenerated
  document is byte identical to the one on the branch, nothing is
  committed, nothing is pushed and no provider call is made. The run
  records `outcome: unchanged`, `reason: identical_document`, and the
  existing pull request is left exactly as it was.

### When publication fails

Publication cannot fail a run. The report is the deliverable, and it is
already in the evidence pack before publication is attempted; a
repository that refuses the push does not retroactively spoil a review
that happened. The execution stays successful, the artifact is intact,
and the run result carries the reason under `report_publication`:

```json
{ "outcome": "failed", "reason": "push_failed",
  "branch": "preloop/report/portfolio", "document": "PORTFOLIO.md",
  "log": "evidence/report-publication.log" }
```

`outcome` is one of `published`, `unchanged` or `failed`. `reason` comes
from a closed list, so it is a diagnosis rather than a provider error
string: `identical_document`, `report_missing`, `checkout_unavailable`,
`base_branch_unavailable`, `worktree_failed`, `copy_failed`,
`stage_failed`, `commit_failed`, `push_failed`,
`pull_request_unavailable`, `pull_request_disabled`,
`provider_unsupported`, `repository_missing`, `repository_ambiguous`,
`invalid_configuration`. The git and provider output behind it is kept in
`evidence/report-publication.log`. This field is written by the platform
from the container's own output; an agent's `result.json` cannot author
it, which is what makes it evidence rather than a claim.

Because the branch is stable, failures heal by themselves: the next run
retries on the same branch, and a run whose push landed but whose pull
request call did not (`pull_request_unavailable`) opens the pull request
on its next attempt.

## Payload knobs

| key | default | meaning |
| --- | --- | --- |
| `target_repo_path` / `repository_url` | - | which repository, exactly one per run |
| `root_path` | `.` | where the walk starts |
| `max_depth` | `3` | how deep it descends, with truncation reported |
| `exclude_paths` | - | extra prefixes to skip, added to the named exclusion list |
| `auto_select_threshold` | `3` | below this many projects, nothing is asked |
| `projects` | - | explicit selection, replaces the first question |
| `max_inline_projects` | `5` | inline reviews this run, hard cap 8 |
| `depth` | `standard` | passed through to each lens run |
| `eol_runtimes` | - | the only source for `eol_runtime` triage points |

## Honest limits

- The triage hint ranks **neglect**, not quality: it has not read a line
  of the code it ranks, and it says so.
- One lens runs here. Code health, architecture conformance, the
  standards walk and the whole security family are not part of a
  portfolio run, and the cover names them as unchecked.
- An inline run cannot honestly review more than 8 projects; beyond that
  the report asks for delegation instead of pretending.
- Approval is recorded, never executed: nothing in this preset files an
  issue or edits a project. The agent opens no pull request either; the
  single pull request a run can produce is opened by the platform after
  the agent exits, contains only the report, and changes nothing about
  the projects it describes.
- Publication is best effort by design. It is not a delivery guarantee:
  a run can be a complete, successful review whose report never reached
  the repository, and the reason for that is recorded rather than
  raised.
