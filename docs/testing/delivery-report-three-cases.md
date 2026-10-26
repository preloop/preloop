# Delivery Report milestone 1 acceptance

`backend/tests/services/test_delivery_report_three_cases.py` exercises a week of
synthetic work through `FlowExecutionOrchestrator.run()`, PostgreSQL, the console
API and the owner email transport. It is part of #2021 and #2025, not completion of the
four-week pilot gate.

The seed reuses the report JSON fixtures, the distribution test's project setup,
the console test's flow setup and the actions test's tracker fake. It contains
four merges in the fixed reporting window, an older merge, an aged open PR,
two closed issues and an open issue. A stored merge audit records an export
criterion that was not met. A separate search change has no tests. The fake
forge supplies reviews by two members on different areas.

Only forge/tracker, SMTP and NATS transports are replaced. The policy, fetch,
CRUD, report assembly, audit reuse, persistence, distribution and action routes
are production code. An agent launch fails the fixture, and socket connections
are forbidden during the scenario and HTTP assertions. The database connection
uses the existing PostgreSQL fixture. No models are called and no emails are
sent. The first-run assessed pass is explicitly disabled; tracker write policy
retains its defaults (proposals need a click).

## Run locally

Use a dedicated, migrated PostgreSQL database, separate from any demo database.
With the repository dev dependencies installed in `.venv`:

```sh
export PRELOOP_DISABLE_TELEMETRY=true
export PYTHONPATH="$PWD/backend"
export DATABASE_URL=postgresql+psycopg://localhost/preloop_delivery_acceptance
createdb preloop_delivery_acceptance
(cd backend/preloop/models && "$OLDPWD/.venv/bin/alembic" upgrade head)
.venv/bin/pytest backend/tests/services/test_delivery_report_three_cases.py -q -rx
```

The tests use the shared transaction rollback fixture. They never insert
proposals or member sections into the generated report. `seed_week` and
`run_week` are also reusable by a local console demo: persist their result in a
separate database, serve the normal application and use the resulting execution
ID at `/console/delivery-reports/{id}`. The captured SMTP body is available in
`Scenario.messages[0][3]` without re-rendering or editing the report.

## What passes

The real report reaches both console endpoints, retains the audit's unmet
criterion and file-based risk evidence, and sends the owner exactly one email
with one full-report link. Duplicate distribution is suppressed. The email
also has PR links and the required opt-out link; “one link” here means one
full-report link, not one total HTML anchor.

1. The export issue closed by `Fixes #12` has a partial audit with its unmet
   date-filter criterion. The deterministic report generates a reopen proposal
   pinned to the audit's merge revision. One HTTP action reopens it through the
   fake tracker and posts the criterion, revision, evidence and report link.
2. The merged search change has `hot_path_without_tests`, citing
   `backend/app/search.py`. Its generated follow-up becomes a labelled
   `preloop:proposed` issue when Open issue invokes the action endpoint.
3. The OSS owner email has the correct report link and reaches only the owner.
   The member-relevance part remains an explicit xfail, described below.

Dependency-bot and docs-only changes remain metadata-only and generate no
follow-up proposals. Generated proposals also appear in the owner email and
stored Markdown report.

The default report run makes no tracker writes. Duplicate clicks cannot repeat
an applied action. Editing the issue or changing the closing PR head after the
report makes a reopen stale and returns 409 before any tracker call. Additional
checks reject covered audits, evidence gaps without a demonstrated miss,
missing evidence, unrelated closing PRs, mismatched revisions, missing heads,
changed criteria and edits after the audit.

The PR ledger records the source head, which is different from the merge SHA.
When the ledger does not supply a merge SHA, proposal generation uses the
controller-observed PR URL and merge SHA in the stored audit's `merges` envelope.
It does not substitute a source head or borrow an unrelated audit revision.

## EE boundary

Per-member relevance and short updates belong to EE. OSS
`services/delivery_report_email.py:render_email` produces an account-wide owner
digest, so it includes the frontend change even though the owner did not review
that area. This test does not claim to validate EE delivery.

The case-3 test has a strict xfail restricted to a dedicated exception for the
known missing behavior. Fixture failures and unrelated assertions still fail
normally. Remove it when the engagement hand-off is available:

- `services/delivery_report_facts.py:details_from_forge` does not retain reviews.
- `models/crud/delivery_report_policy.py:pull_request_facts` does not expose review
  engagement.
- `services/delivery_report.py:assemble_report` emits no per-person selection.

The fake forge supplies distinct backend and frontend reviewers, but the report
cannot select only the owner's reviewed areas. The desired `per_person`
assertion documents the missing hand-off, not an existing OSS schema. No
engagement or review-fact production changes are included here.
