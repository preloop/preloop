# Bitbucket Cloud tracker

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

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

Pick one of three modes when you add the tracker. The first two store a
token you paste (unmanaged); the third stores no token at all.

| Mode | `auth_type` | Token on the row | Renewed | Expiry shown |
| --- | --- | --- | --- | --- |
| API token or repository access token | `api_token` | yes, encrypted | no | the date you enter |
| Pasted OAuth access token | `oauth_token` | yes, encrypted | no | the date you enter |
| Managed Bitbucket Cloud connection | `managed_oauth` | no | yes, by the provider service | the real expiry from the provider status |

Editing a pasted-token tracker never converts it into a managed connection,
and a managed tracker never accepts a pasted token: `PUT /api/v1/trackers/{id}`
with `api_key` answers 409 for it. Use **Reconnect** or **Disconnect** instead.

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

### Pasted OAuth access token (`auth_type: oauth_token`)

Paste an OAuth access token. Preloop sends it as Bearer. A pasted token is
unmanaged: Preloop does not refresh it, so replace the token before it expires
(or use the managed connection below).

### Managed Bitbucket Cloud connection (`auth_type: managed_oauth`)

Editions: Enterprise (the consent and refresh service is a managed-provider
plugin). The console, REST, MCP, scanner, flow clone and publication paths
that consume it ship in OSS and stay inert without the plugin.

When the deployment advertises `bitbucket_cloud_oauth` in `/api/v1/features`,
**Add tracker > Bitbucket Cloud** offers **Connect Bitbucket**. The browser is
sent to Bitbucket for consent; the callback returns to the console with an
opaque completion handle only (never the authorization code or a token), the
console exchanges it once, and the tracker is created with `auth_type:
managed_oauth` and no `api_key`. The modal then shows the authorizing actor,
the workspaces the actor can access, an optional repository, and the existing
scope step. The handle and the error code are removed from the address bar
immediately.

Before every Bitbucket call Preloop asks the provider resolver for the current
access token: REST calls from the console and MCP tools, the scanner, the
flow clone, late push and PR creation, and feedback reads. Requests are
pinned to `https://api.bitbucket.org/2.0`, redirects are refused, a 401
forces exactly one refresh, and there is no Basic fallback and no fallback to a
stale stored token or an anonymous clone. If the resolver is missing (plugin
not installed), the grant needs reconnect, or the provider refuses, the
operation fails with that reason instead.

The tracker page shows the connection state (`connected`,
`workspace_required`, `reconnect_required`, `disconnected`, `unavailable`),
the actor, the real access-token expiry reported by the provider (there is no
date to enter), and the capabilities derived from the consented scopes. A
capability is shown as granted, missing or unknown; discovery success is not
presented as proof that push, approval or webhook registration work until
they are first exercised. **Reconnect** starts consent again for the same
tracker; **Disconnect** erases the grant locally and disables the tracker.
With the feature flag off, managed trackers are marked `provider unavailable`
and cannot refresh; pasted-token trackers are unaffected.

### Git credentials

Clones and pushes use HTTPS with the token as the password, installed through
a git credential helper so the remote URL never carries it. The username is
never your email:

| Token | Git username |
| --- | --- |
| Personal API token, `username` set | your Bitbucket username |
| Personal API token, no `username` | `x-bitbucket-api-token-auth` |
| Repository access token, pasted OAuth token or managed connection | `x-token-auth` |

Preloop rejects a `username` that contains `@`. Put the email in `email`.

For a managed connection the clone uses the access token current at launch.
Hosted legacy publication then reacquires a fresh token from the control
plane immediately before the late push and the pull request REST calls,
through the same execution-bound broker GitHub App flows use: the runner
presents its capability, the control plane checks that the execution is still
running and not stopped, that the tracker belongs to the account, and that the
repository is inside the tracker's workspace (and bound repository), and
returns the current access token with its git username. Refresh tokens and the
OAuth consumer secret never enter the agent container. If the grant was revoked
or disconnected meanwhile, publication fails with `reconnect required`, the
recovery bundle under the evidence directory is kept, and a retry reuses the
existing branch and pull request rather than opening a second one. Isolated
publication and OAuth downscoping are not available on Bitbucket Cloud; a
managed grant still publishes in legacy mode with its documented credential
exposure.

### Token expiry

Bitbucket does not report a pasted token's expiry through the API. Enter the
date in **Token expires on** when you add or edit the tracker. The tracker
page then shows **Token expires <date>** from 14 days before the date, and
**Token expired** after it. A 401 from Bitbucket also names expiry as a likely
cause.

A managed connection has no such field: its access token is short-lived and
renewed by the provider service, and the tracker page shows the actual expiry
from the provider status.

## Add the tracker

Console: **Trackers > Add tracker**, type **Bitbucket Cloud**. With the
managed provider configured, **Connect Bitbucket** is offered first and the
fields below sit under **Or paste a token instead**.

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
answered with 400 and a message that says what to fix. `auth_type:
managed_oauth` is rejected here as well: managed connections are only created
by the consent completion endpoint.

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

- Bitbucket Data Center (see the separate guide for the 10.2 adapter).
- Refresh of a pasted OAuth access token: it is not renewed, the tracker must
  be edited with a new token when it expires. Only the managed connection is
  renewed.
- Isolated publication. Bitbucket tokens, managed or pasted, cannot be
  downscoped to one repository the way a GitHub App lease can, so flows that
  publish to Bitbucket use legacy publication mode.
- Merge and decline policy: Preloop never merges or declines on Bitbucket,
  with any credential kind.
