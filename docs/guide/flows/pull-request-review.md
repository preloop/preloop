# Pull Request Reviewer preset

Reviews a GitHub pull request or a GitLab merge request: security, quality,
performance, tests, documentation impact, and (since this slice) whether the
PR actually does what the issue it references asked for.

The preset ships as `backend/presets/002-pull-request-reviewer.yaml`
(slug `pull-request-reviewer`).

Operators set blocking review policy on the flow form's Review instructions
field when the repository cannot hold `.preloop/review-policy.md`.

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

The reviewer sandbox is `ghcr.io/openai/codex-universal` (see
`backend/preloop/agents/images.py`). That image is not built from this
repository. `environments/preloop/Dockerfile` is an integration fixture,
not the reviewer image, and it does not install Perl. When `perl` or
`Perl::MinimumVersion` is absent, the review says "version linter
unavailable in this sandbox" and judges the diff from the policy. That is
not a pass.

An image you build can add the smallest useful Perl toolchain.
`Perl::MinimumVersion` is pure Perl (it pulls PPI):

```text
apt-get install -y --no-install-recommends perl cpanminus
cpanm --notest Perl::MinimumVersion
```

### Perl 5.10 example

A tree of daemons that must stay on Perl 5.10 commits the yaml above plus
one line of prose: "Perl under daemons/ must run on Perl 5.10." `say`,
`state`, and defined-or (`//`) are part of 5.10 and are listed under
`allowed`, so a review must not flag them. Postfix dereference (`->@*`),
subroutine signatures, `__SUB__`, and `fc` need a newer Perl. They are
forbidden, and a pull request that adds one is a blocking finding.

The same markdown can be pasted into the flow's Review instructions when
the repository cannot carry `.preloop/review-policy.md`.

## Not in this slice

- No tracker-side relation read (GitLab's `/merge_requests/:iid/closes_issues`
  endpoint is not wired into the tracker client, so the body and branch are
  the only sources).
- No cross-tracker resolution: an issue in a repository this account does not
  sync reads as `UNCLEAR`.
- No follow-up issue creation. The section is written so its follow-up lines
  can be pasted into a new issue by a human.
