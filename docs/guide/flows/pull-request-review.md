# Pull Request Reviewer preset

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Reviews a GitHub pull request or a GitLab merge request: security, quality,
performance, tests, documentation impact, and (since this slice) whether the
PR actually does what the issue it references asked for.

The preset ships as `backend/presets/002-pull-request-reviewer.yaml`
(slug `pull-request-reviewer`).

Operators set blocking review policy on the flow form's Review instructions
field when the repository cannot hold `.preloop/review-policy.md`.

For an ephemeral checkout, leave `git_clone_config.source_branch` and the
repository's `branch` / `source_branch` overrides unset to follow the triggering
PR or MR. On the flow form these are the **Source branch** field and each
repository's **Branch** field; both are blank by default, and the form shows a
warning when a reviewer flow pins either of them. A pinned value is not a
default: it replaces the trigger's branch on every run. Do not enable `use_remote_default`
for this purpose. GitLab MR hooks
provide the head commit in `object_attributes.last_commit.id` (or `sha`); MR
note hooks provide it under `merge_request`. When that commit is present, the
executor checks it out before creating the review branch, even if the initial
clone used the target or a configured default branch. Without a commit, explicit
branch settings take precedence over the trigger's source branch. Check the
execution's checkout log and `git rev-parse HEAD`, not just the generated review
branch name, when diagnosing a review of the wrong revision.

The review is **stateful**. One summary comment carries HTML markers
(`<!-- preloop-review:flow-id:pr-reviewer -->`,
`<!-- preloop-review:reviewed-sha:SHA -->`) and is rewritten in place on each
push; inline comments are resolved when their finding is fixed; checkboxes a
user ticks are never re-raised.

## Issue coverage

A diff review answers "is this code good". It does not answer "does this do
what was asked", which is the question the author's team actually has. So the
review reads the referenced issue and publishes one **Issue Coverage** section
in the same summary comment.

### 1. Finding the reference

Neither platform hands the reviewer a linked-issue relation on a PR read: the
GitHub mapping in `backend/preloop/sync/trackers/github.py` and the GitLab
mapping in `backend/preloop/api/endpoints/mcp.py` both return a fixed field
list with no `closes_issues`. Preloop therefore parses the reference off the
PR itself, in `backend/preloop/services/issue_references.py`, and exposes it
to the prompt as `{{trigger_event.payload.object_attributes.referenced_issues}}`:

| Kind | Recognized from | Examples |
| --- | --- | --- |
| `closes` | a closing keyword in the PR body or title | `Closes #12`, `Fixes org/repo#12`, `Resolves PROJ-7`, `Implements #12` |
| `reference` | a plain mention or an issue URL in the body | `#12`, `Related to #12`, `https://gitlab.example.com/grp/proj/-/issues/12` |
| `branch` | the branch name | `123-slug`, `fix/issue-123`, `gh-123-slug`, `feature/PROJ-7-slug` |

Cross-repo (`other-org/other#7`) and GitLab subgroup paths
(`grp/sub/proj#7`) resolve; the PR's own number is dropped (squash-merge
titles routinely carry `(#45)`); a same-issue hit from two sources keeps the
strongest kind; the list is capped at 5 entries, strongest first. When
nothing matches, the field reads `none detected` and the review omits the
Issue Coverage section entirely rather than speculating.

The parser is a hint, not the verdict: the prompt tells the reviewer to
confirm it against the PR description it already fetched and to add anything
stated only in prose.

### 2. Reading the issue

The preset's tool allowlist gained exactly one entry, the read-only
`get_issue`:

```yaml
allowed_mcp_tools:
  - name: get_issue          # new: issue coverage
  - name: get_pull_request
  - name: update_pull_request
  - name: add_comment
  - name: update_comment
```

`get_issue` accepts the identifiers the parser produces (an issue URL, an
`org/repo#123` key, or a Jira key) and reads Preloop's synced copy of the
tracker. Budget: at most 2 issues read and at most 3 calls per run.

**Graceful fallback.** The issue may live in a repository the account does
not sync, or sync may not have caught up. `get_issue` then returns "not
found", and the review reports verdict `UNCLEAR` naming that reason. It is
explicitly forbidden from reconstructing the issue's asks out of the PR
description: the author's summary of the issue is the thing under review.
No `create_issue` or `update_issue` is granted, so follow-ups are proposed
for a human to file, never written to the tracker.

### 3. The verdict

Criteria come from the issue's own text (an "Acceptance criteria" or
"Definition of done" section, numbered asks, otherwise the concrete asks in
prose), quoted where possible, at most 8, and **never invented**: tests,
docs, and telemetry are ordinary review findings unless the issue asks for
them. Each criterion is mapped to a hunk (`file.ext:line`) or recorded as
unmet.

| Verdict | Meaning |
| --- | --- |
| `FULL` | every stated criterion is satisfied by this PR |
| `PARTIAL` | some are, at least one is not |
| `NOT ADDRESSED` | none are (normal when a PR merely mentions a related issue) |
| `UNCLEAR` | the issue could not be read, or its asks cannot be mapped to code |

Published shape, inside the one summary comment:

```markdown
### 🎯 Issue Coverage

<!-- preloop-review:issue-coverage -->

**[`org/repo#123`](https://github.com/org/repo/issues/123): Widget picker loses the last selection** (verdict: **PARTIAL**)

Restores the selection but ships no test and no doc update.

Acceptance criteria as this review reads them (quoted from the issue):
- [x] The picker restores the last selection after a reload - `src/widget-picker.ts:88`
- [ ] The restore is covered by a test
- [ ] The behaviour is documented in the widget guide

Gaps:
- No test covers the restore path - `src/widget-picker.test.ts`
- The widget guide still describes the old behaviour

Follow-ups (ready to file as issues):
- **Cover widget picker restore with a test**: the reload path is untested.
- **Document the widget picker restore**: the guide predates it.
```

**Coverage never blocks a merge.** A `PARTIAL` verdict creates no findings
and does not change the review action (approve / comment / request_changes)
decided from finding severity. Authors split work across PRs on purpose and
a later PR may close the issue. The review reports, it does not police.

### 4. Across pushes

The section is stateful like the rest of the review. On the next push the
reviewer finds it by its `<!-- preloop-review:issue-coverage -->` marker,
reuses the recorded criteria verbatim (rewording them would look like the
review changed its mind), re-checks the unchecked ones against the current
code, ticks off the ones later commits satisfy, and drops their gap and
follow-up lines. A criterion a previous review checked is never unchecked
unless the code that satisfied it left the branch, and never silently
dropped. The verdict is recomputed from the checkbox tally, never restated.
In `INCREMENTAL` scope only criteria whose file appears in the delta are
re-checked; the rest keep their checkbox untouched.

`result.json` carries the machine form:

```json
{
  "status": "success",
  "review_posted": true,
  "risk_level": "medium",
  "findings_count": 3,
  "review_action": "comment",
  "issue_coverage": [
    {"issue": "org/repo#123", "verdict": "partial",
     "criteria_total": 3, "criteria_met": 1, "gaps": 2}
  ]
}
```

`issue_coverage` is `[]` when the PR references no issue.

## Stale reviews stop on their own

When a pull request (GitHub, Bitbucket) or merge request (GitLab) is merged
or closed, Preloop stops every execution still bound to it, in any flow of
the account, unless that flow itself triggers on the merge or close. The
execution shows the reason, for example "Stopped because pull request
example/repo#12 was merged". Nothing happens if no run is bound.

With `webhook_config.supersede_on_update: true`, a new head commit also
stops the older run of the same flow on the same pull request before the
new head is reviewed. The preset sets it; flows created from the preset
before this change keep the old behaviour (the new head waits for the older
run) until the flag is set on them. It applies only when the flow triggers
on `pull_request_updated` (`merge_request_updated` on GitLab).

## Repository review policy

Agent instruction files (`AGENTS.md`, `CLAUDE.md`, `.cursorrules`,
`.clinerules`) are project context. The reviewer reads them in full,
including on the fast path. They are not, by themselves, a blocking
compatibility contract: a sentence in `AGENTS.md` is guidance, and
`CONTRIBUTING.md` is only the first 150 lines (and is skipped on the fast
path). To make "this tree must keep running on runtime X" a blocking
finding, commit a policy file or set the flow field below.

### Where to put the rules

| Source | When to use it | Force |
| --- | --- | --- |
| `.preloop/review-policy.md` at the repository root | The repository can carry a file. Read in full on every review, including the fast path. | Blocking. A violation is HIGH, category Compatibility, and the review requests changes. |
| Flow `review_instructions` | The repository cannot commit that file. Same markdown. Injected as `{{flow.review_instructions}}` (16 KiB cap). Set it on the flow in the console (Review instructions) or the API. It is not part of the prompt template, so a later preset update does not wipe it. | Same as the file. |

An empty field and a missing file are normal. The reviewer does not invent
a policy. In clone-less mode the file is visible only when the diff includes
it; the review says so instead of assuming there is no policy.

### File shape

Markdown. The first fenced `yaml` block is the compatibility config. Prose
around it is also blocking when the diff breaks a rule it states. Versions
are quoted strings: an unquoted `5.10` is the number 5.1 in YAML.

```yaml
compatibility:
  - language: perl
    minimum_version: "5.10"
    paths:
      - "daemons/**"
    extensions:
      - ".pl"
      - ".pm"
      - ".t"
    version_linter: "perlver --blame"
    allowed:
      - "say"
      - "state"
      - "defined-or (//)"
    forbidden:
      - "postfix dereference (->@*, ->%*, ->$*)"
      - "subroutine signatures"
      - "__SUB__"
      - "fc"
```

`paths` defaults to every file. `**/` matches zero or more directories,
so `src/**/*.pl` covers `src/x.pl` and `**/*.pl` covers a file at the
repository root. `*` does not cross `/`. `extensions` defaults from the
language. Perl's default is `.pl`, `.pm`, and `.t`. `allowed` is syntax the minimum
already includes, and must not be flagged. `forbidden` is a violation even
when a linter is silent. Other languages use the same keys and name their
own `version_linter`. There is no default command except Perl's.

A `version_linter` value is a program name (a basename, not a path) plus
plain arguments. The reviewer appends each matching path as one argument.
Shell operators and `:` are not run, so a URL cannot be an argument. A
basename that already exists in the sandbox can still run: the command is
taken from the policy already on the target branch, and that author can
already change CI. If the pull request edits the policy file, the reviewer
uses the target branch copy and does not execute a command the pull request
introduced. A policy file the pull request itself adds has no force on that
review. The reviewer says the policy is newly proposed and applies it only
after it merges.

### Version linters

When a compatibility entry matches changed files and names `version_linter`,
the reviewer runs that command and quotes the output. For Perl, omitting
the command means `perlver --blame <file>` (from `Perl::MinimumVersion`).
If that script is missing, the reviewer tries:

```text
perl -MPerl::MinimumVersion -e 'my $pmv = Perl::MinimumVersion->new(shift); print $pmv->minimum_version, "\n"' <file>
```

A reported version newer than `minimum_version` is a HIGH finding. An equal
version is not. Dotted numbers compare numerically: 5.10 is newer than 5.9
and older than 5.16.

The default reviewer sandbox is the digest-pinned image
`ghcr.io/preloop/preloop-codex-universal` (see
`backend/preloop/agents/images.py`). It includes Perl, `cpanm`, `perlver`
(`Perl::MinimumVersion`), `perlcritic`, and `shellcheck`. When a private
runner's image has no `perl` or `Perl::MinimumVersion`, the review says
"version linter unavailable in this sandbox" and judges the diff from the
policy. That is not a pass.

The environment image built from `environments/preloop/Dockerfile` also
ships `perlver`. Private runners that need only the Perl toolchain build
[`environments/perl`](https://github.com/preloop/preloop/tree/main/environments/perl)
and select it with `agent_config.image`. Hosted Codex ignores that key.
Its README lists the build, the offline smoke and the evidence to keep.

### Perl 5.10 example

A tree of daemons that must stay on Perl 5.10 commits the yaml above plus
one line of prose: "Perl under daemons/ must run on Perl 5.10." `say`,
`state`, and defined-or (`//`) are part of 5.10 and are listed under
`allowed`, so a review must not flag them. Postfix dereference (`->@*`),
subroutine signatures, `__SUB__`, and `fc` need a newer Perl. They are
forbidden, and a pull request that adds one is a blocking finding.

The same markdown can be pasted into the flow's Review instructions when
the repository cannot carry `.preloop/review-policy.md`.

## Running tests

Reading the tests is the main check; a run confirms it. Step 2.4 runs
tests only on the PR branch from this repository, never on a fork's code,
and never installs packages to do it. When nothing could run, the summary
says "tests not run in this sandbox" and names the CI jobs to confirm.

In the environment image built from `environments/preloop/Dockerfile`
(see [Execution environments](environments-and-recovery.md#backend-and-frontend-tests-in-the-image)),
the reviewer runs the backend test files the diff touches with
`preloop-pytest -q -m "not integration" <files>`: the backend lock is
preinstalled and the runner starts its own disposable database, with no
network. For frontend test files it runs `preloop-frontend-deps`, then
`cd frontend && npx --no-install web-test-runner <files>` (`--no-install`
so a missing tree fails instead of fetching a package). It never runs the
whole suite (CI shards it) and keeps runs under about 5 minutes. A failing
test on the reviewed head is a finding. The default `codex-universal` image has
neither runner, so there the reviewer only uses a test command whose
dependencies are already installed.

Browser tests default to one session at a time in flow containers. Operators
can set `PRELOOP_TEST_CONCURRENCY` to a positive integer to override it; see
[test concurrency](environments-and-recovery.md#backend-and-frontend-tests-in-the-image)
for configuration and resource guidance.

## Review delivery check

A reviewer run can be required to prove that the code host accepted its
review. Set `git_clone_config.review_publication.required: true` on the flow.
Flows created from the Pull Request Reviewer preset carry this block; an
existing flow without its own block is never gated, whatever its source preset
says. Turn it on for an existing flow once the API build that writes receipts
is serving, and use `required: false` to opt out.

With the check on, the MCP `update_pull_request` tool records a receipt only
after an approve, request-changes or comment review succeeds on the triggering
pull request. At the end of the run the result carries
`result.review_publication` (`published`, `already_reviewed`, `unverified` or
`not_published`). A run that claimed success without a receipt fails with
`review_publication_incomplete` and keeps its report, with one exception for
API and worker builds that differ during a deploy: when the run holds no
receipt at all but the gateway's own record of its tool calls shows a
succeeded `update_pull_request` review on the triggering pull request, the run
passes with `unverified` (reason `receipt_unavailable`) and the worker logs a
warning. `unverified` is not `published`: nothing proves which head was
reviewed. A receipt for another pull request or head still fails the run.
Receipts are recorded under the project's path, also when the agent named a
GitLab project by its numeric id. The agent's own
`review_posted` claim, the success line and a zero exit code never satisfy the
check. GitLab and Bitbucket receipts do not carry the reviewed head, so they
prove delivery for the current run but cannot justify skipping a head that was
already reviewed. Details: [Flows architecture](../../architecture/flows.md#review-publication-receipts).

## Read-only review posted by Preloop

A restricted CI review that runs with `publication_mode: "read_only"` has no
tracker tools, so the delivery check above does not apply to it. Such a flow
can opt into `git_clone_config.platform_review` instead: after the run
finishes and its result passed the flow's result schema, Preloop posts the
review and a commit status on the reviewed commit with a GitHub App token
that never reaches the run. See
[Review and status posted by Preloop](restricted-ci.md#review-and-status-posted-by-preloop).

## Not in this slice

- No tracker-side relation read (GitLab's `/merge_requests/:iid/closes_issues`
  endpoint is not wired into the tracker client, so the body and branch are
  the only sources).
- No cross-tracker resolution: an issue in a repository this account does not
  sync reads as `UNCLEAR`.
- No follow-up issue creation. The section is written so its follow-up lines
  can be pasted into a new issue by a human.
