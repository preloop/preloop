# Bitbucket Data Center 10.2 fixtures

Synthetic payloads used by `tests/sync/trackers/test_bitbucket_dc.py`. All
identifiers, hosts and users are invented (`bitbucket.example.com`, project
`PRJ`, repository `my-repo` id `42`, user `jdoe`).

## Provenance

Shapes were checked against the official versioned reference for
Bitbucket Data Center **10.2** (LTS):

| Source | Detail |
| --- | --- |
| https://developer.atlassian.com/server/bitbucket/rest/v1002/intro/ | Reference landing page; HTML title "The Bitbucket Data Center REST API", embeds `10.2.swagger.v3.json` |
| https://developer.atlassian.com/server/bitbucket/rest/v1002/api-group-pull-requests/ | Pull request group (create with `fromRef`/`toRef`, comments with `text`/`anchor`/`parent`, `participants/{userSlug}` status `APPROVED`/`NEEDS_WORK`/`UNAPPROVED`, `blocker-comments`, `.diff`, `activities`, `changes`) |
| [10.2 OpenAPI document](https://developer.atlassian.com/server/bitbucket/10.2.swagger.v3.json) (`info.version` = `10.2`, OpenAPI 3.0.1, 365 paths) | Verified schema definitions: `RestPullRequest`, `RestPullRequestRef`, `RestRepository`, `RestProject`, `RestComment`, `RestCommentThreadDiffAnchor`, `RestPullRequestParticipant`, `RestPullRequestAssignStatusRequest`, `RestBuildStatusSetRequest`, `RestBranch`, `RestErrorMessage`, paged envelope `{isLastPage, limit, nextPageStart, size, start, values}` |
| https://confluence.atlassian.com/bitbucketserver/personal-access-tokens-939515499.html | Personal access tokens are sent as `Authorization: Bearer <token>` to `/rest/api/latest/...` |

Notes recorded while verifying:

* The spec lists paths under `/rest/api/latest`; `/rest/api/1.0` is the stable
  alias the issue asks for and is what the adapter sends.
* `POST .../approve` and `DELETE .../approve` still exist in 10.2 but are
  marked deprecated since 4.2 in favour of `PUT .../participants/{userSlug}`.
  The adapter uses the participants resource for all verdicts.
* `RestComment.anchor.multilineMarker` is marked `readOnly` in the 10.2
  schema. Ranged-anchor creation is explicitly unsupported. Multiline text,
  single-line anchors, file comments and replies use documented writable shapes.
* The 10.2 OpenAPI exposes no "current user" resource. The reviewer slug for
  `participants/{userSlug}` comes from `connection_details.username`, or from
  an optional `X-AUSERNAME` response header hint (not part of the OpenAPI
  contract). When neither is available, reviewer operations fail explicitly.
* `commits/{commitId}/builds` `POST` answers `204` with no body (spec); the
  older `/rest/build-status/1.0` module is deprecated since 7.14 and unused.
* The historical 7.21 examples were not used as evidence.

Live validation against a running 10.2 instance remains with the release
owner; these fixtures establish the fixture-tested status only.

Verified OpenAPI SHA-256: `003639410aeb4209aad2dab67d98c44a7b70c2b8ff36d11f1bc63d603c4119ee`.

## Webhook payloads (`webhooks_10.2.json`)

Synthetic deliveries for the webhook intake tests in
`tests/endpoints/test_webhooks_bitbucket_dc.py` and
`tests/sync/test_event_normalizer_bitbucket_dc.py`, one per event family
Preloop subscribes to, plus `diagnostics:ping` and an unsubscribed
`repo:forked`. Shapes follow the Bitbucket Data Center 10.2 "Event payload"
documentation (`eventKey`, `date`, `actor`, `pullRequest` with
`fromRef`/`toRef`, `previousFromHash`, `participant`/`previousStatus`,
`comment`/`commentParentId`, `repository`/`changes`). Deliveries carry
`X-Event-Key`, `X-Request-Id` and, when the hook has a secret,
`X-Hub-Signature: sha256=<hex>` over the raw body. Tests sign the exact
bytes they send; no live instance is contacted.
