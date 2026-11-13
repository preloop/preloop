# Review and status posted by Preloop

Design note and threat model for `git_clone_config.platform_review`: after a
read-only review run finishes and its result passed the flow's result schema,
the control plane posts one pull request review and one commit status on the
commit that was reviewed. The run itself never holds a credential that can
write to GitHub.

User guide: [Restricted CI: Review and status posted by Preloop](../guide/flows/restricted-ci.md#review-and-status-posted-by-preloop).
Code: `backend/preloop/services/platform_review.py`. Tests:
`backend/tests/services/test_platform_review.py` (every test named below is in
that file).

## Route decision

Two routes were considered for "a shared review flow leaves a review and a
status on the pull request".

- **(a) The run stays read-only and the control plane posts.** Chosen.
- **(b) A write-capable shared flow** in which the agent holds a token scoped
  to review, comment and status.

Reasons for (a):

1. GitHub has no permission for "review and comment" that excludes "close,
   reopen and edit the pull request". Creating a review and updating a pull
   request (`PATCH /repos/{owner}/{repo}/pulls/{pull_number}`: state, title,
   body, base) both need `pull_requests: write`
   ([permissions required for GitHub Apps](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps)).
   Under (b) the model would hold a token that can close or rewrite a pull
   request. Under (a) that token exists only inside controller code that can
   send two write requests and nothing else.
2. Shared project selection exists only in read-only mode. Route (a) leaves
   that boundary alone. Route (b) needs a new shared write mode with its own
   cross-repository analysis.
3. The posted text is built by a fixed server template from validated
   fields. A prompt-injected model cannot choose the target, the action or
   free-form markup.
4. Publishing a finished review again needs no model run: it is the same
   controller step.
5. It is the least-privilege option.

Cost of (a): the model cannot reply in threads or react during the run.
Inline comments come only from structured findings in the result.

## Who acts, and with what

| Actor | Credential | Can write to GitHub |
| --- | --- | --- |
| Container, model, approval hook | read lease: `contents: read` on one repository | no |
| CI identity (the caller's CI key) | Preloop CI key | no; it can ask Preloop to publish its own execution |
| Control plane, publication step | publish lease, see below | one review, one commit status |

The publish lease is a GitHub App installation token minted for one
publication attempt:

- requested with `repositories: [<the bound repository>]` and
  `permissions: {"pull_requests": "write", "statuses": "write"}`. No
  `contents` permission is requested, so the token cannot push, merge
  (`PUT .../pulls/{n}/merge` needs `contents: write`) or read source;
- only what the attempt needs: `statuses: write` alone when no review is
  posted, `pull_requests: write` alone when the flow sets no status;
- accepted only when GitHub's response lists exactly that repository and
  exactly those permissions plus the implicit `metadata: read`
  (`mint_repository_lease`, `profile=`);
- minted after the execution is terminal, never placed in an execution
  context, an environment variable, a log line or an API response;
- used through one class (`_GitHub`) that refuses any request other than:
  `POST .../pulls/{n}/reviews`, `POST .../statuses/{sha}`, and reads of that
  pull request, its files, its reviews and that commit's statuses;
- revoked when the attempt ends, whatever the outcome. GitHub's one hour
  expiry is the fallback.

## What is posted

Only fields of the stored result, and only when all of these hold:

- the execution is `SUCCEEDED`;
- the result passed the schema gate when it was captured;
- the same stored result passes the flow's current `result_schema` again at
  publication time;
- `verdict` is one of the values the schema's enum admits and the flow maps
  it explicitly.

Fields read: `verdict`, `summary`, `findings[]` (`severity`, `title`, `body`
or `description` or `message` or `detail`, `path` or `file`, `line`) and
`checked[]` (or `checks[]`). Nothing else in the result is read. The template
adds the reviewed commit, a link to the execution built from server
configuration, and a hidden marker.

### Escaping rule

Every model-written string goes through `plain_text`:

1. A value that is not a string becomes empty.
2. Control, format, surrogate, private-use and unassigned characters are
   dropped. This removes bidirectional overrides and zero-width characters.
3. The text is cut to the field's cap.
4. `&`, `<`, `>` become HTML entities: no tag, comment or character
   reference survives.
5. Every other ASCII punctuation character is backslash-escaped: no link,
   image, emphasis, heading, code span, table, list or quote, and no line
   that starts with `/`.
6. A zero-width space is inserted after `@` and `#`, between a URL scheme's
   `:` and `//`, inside `www.` and inside `GH-<digit>`: no mention, no issue
   or pull request reference (so no closing-keyword target and no
   cross-reference on another issue), no bare-URL autolink.

Multi-line fields are emitted as block quotes, so no model line starts at
column zero.

Caps: body 60,000 characters, summary 4,000, 50 findings in the body (the
rest are counted, with a pointer to the execution), title 200, finding body
2,000, 20 inline comments, 30 checked items of 200.

### Inline comments

A finding becomes an inline comment only when its `path` equals a file name
in the pull request's file list and its `line` is a line of the new side
inside a diff hunk. Every finding is also in the body, so a rejected inline
position costs nothing: if GitHub answers 422 to a review with inline
comments, the same review is sent once more without them.

## Where

Repository, pull request number and head SHA come from the execution's own
`ci_review_binding`, written by the controller when the trigger was accepted.
They never come from the result, the prompt, the flow text or the caller: the
publish endpoint has no request body.

Before any write the step re-runs the dispatch authorization of the
execution's own identity (`authorize_dispatch`): the identity is active, its
project is still on the flow's allowlist with the same tracker, and its
provisioning snapshot still matches the flow. It then reads the pull request
and requires the provider's pull request id, repository id and repository
name to equal the binding.

In a shared flow, execution A cannot post on project B: A's binding names A's
project, the lease is minted for A's repository only, and GitHub enforces the
repository on the token.

Pinned extra read repositories (a separate feature) are read leases. The
publish lease is only ever minted for the bound repository.

## Exact commit

The review is created with `commit_id` equal to the bound head. The status is
created on that SHA.

**Moved head: the review is skipped and the status is still set on the
reviewed SHA.** The status is true for that commit and GitHub evaluates
required statuses on the current head, so it cannot make a newer commit look
reviewed. A review on a commit that is no longer the head would be read as
feedback on the current code, so none is posted
(`review_reason: head_moved`). A closed or merged pull request also gets no
review (`pull_request_closed`). A verdict is never attached to a commit that
was not reviewed.

## Status semantics

- `status.context`: default `preloop/review`. Keep it stable so a branch
  protection rule can require it.
- `status.verdict_states`: explicit state (`success`, `failure`, `error`) for
  every verdict in the schema's enum. Saving the flow fails if one is
  missing.
- `status.incomplete_state` (`error` or `failure`, optional): set with the
  neutral description "Review did not complete; no verdict for this commit"
  when the run stopped, failed or returned no valid result, so a required
  status is not left waiting forever. Without it nothing is posted for such
  runs.
- No `pending` status is set at trigger time. All writes stay on the
  completion path; a required context that has not reported shows as
  expected in GitHub.
- The description is fixed text plus the verdict from the enum; the target
  URL is the execution in the console.

Commit statuses were chosen over check runs: `statuses: write` is narrower
than `checks: write`, and a status is what a branch protection rule requires
by context name.

## Review action

`COMMENT` unless `verdict_actions` maps the verdict to `approve` or
`request_changes`. Nothing implies an approval.

## Idempotency and replay

One row per execution in `platform_review_publication` (unique on
`execution_id`). An attempt first claims the row with a single statement
(insert, or a conditional update of a failed or abandoned row), so two
workers never both publish. Each provider write is recorded as soon as GitHub
confirms it and is never repeated by a later attempt.

If a worker dies between GitHub accepting the review and the row being
updated, the next attempt lists the pull request's reviews and adopts the one
that carries this row's random nonce, the bound commit and a bot author. The
nonce is unknown to anyone before the first post, so a stranger cannot plant
a review that suppresses ours.

After each write the step reads it back from GitHub and compares actor (an
App bot), target (commit, context) and content (body digest, state). A
mismatch is recorded as `readback_mismatch` and is never cleared by a retry:
a person has to look at the pull request.

## Failure isolation

A posting failure never changes the execution's status or stored result. It
is recorded on the publication row with a stable reason and is visible on the
execution, its result and the publish endpoint. Retryable failures (GitHub
unavailable, rate limits) are retried by the execution monitor with
exponential backoff (1, 2, 4, ... minutes, capped at one hour) for at most
six attempts. The same sweep publishes finished executions that no worker
claimed, for example after an operator stop or a worker crash. After that,
or for a non-retryable reason, an explicit publish call retries.

## Opt-in and identities

`platform_review` is off by default and accepted only with
`publication_mode: read_only`; every other publication key stays rejected in
that mode. Saving the opt-in also requires `validate_result: true`, a
`result_schema` with a required string `verdict` enum, and a GitHub App
tracker for every repository (a personal access token cannot be limited to
one repository and two permissions).

A CI identity records, when it is provisioned, which provider writes its flow
can cause (`repository_binding.platform_review`, for example
`["review:comment", "status"]`), next to `publication_mode` and
`ci_project_selection`. Authentication compares that snapshot with the flow.
Turning the opt-in on, adding a status, or adding `approve` or
`request_changes` therefore stops existing identities from authenticating
until an administrator provisions new ones. An identity is never silently
upgraded to a flow that writes, and a run triggered before the change is not
posted.

## Threats and tests

| # | Threat | Control | Test |
| --- | --- | --- | --- |
| 1 | The run obtains a write credential | publish lease minted only after the run is terminal, in the control plane; container context holds the read lease only | `test_container_context_of_an_opted_in_flow_has_no_write_credential` |
| 2 | The lease can push, merge or reach another repository | one repository, no `contents`; GitHub's returned scope must match exactly | `test_publish_lease_is_one_repository_two_permissions_and_revoked`, `test_broader_permission_set_returned_by_github_is_refused`, `test_no_publish_profile_carries_contents`, `test_lease_for_one_repository_cannot_write_to_the_other` |
| 3 | The lease is used for another call (close, edit, merge) | fixed request allowlist; the tests assert the complete list of writes | `test_valid_result_posts_one_review_and_one_status_on_the_bound_commit` |
| 4 | A prompt-injected result mentions people, links out, closes issues, hides HTML or commands a bot | fixed template, `plain_text`, caps | `test_plain_text_neutralises_mentions_links_html_and_commands`, `test_rendered_review_contains_no_model_markup_and_respects_caps` |
| 5 | The result names another pull request, repository or commit | target only from the binding; unknown result fields are not read | `test_result_naming_another_pull_request_is_ignored`, `test_publish_request_cannot_name_a_target` |
| 6 | Shared flow: project A's run or key posts on, or reads, project B | binding, per-repository lease, principal-owned queries | `test_two_projects_on_one_shared_flow_never_cross`, `test_ci_identity_cannot_publish_another_principals_execution` |
| 7 | A verdict lands on a commit that was not reviewed | `commit_id` and status SHA from the binding; moved head skips the review | `test_moved_head_skips_the_review_and_keeps_the_status_on_the_reviewed_sha`, `test_closed_pull_request_gets_no_review`, `test_pull_request_identity_mismatch_is_refused` |
| 8 | An invalid, stopped or failed run posts a verdict | plan requires SUCCEEDED, stored validity and re-validation | `test_invalid_stopped_and_failed_runs_post_no_verdict`, `test_stored_result_that_no_longer_matches_the_schema_is_not_posted`, `test_incomplete_run_posts_nothing_without_incomplete_state` |
| 9 | Retries, restarts or republish calls duplicate the review | claim row, recorded writes, nonce adoption | `test_duplicate_delivery_and_republish_post_once`, `test_live_claim_blocks_a_concurrent_attempt`, `test_lost_response_after_posting_is_adopted_not_reposted`, `test_review_with_a_foreign_marker_is_not_adopted` |
| 10 | What GitHub holds differs from what was sent | read-back of actor, target and content | `test_read_back_mismatch_is_recorded_and_never_cleared_by_a_retry` |
| 11 | A posting failure corrupts or hides the review result | isolation, bounded retry, explicit recovery | `test_posting_failure_leaves_status_and_result_untouched_then_recovers`, `test_automatic_retries_are_bounded`, `test_unexpected_error_during_publication_is_recorded_not_raised`, `test_completion_path_swallows_a_publication_crash` |
| 12 | An existing identity is silently upgraded to a flow that writes | provisioning snapshot | `test_identity_snapshot_records_the_opt_in_and_breaks_when_it_changes`, `test_identity_provisioned_before_the_opt_in_cannot_cause_a_post` |
| 13 | A CI key turns the opt-in on, or publishes without the action | flow routes deny CI keys; `execution:review:publish` | `test_ci_identity_cannot_enable_the_opt_in`, `test_publish_needs_the_explicit_action` |
| 14 | The opt-in is used with a credential that cannot be narrowed | save-time and publish-time tracker check | `test_opt_in_is_rejected_for_personal_token_and_non_github_trackers`, `test_opt_in_is_accepted_in_read_only_mode_only` |
| 15 | Access continues after admission is removed | dispatch authorization re-run before every attempt | `test_project_removed_from_the_flow_revokes_posting`, `test_disabled_identity_revokes_posting` |
| 16 | An approval is posted without anyone asking for it | explicit `verdict_actions` only | `test_approve_and_request_changes_only_by_explicit_mapping` |
| 17 | The App lacks `statuses: write` and the status is silently dropped | review-only lease, recorded reason | `test_missing_statuses_permission_posts_the_review_and_says_why` |
| 18 | With the opt-in off anything changes | no claim, no lease, no call | `test_opt_in_off_mints_no_lease_and_makes_no_call`, `test_completion_path_does_nothing_without_the_opt_in_or_a_ci_binding` |

## GitHub App permissions

The App installation that owns the repository needs, under repository
permissions:

- **Pull requests: Read and write** (create the review; read the pull
  request, its files and its reviews).
- **Commit statuses: Read and write** (create the status and read it back).
  Only needed when the flow sets `status`.

**Checks** is not used. **Contents** is not requested by the publish lease
(the read-only clone keeps using `contents: read`).

An installation token cannot exceed what the installation was granted. If
the App does not hold **Commit statuses**, GitHub refuses the combined lease;
Preloop then mints a review-only lease, posts the review and leaves the
publication `failed` with `reason: statuses_permission_missing` (the review
is recorded as posted). An organisation owner has to approve the added
permission on the App installation; a publish call then adds the status
without posting the review again.

## Not covered

- GitLab and Bitbucket. The opt-in is refused for them.
- Threaded replies, reactions, re-requesting review, dismissing an earlier
  review, or updating a previous review in place.
- A `pending` status at trigger time.
- Check runs and annotations.
- Verified against GitHub's documentation and a fake GitHub in tests, not
  against a live repository: that GitHub echoes a review body byte for byte
  (the read-back compares a digest after normalising line endings), that
  reading a pull request works with `pull_requests` permission alone, and
  that a lease request beyond the installation's permissions answers 422.
