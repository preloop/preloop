# GitLab self-hosted tracker

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

A GitLab tracker synchronizes group projects and issues, registers webhooks,
reviews merge requests and lets an implementation flow push a branch and open
an MR. GitLab Community Edition uses project webhooks; group webhooks require
a GitLab tier that supports them.

## Create a bot and token

Create a dedicated user such as `preloop-bot`, give it access to the group and
projects you want to automate, and create a personal access token with an
expiry date. Store the token in Preloop's tracker credential field. Do not put
it in a repository URL, flow prompt, shell history or screenshot.

Use the `api` scope for the full integration. It permits authenticated API
reads and writes, and Git over HTTP read/write access. Separate
`read_repository` and `write_repository` scopes are redundant when `api` is
present, although selecting all three also works. A repository-only token
cannot register webhooks or publish review comments. `read_api` is suitable
only for read-only API access, not this end-to-end workflow.

Prefer a dedicated user's personal token for predictable discovery: Preloop
lists groups, then their projects. A project token's bot identity sees only
its membership scope; do not assume it can discover the parent group or other
projects. Group access tokens are an alternative where your GitLab edition
supports them and their bot can discover the intended group. Neither token
scope nor an administrator's token bypasses project membership and branch rules.

| Feature | Minimum practical access |
| --- | --- |
| Read private source and issues | Reporter on the project, with the relevant read scope or `api` |
| Post review discussions and commit statuses, create MRs, push unprotected branches | Developer on the project, `api` for the API operations |
| Register project webhooks automatically | Maintainer on each project, `api` |
| Register a group webhook | Owner on the group, `api`, and a GitLab tier supporting group hooks |
| Approve an MR | Developer or higher, `api`, and eligibility under the project's approval settings |

For the automatic setup below, grant Maintainer on the selected projects.
On CE, group hooks return 404 and Preloop falls back to project hooks. Owner
access to the group is needed for group-hook setup on supported editions;
Maintainer on a project does not grant that access. A 403 is a permissions
failure, not the CE detection signal.

## Connect the instance

1. In **Trackers > Add tracker > GitLab**, enter the instance root URL, for
   example `https://gitlab.example.com`, and the bot token. Use the URL your
   Preloop services and agent containers can reach. The client also accepts
   an `/api/v4` suffix and removes it.
2. Test the connection. Select the group and intended projects, then save.
   A successful connection test proves authentication and discovery, not
   webhook delivery or branch push permission.
3. Wait for the first scan. Open **Settings > Webhooks** in each selected
   GitLab project and verify the callback. Check the group hooks instead if
   you use group hooks on a supported edition.

The connection test must finish within 8 seconds (3 seconds to connect, 5
seconds per read). An instance that answers more slowly fails the test with
"did not respond"; check the URL and the network path from the API. A private
instance URL works unless the operator set `TRACKER_BLOCK_PRIVATE_TARGETS`
(Helm `trackers.blockPrivateTargets`), which multi-tenant instances use to
refuse private and cluster-internal addresses. Link-local and cloud metadata
addresses are always refused.

Preloop's general URL classifier recognizes GitLab.com and hostnames with a
complete `gitlab` label, such as `gitlab.example.com`. An arbitrary instance
name such as `code.example.com` does not identify its platform by URL alone.
Tracker-backed clone and MR publication can use the stored GitLab type; use
the tracker/project binding rather than an unbound raw URL, and test your
chosen hostname end to end.

## Issues load but merge requests fail

Issues are read from Preloop's synced data; merge requests are fetched live
from GitLab. A previous successful connection check does not prove that the
saved token still works. For a rejected token (HTTP 400), edit the tracker
and replace an expired, revoked or invalid token. For denied access (HTTP
403), check the token's `api` scope and membership in the selected group and
project. A configuration error (HTTP 400) asks you to check the GitLab URL
and re-enter the token. Provider connection failures return HTTP 502.

## Webhooks and network access

Set `PRELOOP_URL` on the API, scanner/scheduler and workers to the externally
reachable Preloop origin. The automatically registered callback is:

```text
https://preloop.example.com/api/v1/private/webhooks/gitlab/<organization-id>
```

`<organization-id>` is Preloop's organization UUID, not GitLab's numeric group
ID. The scanner generates the organization's secret and sends it to GitLab
as the webhook secret token. GitLab delivers it in `X-Gitlab-Token`; Preloop
validates it. Treat it as a credential. There is no console field that reveals
this secret for a manual copy-and-paste setup.

Preloop requests issues, pushes, merge requests, notes, pipelines, jobs and
repository updates, with TLS verification enabled. GitLab may ignore unsupported
events (CE project hooks do not enable repository updates). Verify the enabled events
in GitLab and use a test delivery; a sync that looks healthy is insufficient.
Registration failures can be logged without an obvious tracker setup error.
Inspect the scanner/worker logs and GitLab's recent deliveries if no flow starts.

Connectivity is required in both directions:

- Preloop API and sync workers must resolve and reach GitLab's API.
- Agent containers must reach GitLab for clone/push and API publication, and
  reach Preloop's MCP and model gateway endpoints.
- GitLab must resolve and reach the Preloop callback origin. A browser reaching
  the console does not prove the GitLab container can reach it.
- With a private callback address, a GitLab administrator must enable
  **Admin > Settings > Network > Outbound requests > Allow requests to the
  local network from webhooks and integrations**. The application-settings API
  field is `allow_local_requests_from_web_hooks_and_services`.

Use trusted HTTPS and matching certificates for a durable installation. HTTP
is appropriate only for an isolated local rehearsal. On Docker Desktop,
`host.docker.internal` points to the host from containers. For example, GitLab
can advertise `http://host.docker.internal:53080` while Preloop advertises
`http://host.docker.internal:53173`; publish those ports on the host and verify
them from both containers. On Linux, configure the host gateway explicitly or
use DNS names routable from every participating network. `localhost` inside
one container refers to that container.

## Review and approval settings

Clone **Pull Request Reviewer** from Flows > Presets, select the GitLab project
and a configured model, and select the GitLab tracker as the event source. Enable
`merge_request_opened`. Add `merge_request_updated` if subsequent pushes should trigger another review.
Enable gateway routing for a chat-only local model used by Codex so the
Preloop gateway can provide its Responses API adapter. The model endpoint must
be reachable from the gateway, for example the host's Ollama `/v1` endpoint.
For a private model address, add its exact IP CIDR to
`MODEL_ENDPOINT_ALLOWED_CIDRS` on the Preloop services (for example
`192.0.2.10/32`, replaced with your actual host address), then restart them.
Include every private address the hostname resolves to, including IPv6.
The default endpoint policy rejects private addresses; opening GitLab's
webhook setting does not change Preloop's model endpoint policy.

GitLab approval rules and required approval counts depend on edition. CE can
record approvals, but does not provide the same required-rule enforcement as
paid editions. On an edition with approval rules, make the bot an eligible
approver. Check settings preventing authors or committers from approving;
use a separate human or fixture author when testing bot approval. A reviewer
finding, discussion or commit status is separate from GitLab's merge gate.
Do not assume an approval alone enforces a policy or that CE provides a native
GitHub-style request-changes gate. Inspect both the review summary and status.
The `preloop` commit status records whether the execution completed: a reviewer
can post **Changes Requested** while that status is successful. Configure
GitLab's merge checks separately. Requiring a successful pipeline blocks failed
tests; a review discussion still does not create a required approval rule.

## Issue to merge request

Clone **Automated Issue Implementation**, bind it to the same project and
model, and configure its label filter for `agent-ready`. Check the preset's
issue event types and label an existing issue after the hook is active. Leave
MR creation enabled in the preset's Git configuration. Use a small issue with
explicit acceptance criteria for the first run.

Configure the implementation preset's runner-controlled verification profile
before changing code. Its generic default refuses non-documentation changes
until an operator supplies checks for the repository. For a Python project
using standard-library tests, append this rule to
`git_clone_config.verification.profile.rules`, preserving the existing universal
checks and unknown-path default:

```yaml
- id: python-unit-tests
  path_globs: ["*.py", "**/*.py"]
  commands:
    - id: unit-tests
      command: python -m unittest
      reason: Verify the project test suite
      scope: shared
```

Use your repository's actual test command and toolchain. The gate re-runs it
against the final commit before publishing; an agent's claim that tests passed
does not replace the gate. See [Automated Issue Implementation](flows/automated-issue-implementation.md).

Allow the bot to push its implementation branch. Do not protect the
`preloop/*` branch pattern unless the bot is explicitly allowed to push it.
Keep the default branch protected and use an MR. Check push rules for verified
committer email, signed commits and branch names; a valid token cannot override
those rules. Configure the flow's Git author identity to satisfy your policy.

Preloop uses a credential helper for HTTP clone/push, pairing the token with
`gitlab-ci-token`; the remote URL stays credential-free. GitLab personal access
tokens accept a nonempty username for this transport. A CI job token is a
different credential and is not a substitute for the bot's API token.

## First-run acceptance checklist

- Confirm the registered hook and a successful delivery in GitLab.
- Open an MR with a clear defect. Confirm the webhook starts an execution and
  the reviewer posts a finding and a blocking verdict/status as configured.
- Open a clean MR from another author. Confirm the review and any configured
  approval on the current head.
- Label a small issue `agent-ready`. Confirm an execution, pushed branch,
  linked MR and a subsequent MR review.
- Record elapsed time, model tokens and cost from each execution. A local model
  may have no provider invoice; an absent price is not a measured zero cost.

For a failed run, retain its execution ID, current commit SHA, webhook response
and relevant redacted logs. Re-run after a fix and distinguish a failed attempt
from a successful acceptance run.
