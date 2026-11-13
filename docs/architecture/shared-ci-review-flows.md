# Shared review flows for restricted CI

Design note and threat model for issue #2103: one hosted read-only review flow
serving several projects, while every restricted CI identity stays bound to one
project.

## Summary

- A CI identity is bound to exactly one project and one flow. That has not
  changed. A grant cannot name two projects.
- The flow is either **dedicated** (one project, one repository) or **shared**:
  a read-only review flow (`git_clone_config.publication_mode: read_only`) with
  `ci_project_selection: true`, whose trigger projects and clone repositories
  list the admitted projects one to one. This allowlist is the only thing that
  makes a flow shared.
- The selection mechanism has been in the core since 1.0.0. This change makes
  it a supported, administered feature: a human-only allowlist API
  (`GET`/`PUT /api/v1/ci-identities/shared-flows/{flow_id}`), CLI commands
  (`preloop ci shared-flow`, `preloop ci share-flow`), console support, truthful
  capability text, an operator check script and tests for every boundary below.
- No schema change and no migration. An existing one-project identity is
  already "an allowlist of one": its flow stays dedicated and nothing about it
  is rewritten.

## What "organisation" means

Preloop's isolation boundary is the **account**. A tracker organisation (a
GitHub organisation or GitLab group) is a record inside one account and is not
an access boundary. Every query that resolves a CI binding filters the flow by
`flow.account_id` and the project by its tracker's `account_id`, so a flow can
only be shared between projects of its own account. To keep groups of
repositories apart, either use one account per group (a hard boundary) or one
shared flow per group inside one account (each flow admits only its own
projects, and each identity still reaches one project). Sub-accounts are not
used for this.

## Trust boundaries

| Party | Controls | Does not control |
| --- | --- | --- |
| CI caller (holds a `ci_` token) | `pr_number`, `head_sha` | project, repository, clone source, base revision, flow, model, prompt, sandbox, setup commands, result schema |
| Account administrator (human; owner in the open-source edition, `manage_ci_identities` in Enterprise) | which projects a flow admits, which identities exist, their actions and keys | another account's flows or projects |
| Preloop control plane | repository and revision selection, lease minting, execution ownership | |
| Review container (runs repository code) | its own workspace and result | other executions, the flow configuration, any credential but its own read lease |

Preloop reads the pull request from the provider by the bound project's
immutable repository id, requires its head to equal `head_sha`, builds the
event server-side and resolves the clone to that one repository at that commit.

## How an execution's project is resolved

Code that acts on a restricted CI execution, including any later feature that
posts a review or a commit status, must take the target from the execution's
stored binding and from nowhere else:

- `flow_execution.ci_review_binding` is written once, in the insert that
  creates the execution, from the identity's own project. It holds
  `project_id`, `tracker_id`, `repository_identifier` (the provider's immutable
  repository id), `repository_slug`, `pr_number`, `head_sha`, `base_branch`
  and `base_sha`. `crud_ci_execution.authorize_dispatch` returns it after
  re-checking the principal, the grant and the allowlist; it raises when the
  authority is gone.
- The trigger event is built from that binding and carries the same
  `project_id`, so the orchestrator's `_resolve_trigger_project_id` returns the
  bound project for every position in the allowlist.
- Never use `flow.trigger_project_ids`, the order of
  `git_clone_config.repositories`, the agent's result or any caller input to
  choose a repository on a shared flow. On a shared flow the first listed
  project is somebody else's repository.

## Should a flow that publishes be shareable?

No. Shared selection stays limited to `publication_mode: read_only`, and
`_binding` and the allowlist API both enforce that.

- In legacy publication mode the review container receives the tracker's own
  credential, which is not limited to one repository. On a shared flow a run
  for one project would hold a credential that can write to its siblings.
- Isolated publication mints a repository-scoped writer lease, but its purpose
  is pushing branches and opening pull requests. That is more authority than a
  review needs, and it is bound to a single-repository flow.
- A review that should leave a comment and a commit status does not need a
  write credential inside the container. The control plane can post the
  validated result itself, with a token minted for the one repository in
  `ci_review_binding`, limited to pull request and status writes, on exactly
  `head_sha`. The container stays read-only and sharing stays safe.

So the route for "one shared flow that also writes reviews and statuses" is a
read-only shared flow plus posting by the platform, not a write-enabled shared
flow.

## Threats

Tests are in `backend/tests/api/test_ci_shared_flow.py` unless another file is
named.

| # | Threat | Outcome | Test |
| --- | --- | --- | --- |
| T1 | Confused deputy: identity A makes the shared flow review project B | The trigger body has no project or repository field (422 on any extra field). The provider is addressed by A's repository id, and an answer naming another repository is refused with 403 | `test_shared_trigger_accepts_only_pr_number_and_head`, `test_identity_cannot_trigger_for_another_repository` (sibling, unapproved and other-account repository) |
| T2 | IDOR: A reads, lists or stops B's execution, result, startup receipt or tool evidence by id | 404, identical to an id that never existed; the list and its count are filtered by principal before pagination | `test_sibling_execution_is_not_found_for_the_other_identity`, `test_each_identity_lists_and_counts_only_its_own_executions` |
| T3 | Enumeration: A learns that sibling projects exist | No machine route returns the flow, its allowlist or other identities; A's responses never contain a sibling id or slug | `test_identity_cannot_read_the_allowlist_or_another_flow`, `test_each_identity_lists_and_counts_only_its_own_executions` |
| T4 | Caller overrides clone, setup, model, prompt, sandbox or result schema | 422 before any execution is created | `test_shared_trigger_accepts_only_pr_number_and_head`, `backend/tests/api/test_ci_execution.py::test_review_request_rejects_every_override` |
| T5 | Wrong repository picked for the second project on the list (first-project fallback) | The trigger project resolves from the execution's binding | `test_trigger_project_resolves_from_the_binding_not_the_first_listed` |
| T6 | Concurrent runs share workspace input, lease or configuration | Each run gets its own context with one repository, its commit and a read lease for that repository only; the flow row is not written | `test_concurrent_runs_get_separate_repository_revision_and_lease`, `test_two_projects_one_flow_end_to_end` |
| T7 | Allowlist change races an in-flight run | The allowlist write locks the flow row. Authority is re-read at every step: a pending run of a removed project is refused at dispatch and gets no lease; reads by its identity are denied from the next request. A run that already holds a lease finishes on it (the lease is repository-scoped, read-only and expires in about an hour) | `test_removing_a_project_denies_its_identity_and_pending_run_only`, `test_removed_project_gets_no_lease_for_an_accepted_run` |
| T8 | Revoking, expiring or retiring one identity affects a sibling, or fails to stop the first | Per identity only | `test_revocation_expiry_and_retire_affect_one_identity_only` |
| T9 | A dedicated identity silently becomes shared (or the reverse) | The binding mode is part of the identity's stored snapshot, so a flip denies the identity. The allowlist API refuses to share a dedicated flow that still has identities | `test_existing_dedicated_identity_is_unchanged_and_cannot_be_flipped`, `test_hand_edited_mode_flip_denies_a_dedicated_identity` |
| T10 | Cross-account sharing | The other account gets 404 for the flow. Admitting a foreign project is refused with the same answer as an unknown id. A hand-edited foreign entry binds nobody | `test_a_flow_cannot_be_shared_across_accounts`, `test_hand_edited_cross_account_entry_grants_nothing` |
| T11 | A CI credential or an unprivileged human edits the allowlist | 403, audited as a denied `share_flow` | `test_allowlist_administration_is_human_owner_only` |
| T12 | Allowlist request smuggles a repository URL, tracker or wildcard | 422; Preloop derives the repository and tracker from each project | `test_allowlist_request_shape_fails_closed`, `test_admin_shares_a_flow_and_each_identity_keeps_one_project` |
| T13 | A write-capable flow is shared | Only an enabled read-only review flow can be shared (409) | `test_only_a_read_only_review_flow_can_be_shared`, `backend/tests/services/test_read_only_review.py::test_shared_selector_cannot_enable_write_capability` |
| T14 | A project outside the allowlist gets an identity on the flow | Refused at preview and at provisioning | `test_project_outside_the_allowlist_cannot_get_an_identity` |
| T18 | A project whose repository cannot be scoped (not a GitHub App tracker, inactive project, organisation or tracker, no clone slug) is admitted | 409, flow unchanged | `test_project_that_cannot_be_scoped_is_refused_from_the_allowlist`, `test_inactive_tracker_refuses_every_project_on_it` |
| T15 | Moving a repository to a shared flow interrupts or cross-wires its reviews | A new identity on the shared flow runs next to the dedicated one; each sees only its own executions; retiring the old one leaves the new one working | `test_project_moves_to_a_shared_flow_while_its_dedicated_review_keeps_working` |
| T16 | A stale head is reviewed | The requested head must equal the pull request's current head at admission and again at dispatch | `test_stale_head_is_refused_on_a_shared_flow`, `backend/tests/services/test_ci_execution_admission.py` |
| T17 | A completion callback tells one identity about a sibling's run | A subscription belongs to one principal and only carries that principal's executions; it stops when the project is removed | `test_completion_callback_is_delivered_to_the_owning_identity_only` |

## Two-repository acceptance walk-through

The sequence an operator would run against two repositories, and where each
step is covered:

| Step | Covered by |
| --- | --- |
| Admit two repositories to one flow, one project-bound identity each; record the selection mode and runner | `test_admin_shares_a_flow_and_each_identity_keeps_one_project` (`binding_mode`, and `runner_pool` in the preview) |
| Review one exact head and base per repository, concurrently, without changing the shared flow | `test_two_projects_one_flow_end_to_end`, `test_concurrent_runs_get_separate_repository_revision_and_lease`, `test_own_result_and_receipt_are_readable_on_a_shared_flow` |
| Read the complete validated result | `test_own_result_and_receipt_are_readable_on_a_shared_flow`, `backend/tests/api/test_ci_execution_http.py` |
| A's trigger cannot select B, an unapproved repository or another account; A cannot read B's evidence; removal denies access | T1, T2, T7, T10 |
| Authenticated completion without polling; stale head refused | T16, T17 |
| Live check with two real tokens | `scripts/verify_restricted_ci.py shared-flow` (`test_operator_check_passes_against_two_identities_on_one_flow`) |

Not covered here, by design: posting the review and a commit status (see
"Should a flow that publishes be shareable?"), retrying only the publication
of a finished review, and several repositories inside one run (see the last
section). A CI identity also cannot read the flow's model or a configuration
revision; an administrator reads those from the flow itself.

## What an administrator can and cannot delegate

- Can: admit or remove projects of their own account on a read-only review
  flow; provision one identity per project; narrow, rotate, revoke, retire.
- Cannot: give one identity two projects; share a flow that can publish; admit
  a project of another account; let a CI credential manage the allowlist.
- Admitting a project grants nothing until an identity is provisioned for it.
  Removing a project is always possible and takes effect at the next
  authorization of every identity bound to it.
- Anyone who may edit the flow can still change its prompt, model and the
  underlying clone settings; that is the existing flow-edit authority, and it
  can only narrow or break a CI binding, never widen an identity to a second
  project. In Preloop Enterprise the allowlist API requires the
  `manage_ci_identities` permission; no Enterprise change is needed.

## Residual risks

- Everything on a shared flow uses the same prompt, model, limits and setup
  commands. A flow editor changes them for every admitted project at once.
- Execution capacity and budgets are per flow and account, so one project's
  runs can delay another's. That is an availability effect, not a data path.
- The review container runs repository code. The startup receipt is the
  platform's assertion, as for a dedicated flow.

## Not included: extra read-only context repositories

A review that needs several repositories is designed here and **not shipped**.
Today a read-only review clones exactly one repository; a second one cannot be
admitted, implicitly or explicitly, and the tests above assert that.

Planned shape:

- Per (flow, project), on that project's repository entry:
  `context_repositories: [{"project_id": ..., "commit": "<full SHA>"}]`,
  default empty, written only through the allowlist API by an administrator.
- Each entry must be an active project of the same account on a GitHub App
  tracker. No URL, no account-wide scope.
- At launch Preloop mints one read lease per repository, clones each at its
  pinned commit into its own directory, verifies each `HEAD`, and lists each in
  the startup receipt. Leases are revoked together.
- The context list becomes part of the identity's stored snapshot, so widening
  it denies existing identities until they are re-provisioned.

It is left out because the hosted executor, the private-runner checkout plan,
the startup receipt and lease revocation each assume one repository. Doing it
partially would be a half-enforced boundary. Two decisions are open: whether a
moving ref (a branch) may be admitted next to full SHA pins, and whether
widening should require re-provisioning or only an audit record.
