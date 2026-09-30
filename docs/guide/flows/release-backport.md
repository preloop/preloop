# Release Backport

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

The Release Backport preset (`backend/presets/018-release-backport.yaml`)
carries a change merged into one release branch forward to later branches.
It runs no agent. The Preloop control plane does the Git work and opens the
pull requests, and a person reviews and merges them.

## What it does

When a pull request (GitHub) or merge request (GitLab) merges into
`backport.source_branch`, the flow takes each entry of
`backport.target_branches`, in order:

1. Fetch the merge commit of the original pull request and cherry-pick it
   onto a new branch cut from the target tip. The branch is named
   `backport/pr-<number>-to-<target>`, with `/` in the target replaced by `-`.
   A merge commit is replayed against its first parent. A squash or rebase
   merge is a single commit and is picked as is.
2. On a conflict, push nothing, record the conflicting files and move on to
   the next target. Conflicts are left for a person.
3. On a clean pick, push the branch and open one pull request. The title is
   the original title followed by `(backport to <target>)`. The description
   is the original description followed by a `Backport of <original URL>`
   line and the cherry-picked commit. The configured reviewers are asked to
   review. If that request fails, the failure is recorded and the pull
   request stays open.

Nothing is ever merged. The execution result lists a status per target under
`result.backport.targets`, and one summary comment is posted on the original
pull request.

A merge into any other branch does not start the flow.

## Configuration

```yaml
trigger_event_types:
  - pull_request_merged    # GitHub
  - merge_request_merged   # GitLab
git_clone_config:
  enabled: false
  create_pull_request: false
  git_user_name: Preloop            # committer of the backport commits
  git_user_email: hello@preloop.ai
  backport:
    enabled: true
    source_branch: release/1.0      # release N
    target_branches:                # tried in order
      - release/1.1                 # release N+1
      - main
    reviewers: [maintainer-a]
    comment_on_original: true
```

Validation refuses:

- a `source_branch` listed among the targets;
- duplicate targets, or two targets that map to the same backport branch
  name (for example `release/1.1` and `release-1.1`);
- names that are not plain branch names;
- `create_pull_request`, `report_publication` or `follow_up_filing` next to
  an enabled backport block. The backport opens its own pull requests.

The original author is kept on each cherry-picked commit. Only the committer
is set from `git_user_name` and `git_user_email`.

## Target statuses

| Status | Meaning |
| --- | --- |
| `opened` | The branch was pushed and a pull request opened. |
| `updated` | A pull request was already open for this branch. Its title and description were refreshed and nothing was pushed again. |
| `exists` | A pull request for this branch was closed or merged. It is left alone. |
| `already_applied` | The change is already on the target. Nothing was pushed. |
| `conflict` | The cherry-pick conflicts. Nothing was pushed. The conflicting files are listed. |
| `failed` | The target branch is missing, or Git or the code host refused a step. The reason is recorded. |

The execution fails when any target is `conflict` or `failed`. The other
targets are still attempted.

## Retries and re-deliveries

The branch name is derived from the original number and the target, so a
repeated run finds its earlier work. It does not open a second pull request.
If the branch exists but no pull request does (for example, the host call
failed after the push), the next run opens the pull request without pushing
again. A backport branch is only ever created, never overwritten. A branch
that someone pushed to by hand is kept as it is.

Each run posts its own summary comment on the original pull request.

## Hosts

GitHub and GitLab are supported. Bitbucket Cloud support depends on the
pull request work tracked in issue #955. Until that lands, a Bitbucket
repository is refused with a message that says so. The runner talks to code
hosts through a small adapter (`preloop/services/backport_hosts.py`), so
adding Bitbucket means adding one adapter.

## Limits

- Only the merge commit is cherry-picked. If a GitHub pull request was
  merged by rebase and had several commits, `merge_commit_sha` names only the
  last of them, and only that commit is backported.
- The merge commit must be fetchable by id or reachable within the last 100
  commits of `source_branch`.
- Out of scope: conflict resolution, merging, Bitbucket Data Center and
  release notes.
