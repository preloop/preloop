# Forge reconciliation

The pull request record stores forge state independently of issue closure.
GitHub, GitLab and Bitbucket Cloud distinguish merged from closed without a
merge. Feedback subscriptions report `pr_merged` or `pr_closed`, respectively.
A merge is final even when a later webhook or an in-flight read is stale.

The refresh task runs every minute. Each tick claims at most 25 due records
and makes at most 25 API reads, including attempts through another tracker
and Bitbucket commit lookups. Provider authentication retries and redirects
have their own transport limits. Open records re-arm after five minutes;
records that did not fit the request budget retry on the next tick. Webhooks
accelerate known records, including reopened PRs. Copilot PRs use this same
record, refresh task and webhook delivery dedupe.

Legacy records without a known state are also refreshed, regardless of age.
Each batch reserves half its slots for due known-state records before filling
from the unobserved backlog, so backfill and webhook updates both progress.
Unobserved records ignore stored ETags until a full response supplies state.

A `304` changes only refresh bookkeeping. A `429` defers the remaining
claimed records on that forge according to `Retry-After`, with a one-minute
floor and one-hour ceiling. This also applies when Bitbucket throttles the
commit lookup used to expand an abbreviated head SHA.
If that lookup fails for another reason, the row retries after five minutes
before caching the new ETag or recording a terminal state. This prevents a
later 304, or a terminal row leaving the schedule, from hiding the changed head.

Ingress claims a forge delivery ID before queue publication. A published
claim rejects redelivery; an unpublished claim older than two minutes can be
taken over. Publication and release match the original claim timestamp, so
a late publisher cannot delete or acknowledge a newer holder's claim.

## Bitbucket Cloud conditional reads

The [Get a pull request API reference](https://developer.atlassian.com/cloud/bitbucket/rest/api-group-pullrequests/#api-repositories-workspace-repo-slug-pullrequests-pull-request-id-get)
and its [OpenAPI definition](https://dac-static.atlassian.com/cloud/bitbucket/swagger.v3.json)
were checked on 2026-10-26. For
`GET /repositories/{workspace}/{repo_slug}/pullrequests/{pull_request_id}`,
the definition lists 200, 401 and 404 responses, with no ETag, Last-Modified,
conditional-request parameters or 304 contract. The documentation explicitly
describes ETags on file reads and 304 on pipeline logs, so those endpoints'
capabilities should not be assumed for PR reads.

An anonymous read of a public PR also returned 200 without ETag or
Last-Modified. This is evidence of the fallback being necessary, not a
guarantee about every Bitbucket deployment. Preloop sends `If-None-Match`
only when a preceding response supplied an ETag. Without a validator, it
explicitly falls back to plain reads. A 200 without an ETag clears an older
stored validator. It never derives `If-Modified-Since` from `updated_on` or
sends `If-None-Match: *`: the latter tests existence and can hide changes.
If Bitbucket starts supplying validators, the existing conditional path
accepts them and handles 304 without a provider change.

## Acceptance evidence for #2017

Paths below are relative to `backend/tests/`. Each row is covered; the extra
cases close gaps in the original #2045 tests.

| Acceptance | Test evidence |
| --- | --- |
| Merged vs closed on all three forges | `services/test_forge_reconciliation.py::test_each_forge_reports_merged_apart_from_closed` (GitHub, GitLab, Bitbucket merged/declined/superseded cases) |
| 304 on all three forges | `services/test_forge_reconciliation.py::test_github_conditional_read_sends_etag_and_reports_304`, `test_gitlab_conditional_read_sends_etag_and_reports_304`, `test_bitbucket_conditional_read_sends_etag_and_reports_304` |
| 429 on all three forges | `services/test_forge_reconciliation.py::test_github_throttling_carries_the_advised_wait`, `test_gitlab_throttling_carries_the_advised_wait`, `test_bitbucket_throttling_carries_the_advised_wait` |
| Every `decide()` stop reason, unchanged except the split | `services/test_forge_reconciliation.py::test_decide_golden_table`, `test_golden_table_covers_every_stop_reason_decide_returns`; includes computed timeout exhaustion and provider gate reasons |
| 50 synthetic PRs stay within the bound | `models/crud/test_forge_reconciliation.py::test_fifty_open_pull_requests_stay_within_the_bound`, `test_fifty_prs_bound_includes_fallback_and_commit_reads` (all three forges) |
| Dedupe and ordering | `models/crud/test_forge_reconciliation.py::test_ingress_queues_a_forge_delivery_once_and_releases_on_failure`, `test_an_unpublished_claim_is_taken_over_after_the_grace_period`, `test_late_publisher_cannot_mutate_a_taken_over_claim`, `test_merge_webhook_during_refresh_emits_only_once`, `test_existing_webhook_merge_survives_a_stale_refresh`, `test_a_webhook_mark_during_the_read_survives_the_record` |
| Bitbucket capability fallback | `services/test_forge_reconciliation.py::test_bitbucket_pr_without_validators_degrades_to_plain_reads`, `test_bitbucket_uses_an_etag_only_when_the_server_supplies_one`; `models/crud/test_forge_reconciliation.py::test_plain_200_discards_an_obsolete_validator`, `test_failed_bitbucket_head_lookup_retries_before_caching_validator` |
| Copilot shares the bound and dedupe | `services/test_copilot_pull_requests.py::test_copilot_webhooks_share_dedupe_worker_and_refresh_bound` (50 webhook-created records) |

The original issue's `expected_artifact` field was removed from the revised
Delivery Report design, as recorded in #2045. It is not needed by the
reconciliation acceptance criteria and is not introduced here.
