# Bitbucket and Jira software factory quickstart

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

This checklist connects a Jira project and a Bitbucket Cloud repository so
that a Jira ticket becomes a Bitbucket pull request through the Automated
Issue Implementation flow, with the pull request reviewer and the durable
feedback loop running on the result. Merging stays a human action throughout:
Preloop never merges or declines a pull request.

The pipeline, end to end:

1. A ticket lands in Jira. The triage preset assesses it and reports
   readiness.
2. A ready ticket triggers the implementation preset. The agent clones the
   bound Bitbucket repository, implements the change, pushes a branch and
   opens a Bitbucket pull request. The pull request URL is written back onto
   the Jira issue.
3. The reviewer preset reviews the pull request, posts inline comments and
   records approve or request changes. With commit statuses enabled, the
   verdict also lands as a build status on the head commit.
4. Review comments and request-changes verdicts from trusted reviewers wake
   the implementation thread for repair rounds until the pull request is
   approved. A person merges.

## 1. Connect the Bitbucket tracker

Follow [Bitbucket Cloud tracker](bitbucket-tracker.md). Notes that matter for
this pipeline:

- With a personal API token (`auth_type: api_token`,
  `token_kind: api_token`), also enter the Atlassian account email. It is
  used for the HTTP Basic fallback on the REST API and for pull request
  creation from the agent container. Git pushes authenticate with the
  `x-bitbucket-api-token-auth` username and the token; the email is never
  used as a git username.
- The token needs the read and write scopes for repositories, pull requests
  and webhooks listed in the tracker guide. The repository write scope also
  covers commit build statuses.
- Let the tracker sync, then confirm the target repository appears as a
  Preloop project and its webhook is registered.

## 2. Connect the Jira tracker

Add the Jira tracker for the site that hosts the project. Let it sync so the
Jira project exists as a Preloop project. Jira issue events (created,
updated, labeled, commented) become flow triggers.

## 3. Bind the Jira project to the Bitbucket repository

A Jira project has no repository of its own. Bind it to the Bitbucket
repository so issue-triggered flows can clone and publish. The binding is
described in the Jira project repository binding guide
(docs/guide/flows/jira-repository-binding.md, issue #957); in short, set
`settings.repository_bindings` on the Jira project:

```json
{
  "tracker_id": "<bitbucket tracker id>",
  "repository": "<workspace>/<repo-slug>",
  "base_branch": "main",
  "default": true
}
```

The bound tracker must be the Bitbucket tracker and the repository must be
one it has synced. The Jira token is never attached to the clone or push; the
git credential always comes from the Bitbucket tracker.

## 4. Clone the presets

Clone three presets from the catalog and point their triggers at the right
projects:

- `001-issue-triage-assistant` on the Jira project. It triages new tickets
  and reports readiness.
- `011-automated-issue-implementation` on the Jira project. It implements a
  ready ticket and opens the Bitbucket pull request. Review its
  `git_clone_config` and confirm git clone is enabled; the repository comes
  from the binding in step 3, so leave the flow's own `repositories` list
  empty.
- `002-pull-request-reviewer` on the Bitbucket repository project, triggered
  by pull request events. It honors the repository review policy file (step
  5).

Do not add the `agent-ready` label to live tickets while wiring things up;
that label is the implementation intake condition.

## 5. Review policy file

Add `.preloop/review-policy.md` to the root of the Bitbucket repository. The
reviewer preset reads it on every run and applies it on top of its generic
review rules: severity conventions, what blocks approval, what is advisory,
project-specific expectations. A missing file is fine; the reviewer says so
and reviews with its defaults.

## 6. Feedback loop and trusted reviewers

In the implementation flow, enable the feedback subscription (see
[Durable implementation feedback](flows/durable-implementation-feedback.md)).
For Bitbucket, `trusted_reviewer_ids` entries can be a Bitbucket username, an
account ID (`digits:uuid`) or a user UUID (with or without braces). Add the
reviewer's Bitbucket account there so its request-changes verdicts and
comments wake repair rounds.

Bitbucket specifics of the loop:

- Approve and request changes are participant states on the pull request, not
  standalone review objects. A push resets approvals only when the repository
  setting "reset approvals on new commits" is on; decide that setting
  deliberately.
- Required checks come from the flow policy (`required_checks`); commit build
  statuses posted by the reviewer or CI are matched by their status key.
- Use a dedicated Bitbucket account for the reviewer's token so its verdict
  is distinguishable from a person's.

## 7. First ticket

1. Create a Jira ticket with a small, well-scoped change.
2. Watch triage run and mark it ready (or mark it ready manually according to
   your readiness policy).
3. The implementation flow picks it up, clones the bound repository, pushes a
   branch and opens the pull request; the Jira issue gets a comment and a
   remote link with the pull request URL.
4. The reviewer flow reviews the pull request. Check the verdict, the inline
   comments and, if enabled, the build status on the head commit.
5. Reply to a review comment or request changes from a trusted reviewer
   account and confirm a repair round starts and pushes to the same branch.
6. Approve and merge by hand.

## Verification checklist

- Bitbucket tracker synced, repository visible as a project, webhook
  delivered on a test pull request event.
- Jira tracker synced, ticket events triggering the triage flow.
- Repository binding resolves: an issue-triggered dry run clones the
  Bitbucket repository, not nothing and not a wrong repository.
- Pull request opened by the implementation flow, URL written back to the
  Jira issue.
- Reviewer verdict recorded on the pull request; build status present when
  enabled.
- A trusted reviewer's request changes wakes a repair round; an untrusted
  account's comment does not.

## Known limits

- Bitbucket Data Center is not supported, Bitbucket Cloud only.
- OAuth token refresh is not implemented; expiring OAuth tokens need a
  reconnect. Prefer API tokens or repository access tokens for unattended
  flows.
- Isolated publication (downscoped, single-repository credentials) is GitHub
  App only; Bitbucket publication uses the tracker credential in legacy
  publication mode.
