# Bitbucket Cloud tracker

A Bitbucket tracker connects Preloop to one Bitbucket Cloud workspace. Its
repositories become Preloop projects, pull request events start flows, and the
pull request reviewer preset can read a pull request, comment on it and record
a verdict. Issues are not synced: keep them in Jira.

Bitbucket Data Center is not supported.

For the end-to-end setup where a Jira ticket becomes a Bitbucket pull request
(tracker, Jira repository binding, presets, review policy, first ticket), see
the [Bitbucket and Jira quickstart](bitbucket-jira-quickstart.md).

## What Preloop does and does not do

Preloop can:

- read a pull request, its diff, diffstat, commits and comments;
- post general comments, inline comments (on the new or the old side of the
  diff) and replies;
- edit comments and resolve or reopen comment threads;
- approve or withdraw an approval, and request changes or remove the request;
- open pull request tasks on review comments, when the token may (a 403 is
  logged and skipped, the review still goes through);
- create a pull request from a flow and update its description on later
  rounds;
- post a commit build status for a reviewer verdict, when the flow enables it;
- register one webhook per repository and verify every delivery.

Preloop never merges or declines a pull request. The client refuses those
endpoints outright, and the `update_pull_request` tool rejects
`state: "closed"`, `"declined"` and `"merged"`.

## Authentication

Pick one of two modes when you add the tracker.

### API token or repository access token (`auth_type: api_token`)

- **Personal API token** (`token_kind: api_token`). Create an Atlassian API
  token with Bitbucket scopes. Preloop sends it as `Authorization: Bearer`. If
  Bitbucket answers 401 and you entered your Atlassian account email, Preloop
  retries once with HTTP Basic `email:token` and keeps using Basic for that
  client.
- **Repository access token** (`token_kind: access_token`). Created in the
  repository settings and bound to that one repository, so the tracker also
  needs `repository` set. Sent as Bearer.

Scopes for a personal API token (they do not imply each other):

- `read:repository:bitbucket` and `write:repository:bitbucket` (source, clone
  and push);
- `read:pullrequest:bitbucket` and `write:pullrequest:bitbucket` (comments,
  approvals, request changes and tasks);
- `read:webhook:bitbucket` and `write:webhook:bitbucket` (webhook
  registration).

A repository access token needs `repository`, `repository:write`,
`pullrequest`, `pullrequest:write` and `webhook`. Write access on pull requests
also allows merge and decline in Bitbucket; Preloop never calls those. If a
task call is refused (403), Preloop skips the task and says so in the result.

**App passwords are rejected.** Bitbucket is retiring them. Preloop refuses a
secret that starts with the app password prefix `ATBB` and an `auth_type` or
`token_kind` of `app_password`, and tells you to create an API token instead.

### OAuth access token (`auth_type: oauth_token`)

Paste an OAuth access token. Preloop sends it as Bearer. Preloop does not
refresh OAuth tokens yet, so replace the token before it expires.

### Git credentials

Clones and pushes use HTTPS with the token as the password. The username is
never your email:

| Token | Git username |
| --- | --- |
| Personal API token, `username` set | your Bitbucket username |
| Personal API token, no `username` | `x-bitbucket-api-token-auth` |
| Repository access token or OAuth token | `x-token-auth` |

Preloop rejects a `username` that contains `@`. Put the email in `email`.

### Token expiry

Bitbucket does not report a token's expiry through the API. Enter the date in
**Token expires on** when you add or edit the tracker. The tracker page then
shows **Token expires <date>** from 14 days before the date, and
**Token expired** after it. A 401 from Bitbucket also names expiry as a likely
cause.

## Add the tracker

Console: **Trackers > Add tracker**, type **Bitbucket Cloud**.

| Field | Required | Notes |
| --- | --- | --- |
| Workspace | yes | The workspace ID from `bitbucket.org/<workspace>` |
| Authentication | yes | API token or access token, or OAuth access token |
| Token kind | API token mode | Personal API token or repository access token |
| Repository | access tokens | Repository slug. Optional otherwise: it limits the tracker to one repository |
| Atlassian account email | no | Enables the Basic auth fallback for personal API tokens |
| Bitbucket username | no | Git username for personal API tokens |
| Token expires on | no | Drives the expiry warning |

API (`POST /api/v1/trackers`):

```json
{
  "name": "Bitbucket",
  "type": "bitbucket",
  "auth_type": "api_token",
  "api_key": "<token>",
  "config": {
    "workspace": "example-workspace",
    "token_kind": "api_token",
    "email": "reviewer@example.com",
    "username": "example-user",
    "token_expires_at": "2027-01-31"
  },
  "scope_rules": [
    {"scope_type": "ORGANIZATION", "rule_type": "INCLUDE", "identifier": "example-workspace"}
  ]
}
```

The configuration is checked before any network call. A bad configuration is
answered with 400 and a message that says what to fix.

In the scope step, repositories are grouped by their Bitbucket project. The
tracker page groups synced repositories the same way.

## Projects

Each repository becomes a project:

- identifier: the repository UUID without braces;
- slug: `workspace/repo`, which is also what MCP tools accept;
- group: the Bitbucket project name.

## Webhooks

The sync registers one hook per repository at
`/api/v1/private/webhooks/bitbucket/<organization id>`, reusing the
organization's webhook secret. An existing hook with the same URL is updated
instead of duplicated.

Subscribed events: `repo:push`, `pullrequest:created`, `pullrequest:updated`,
`pullrequest:approved`, `pullrequest:unapproved`,
`pullrequest:changes_request_created`, `pullrequest:changes_request_removed`,
`pullrequest:fulfilled`, `pullrequest:rejected`,
`pullrequest:comment_created`, `pullrequest:comment_updated` and
`pullrequest:comment_deleted`.

Every delivery must carry `X-Hub-Signature: sha256=<hex>`, the HMAC-SHA256 of
the raw body with the secret. Preloop compares it in constant time. A missing
signature and a wrong signature are both answered with 403. The event comes
from `X-Event-Key` and the delivery id from `X-Request-UUID`.

Events map onto the shared flow trigger names, so a flow written for GitHub
reacts to Bitbucket the same way. Comment events use the same names GitHub
and GitLab comment events use:

| Bitbucket event | Flow event |
| --- | --- |
| `pullrequest:created` | `pull_request_opened` |
| `pullrequest:updated` | `pull_request_updated` |
| `pullrequest:fulfilled` | `pull_request_merged` |
| `pullrequest:rejected` | `pull_request_closed` |
| `pullrequest:approved` | `pull_request_approved` |
| `pullrequest:unapproved` | `pull_request_unapproved` |
| `pullrequest:changes_request_created` | `pull_request_changes_requested` |
| `pullrequest:changes_request_removed` | `pull_request_changes_request_removed` |
| `pullrequest:comment_created` | `comment_created` |
| `pullrequest:comment_updated` | `comment_updated` |
| `pullrequest:comment_deleted` | `comment_deleted` |
| `repo:push` | `push` |

`pull_request_opened` starts the pull request reviewer preset. The trigger
payload also carries `object_attributes` (title, description, url, branches,
state, draft, author, number) in the same shape GitHub pull requests use, so
the preset prompt is the same for every provider.

## Required approvals

Bitbucket merge checks can require a minimum number of approvals. The author's
own approval does not count. Preloop approves as the tracker's token owner, so
that approval counts like any other reviewer's:

1. The branch requires two approvals and one person has already approved.
2. The reviewer flow approves (`review_action: "approve"`). Bitbucket now
   counts two approvals.
3. A person merges. Preloop never does.

Request changes is a separate participant state. The reviewer can remove it
(`remove_request_changes`) or withdraw its approval (`unapprove`) on a later
run. Use a dedicated Bitbucket account for the reviewer's API token, so its
verdict is not mistaken for a person's.

## MCP tools

`get_pull_request`, `add_comment`, `update_pull_request` and `update_comment`
accept a Bitbucket pull request URL
(`https://bitbucket.org/<workspace>/<repo>/pull-requests/<id>`) or
`workspace/repo#id`.

- `add_comment`: `path` and `line` make an inline comment; passing only one
  of them is rejected. `side: "LEFT"` comments on the old file line.
  `in_reply_to` is the parent comment id.
- `update_pull_request`: `review_action` is `approve`, `request_changes` or
  `comment`, and on Bitbucket also `unapprove` and `remove_request_changes`.
  `review_body` is posted as a comment. Each `review_comments` entry becomes
  an inline comment; add `"task": true` to also open a task on it. Title and
  description can be changed. Labels, assignees, reviewers, draft and
  reactions are not supported and are reported as ignored. Bitbucket has no
  atomic review, so every input is validated before the first call, the
  comments are posted first and the verdict is applied last. If a comment
  fails, no approval or change request is recorded.
- `update_comment`: `body` edits the comment, `resolved` resolves or reopens
  the thread. Pass the thread's top comment as `thread_id` when you resolve
  from a reply.

`create_pull_request` opens a pull request from `source_branch` to
`target_branch`. `reviewers` takes Bitbucket account IDs, user UUIDs or
nicknames; unresolvable entries are reported as ignored, never fail the
creation. `extra_options.close_source_branch` (or `remove_source_branch`,
the GitLab spelling) sets the close-source-branch flag. Draft, labels,
assignees and milestone do not exist on Bitbucket pull requests and are
reported as ignored.

Deleting comments is available in the tracker client but not exposed as an
MCP tool.

## Commit build statuses

When a flow enables commit statuses for reviewer verdicts (the same option
that produces GitHub check runs), the reviewer posts a build status on the
pull request's head commit
(`POST /repositories/{workspace}/{repo}/commit/{sha}/statuses/build`):
`SUCCESSFUL` for approve, `FAILED` for request changes, `INPROGRESS` while
the review runs. The status key is `preloop`, so a later run updates the same
status instead of stacking new ones. The status carries the pull request's
source branch as `refname`, which is what makes Bitbucket show it on the pull
request and not only on the commit. A Bitbucket merge check can then require
the status before merge.

## Not supported yet

- Bitbucket Data Center.
- OAuth token refresh: an expiring OAuth access token is not renewed, the
  tracker must be reconnected when it expires.
- Isolated publication. Bitbucket tokens cannot be downscoped to one
  repository the way a GitHub App lease can, so multi-repository flows that
  publish to Bitbucket use legacy publication mode.
