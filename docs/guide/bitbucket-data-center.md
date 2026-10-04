# Bitbucket Data Center 10.2 LTS

The optional `bitbucket_dc` provider supports manual user PAT configuration,
repository discovery and pull request review against the Bitbucket Data Center
10.2 LTS REST contract. It is separate from `bitbucket` (Bitbucket Cloud).
Existing Cloud trackers keep their identities and configuration.

This support is **fixture-tested, not live-certified**. Synthetic fixtures are
checked against Atlassian's [versioned 10.2 OpenAPI document](https://developer.atlassian.com/server/bitbucket/10.2.swagger.v3.json)
and [10.2 reference](https://developer.atlassian.com/server/bitbucket/rest/v1002/intro/).
Other server releases are unsupported/unvalidated; they never fall back to Cloud.
A release owner must validate an explicitly approved test instance before claiming
live support. No external instance is needed to run the regression tests.

## Deployment policy

The provider and its console option are off by default. An administrator enables
`PRELOOP_BITBUCKET_DC_ENABLED=true` and sets
`PRELOOP_BITBUCKET_DC_INSTANCES` to a JSON array of approved canonical HTTPS
instance URLs, including any non-default port and context path. For example:

```sh
PRELOOP_BITBUCKET_DC_ENABLED=true
PRELOOP_BITBUCKET_DC_INSTANCES='["https://bitbucket.example.com/stash"]'
```

Private-network destinations additionally require explicit deployment CIDRs in
`PRELOOP_BITBUCKET_DC_PRIVATE_NETWORKS` (comma separated). Approval of a hostname
alone does not approve private addresses. Metadata, link-local and other forbidden
addresses remain blocked. The transport validates resolved destinations at
connection time. Redirects are rejected; environment proxies cannot bypass the
policy. URL userinfo, query strings, fragments and traversal are rejected.

TLS verification stays enabled. Configure `PRELOOP_BITBUCKET_DC_CA_BUNDLE` with a
trusted private CA bundle when the instance uses a private certificate authority.
Do not disable certificate verification. Trust settings are deployment-owned;
tracker JSON cannot supply a CA path, private-network allowlist or TLS override.
Cloud's host restrictions remain unchanged.

## Manual setup

In **Add tracker**, choose **Bitbucket Data Center** and enter the approved
instance URL and a user personal access token. REST uses `Authorization: Bearer`.
The optional **Reviewer user slug** identifies your user for reviewer verdicts
when the instance does not supply a current-user response header; it is not
an HTTP Basic username. Use the minimum project/repository permissions needed: project/repository read for
discovery and PR read; repository write for creating/updating PRs, comments,
review verdicts, tasks and build statuses. Instance administrators may impose
additional permission policy. Repository administration is only needed if you
want Preloop to register the webhook itself (see [Webhooks](#webhooks)); a
repository administrator can add it by hand instead. This is a user PAT
integration; managed OAuth is a separate feature.

Discover projects and repositories, then select the desired scope. Project key,
numeric repository ID and repository slug are separate fields. The numeric ID is
stable across a repository rename; it is not a Cloud workspace identity. A repository ID or slug binding requires its project key. Credentials
are encrypted through the existing tracker SecretReference/CRUD path. They are
never part of public connection details or tracker responses. A stored credential
cannot be reused by discovery against another instance; create a new tracker when
changing instances.

DC hosts repositories and pull requests. Keep issue tracking in Jira. Merge,
decline, managed OAuth and execution/publication routing are outside this
integration. Unsupported operations report that limitation rather
than returning a success result. In particular, 10.2 marks `multilineMarker` as
read-only, so creation of ranged diff anchors is unsupported; multiline text,
single-line diff anchors and replies are supported. Returned clone URLs are metadata only and do not
authorize a connection or publication.

## Webhooks

Webhook delivery is what starts review and feedback flows. It is separate from
the connection test: a token that reads pull requests can still lack the
repository admin right to manage hooks, and a hook only works once Preloop
holds its secret. The webhook endpoints report each of those states on its own.

**Callback.** Each tracker has its own callback URL,
`<PRELOOP_URL>/api/v1/private/webhooks/bitbucket_dc/<tracker id>`. `PRELOOP_URL`
must be set, and the Data Center instance must be able to reach that URL over
HTTPS (check proxies, firewalls and the instance's outbound allowlist).

**Secret.** `POST /api/v1/trackers/<id>/bitbucket-dc/webhook/secret` generates
a per-tracker secret, stores it encrypted and returns it once. Rotating again
replaces it: deliveries signed with the old secret are rejected right away, so
update the hook immediately afterwards.

**Required events.** Subscribe the hook to exactly these 10.2 events:

| Event key | Preloop event |
| --- | --- |
| `repo:refs_changed` | `push` |
| `pr:opened` | `pull_request_opened` |
| `pr:from_ref_updated` | `pull_request_updated` (new head) |
| `pr:modified` | `pull_request_updated` (title, description or target) |
| `pr:reviewer:approved` | `pull_request_approved` |
| `pr:reviewer:unapproved` | `pull_request_unapproved` |
| `pr:reviewer:needs_work` | `pull_request_changes_requested` |
| `pr:comment:added` | `comment_created` |
| `pr:comment:edited` | `comment_updated` |
| `pr:comment:deleted` | `comment_deleted` |
| `pr:merged` | `pull_request_merged` |
| `pr:declined`, `pr:deleted` | `pull_request_closed` |

**Administrator setup (default).** A repository administrator opens
**Repository settings**, **Webhooks**, **Create webhook**, enters the callback
URL and the secret, selects the events above and saves.

**Registration by Preloop.** When the tracker's personal access token already
has repository admin rights, `POST /api/v1/trackers/<id>/bitbucket-dc/webhook/register`
creates the hook, or updates Preloop's existing hook in place after a secret
rotation. It only touches the hook whose URL is the tracker's callback URL and
never deletes or edits other hooks. Without admin rights it answers `403` with
the administrator instructions; Preloop never asks for broader permissions.

**Inspection.** `GET /api/v1/trackers/<id>/bitbucket-dc/webhook?check=true`
returns the callback URL, the required events, whether a secret is configured
(`signature`), and the hook state on the bound repository: `registered`,
`events_missing` (with the missing events), `inactive`, `missing`,
`permission_denied` or `unauthorized`.

**What the intake enforces.**

* The `X-Hub-Signature: sha256=<hex>` HMAC is checked over the raw bytes,
  in constant time, before the body is parsed. Unsigned deliveries are
  rejected; there is no unsigned fallback. 10.2 sends no signed timestamp, so
  none is required.
* Bodies above 1 MiB are refused.
* The payload repository must be the tracker's bound repository (by numeric
  id and project key), or an imported repository for an unbound tracker, and
  any `links.self` must point at the approved instance. Anything else is
  rejected rather than trusted.
* `X-Request-Id` (or, without it, a digest over the immutable ids and the raw
  body) becomes a tracker-scoped delivery id. A redelivery, or two workers
  racing on the same delivery, creates at most one execution per flow.
* A `pr:from_ref_updated` whose head did not change, `diagnostics:ping` and
  events not in the list above are acknowledged with `status: ignored` and a
  reason (`head_unchanged`, `ping`, `unsupported_event`, `not_subscribed`) and
  start nothing.
* Events whose actor is the tracker's configured user slug are treated as
  Preloop's own and do not start flows (reviewer comments with Preloop's
  review marker still hand off as on other providers).
* A pull request update that arrives after a newer one (lower PR `version`
  than an active run) neither starts a run nor supersedes the newer run.

**Troubleshooting deliveries.** The hook's **View details** page on the
instance shows each delivery's HTTP status. `403 Missing Bitbucket signature`
means the hook has no secret; `403 Invalid Bitbucket signature` means the
secret differs from Preloop's (rotate and update the hook);
`403 repository_mismatch` means the hook is on a repository the tracker is not
bound to; `404` means the tracker id is wrong or the Data Center feature is
off; `200` with `status: ignored` names the reason the delivery started nothing.
The whole feature stays behind `PRELOOP_BITBUCKET_DC_ENABLED`.

## Repository identity and rename limits

Before each repository resource request, the adapter reads the repository and
compares its numeric ID with the binding. If the old slug has disappeared or
been reused, it discovers the original ID within the approved project and uses
that repository's current slug. If the original ID cannot be found, it refuses
the operation. PR creation also uses the resolved ID and slug in its reference
objects before sending the request.

The verified 10.2 REST paths address repositories by slug. This adapter does not
assume that substituting a numeric ID for a slug provides immutable addressing.
The REST contract does not provide an atomic repository-ID condition for every
comment, task, verdict or build-status write. A remote rename/reassignment between
the final identity lookup and the following request therefore remains an inherent
race. Use a PAT user whose repository permissions match the intended scope, and
coordinate repository renames with active review operations. Fixture tests prove
that already-reused slugs are rejected or resolved before a write; they do not
establish atomic protection against a concurrent server-side rename.

## Regression tests

Set `PRELOOP_DISABLE_TELEMETRY=true` for every test run. Backend fixtures cover DC
payloads, pagination, optimistic versions and network policy; console tests cover
PAT configuration and feature/version/capability display. The adapter does not
require a live PAT or instance for those tests.
