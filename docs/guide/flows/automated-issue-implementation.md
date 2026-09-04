# Automated Issue Implementation preset

Turns a tracker issue into a working change. The agent reads the issue,
implements it, adds tests, runs the project's checks, and commits to the
checkout it was given. Preloop pushes the branch and opens the pull request
after the run.

The preset ships as `backend/presets/011-automated-issue-implementation.yaml`
(slug `automated-issue-implementation`).

## What the agent does and what the flow does

| Step | Owner |
| --- | --- |
| Decide which issues qualify | flow trigger (`trigger_config`) |
| Read the issue, plan, implement, test, lint | agent |
| Commit to the local checkout | agent |
| Write `/workspace/result.json` | agent |
| Push the branch and open the pull request | flow (`git_clone_config.create_pull_request`) |

The split matters. The agent has no tool that can push, open, or merge a pull
request, so a confused run cannot publish anything. Its toolset is
`get_issue`, `get_pull_request`, `add_comment`, `update_comment`, `ask_user`.
There is no approval tool: gating belongs to your deployment's policies, not
to a preset prompt.

## Choosing which issues qualify

The prompt does not check labels. `trigger_config` does:

```yaml
trigger_event_types:
  - issue_labeled
  - comment_created
trigger_config:
  filter_conditions:
    labels: ["agent-ready"]
```

Every `issue_labeled` event whose labels intersect the list starts a
run. Matching reads label names from the webhook enrichment
(`extract_filter_fields`) and also unwraps GitHub `issue.labels[].name`
and GitLab `labels[].title`, so the example works on raw tracker payloads.
Without a filter, every labeling event qualifies, which is rarely what
you want on a busy repository.

## Resume on pull request comments

`comment_created` is only a trigger when the comment lands on a pull request
this flow opened. The trigger service correlates the comment to the earlier
execution and starts a new run with `{{execution.resume_from}}` set and the
checkout already on the pull request's branch. On that path the agent reads
the pull request with `get_pull_request`, addresses the review comments, and
commits again on the same branch. It never opens a second pull request.

Comments on unrelated issues or pull requests do not start a cold run.

## Unclear issues

The agent asks a human only when a critical decision is missing and guessing
wrong would waste the run. It uses `ask_user`, which surfaces the question in
the console and in notifications. For anything smaller it picks the most
reasonable option and records the choice under `decisions` in
`/workspace/result.json` (and in its optional closing comment on the issue),
so the reviewer sees what was assumed rather than having to infer it.

## Result contract

```json
{
  "status": "success",
  "summary": "...",
  "changes": ["backend/preloop/foo.py: keeps the retry hint"],
  "tests": ["pytest tests/services: 212 passed"],
  "decisions": ["chose the additive migration, no data rewrite"],
  "skipped": ["full backend suite: needs Postgres, not available"],
  "commits": ["550ca71c preserve retry_after_seconds when re-wrapping"]
}
```

`status` is `success` when a change was committed, even if some checks could
not run (those belong under `skipped`), and `failure` with a `reason` when the
issue could not be implemented. Checks that cannot run are reported, never
faked.

## Timeout

The preset sets `timeout_seconds: 5400`. Reading an unfamiliar repository,
writing tests, and running a suite regularly outlives the 3600s default, and a
run killed at the finish line loses the commit.
