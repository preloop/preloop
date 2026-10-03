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
additional permission policy. Webhook administration and repository administration
are not required by this adapter. This is a user PAT integration; managed OAuth
is a separate feature.

Discover projects and repositories, then select the desired scope. Project key,
numeric repository ID and repository slug are separate fields. The numeric ID is
stable across a repository rename; it is not a Cloud workspace identity. A repository ID or slug binding requires its project key. Credentials
are encrypted through the existing tracker SecretReference/CRUD path. They are
never part of public connection details or tracker responses. A stored credential
cannot be reused by discovery against another instance; create a new tracker when
changing instances.

DC hosts repositories and pull requests. Keep issue tracking in Jira. Merge,
decline, managed OAuth, webhook ingestion and execution/publication routing are
outside this integration. Unsupported operations report that limitation rather
than returning a success result. In particular, 10.2 marks `multilineMarker` as
read-only, so creation of ranged diff anchors is unsupported; multiline text,
single-line diff anchors and replies are supported. Returned clone URLs are metadata only and do not
authorize a connection or publication.

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
