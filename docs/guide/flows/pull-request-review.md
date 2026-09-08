# Pull Request Reviewer preset

Reviews a GitHub pull request or a GitLab merge request: security, quality,
performance, tests, documentation impact, and (since this slice) whether the
PR actually does what the issue it references asked for.

The preset ships as `backend/presets/002-pull-request-reviewer.yaml`
(slug `pull-request-reviewer`).

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

## Not in this slice

- No tracker-side relation read (GitLab's `/merge_requests/:iid/closes_issues`
  endpoint is not wired into the tracker client, so the body and branch are
  the only sources).
- No cross-tracker resolution: an issue in a repository this account does not
  sync reads as `UNCLEAR`.
- No follow-up issue creation. The section is written so its follow-up lines
  can be pasted into a new issue by a human.
