# Jira project repository binding

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

A Jira project tracks issues but has no git repository. A flow triggered by a
Jira issue event can still clone, push and open a pull request once the Jira
project is bound to a repository on a code host (GitHub, GitLab or Bitbucket
Cloud). A
new code host becomes bindable by registering it in `TRACKER_CLASSES` and
setting `hosts_repositories` on its client; issue-only triggers are gated on
`hosts_issues`, so registering a code host does not treat it as Jira-like.

## Where the binding lives

Two places, checked in this order:

1. `git_clone_config.repository_bindings` on the flow.
2. `settings.repository_bindings` on the Jira project, the default every flow
   on that project inherits.

A non-empty flow list overrides the project default. The project default is
set with `PUT /api/v1/projects/{id}`. That call replaces the whole `settings`
object, so send the existing keys along with `repository_bindings`. There is
no form for it in the console yet. Each entry:

```json
{
  "tracker_id": "<code-host tracker id>",
  "repository": "owner/name",
  "base_branch": "main",
  "default": true
}
```

- `repository` is the path on the code host (`group/subgroup/name` on
  GitLab). A leading `/` or trailing `.git` is removed.
- `base_branch` is optional. Without it the flow's `source_branch` is used,
  then `main`.
- With one entry, that entry is used. With several, exactly one must set
  `default: true`. The repository is never guessed from the issue text.

## When it applies

Only when all of these hold: git clone is enabled, the flow lists no
`repositories` of its own, the trigger came from an issue-only tracker (Jira),
and a binding exists. GitHub, GitLab and Bitbucket triggered flows are
unchanged.

The execution fails with a clear message, before any token is minted, when
the binding names a tracker of another account, a deleted or inactive
tracker, a tracker that hosts no repositories, or a repository that tracker
has not synced.

## Credentials

The git credential comes from the bound code-host tracker only. The Jira
token is never attached to the clone or push, and the agent container does
not fall back to the triggering tracker or project for a bound run.

## Writing the pull request back to Jira

When the run opened a pull request, Preloop writes it back onto the Jira
issue:

- a comment with the pull request URL and branch (Jira REST v3, Atlassian
  Document Format), and
- a remote link (`POST /rest/api/3/issue/{key}/remotelink`). The link's
  `globalId` is keyed on the repository, so a later run on the same issue
  and repository updates the link instead of adding another one. Issue
  linking must be enabled on the Jira site.

The development information panel (the Jira Software devinfo API) is not
used: it needs a Connect or Forge app.

When the write-back comment was posted, the generic "PR opened" comment from
`notifications.on_success` is skipped so the URL is not commented twice.

## Jira triggers

Jira `jira:issue_updated` webhooks are classified from the changelog:

| Changelog | Event type |
| --- | --- |
| `labels` gained a value | `issue_labeled` |
| `status` changed | `issue_status_changed` |
| `labels` lost a value only | `issue_unlabeled` |
| anything else | `issue_updated` |

One edit can carry several of these. The delivery is classified by the
first row that applies, and flows subscribed to any other row it carries
also receive it. Flows subscribed to `issue_updated` receive every edit, as
they did before label and status changes had their own types.

Each flow's conditions are evaluated as the type it subscribed to. On
`issue_labeled` and `issue_unlabeled`, `trigger_config.labels` matches the
labels this edit added or removed. On `issue_updated` it matches the
issue's current labels. `trigger_config.status_to` (the "Moved to status"
field in the flow form) matches the new status name exactly.

## Known limits

- Isolated publication binds GitHub repositories only, so a GitLab or
  Bitbucket binding runs in the default publication mode.
- The issue lifecycle merge audit is GitHub-only and expects the issue and
  the pull request in the same project, so it does not follow a bound Jira
  pull request yet.
- Jira events are never treated as bot events: the loop guard reads the
  sender on GitHub, GitLab and Bitbucket only. A label or status change that
  a flow makes on a Jira issue therefore starts every flow subscribed to it.
  Avoid a flow whose own edit matches its trigger.
