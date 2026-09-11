# Reusing successful CI tests after a changelog update

After a suite passes on a PR, a follow-up commit changing only the regular root
`CHANGELOG.md` content can reuse that result. This avoids another set of database
shards, frontend tests, plugin tests, or Windows tests just to resolve a changelog
conflict. The CI summary links the actual prior run and explicitly labels reuse.

This is deliberately conservative. The effective checked-out merge tree must be
identical in every other tracked path, including tests, fixtures, workflows,
actions, helper scripts, lockfiles, configuration and file modes. A change on the
base branch is included in that comparison. Other Markdown files are included:
documentation and fixtures can be inputs to tests. Adding/removing CHANGELOG or
changing it to an executable/symlink also invalidates reuse.

| Follow-up or condition | Result |
| --- | --- |
| CHANGELOG content only, with verified recent suite success | Reuse that suite |
| Frontend code changes after backend passed | Rerun applicable suites; whole-tree equality is required |
| Base branch changes code, lockfiles, or workflow | Rerun applicable suites |
| README, documentation fixture, or shared configuration changes | Reuse is unavailable; normal full-PR path filters decide applicable suites |
| Earlier backend passed but an unrelated job failed | Backend can reuse; its eight shards and combined coverage must really have passed |
| One shard/test step failed, skipped, canceled, missing, or duplicated | Rerun that suite |
| Fork PR, missing/invalid API evidence, or result older than 24 hours | Rerun applicable suites |
| Push to main or a version tag | Fresh tests |
| Manual CI dispatch or “Re-run all jobs” | Fresh tests; manual dispatch does not publish images |

Lint, dependency resolution, advisory scanning and secret scanning retain their
existing fresh-run behavior. The Windows suite remains informational as before;
its explicit Build, Vet and Test steps must all have succeeded to reuse it.
`continue-on-error` at the job level does not establish test success.

## Evidence and trust

The planner checks at most the twenty most recent CI runs on the same PR head
branch, within 24 hours and a bounded lookup budget. It only accepts the same
repository and PR. It reads jobs from the exact historical run attempt. Backend
reuse requires all eight test shards, their actual `Run tests` steps, and the
combined coverage-enforcement step. Each other suite has an explicit required
job/step list in `scripts/ci/reuse_tests.py`.

Job names alone are not trusted: a different old workflow could fabricate a
successful-looking name. The planner first reads GitHub GraphQL
[`WorkflowRun.file`](https://docs.github.com/en/graphql/reference/objects#workflowrun),
which identifies the **executed workflow file** at an immutable source SHA. It
validates the run ID, exact repository and workflow path, fetches that source,
and requires byte equality with the current CI workflow. Only then does it trust
the fixed GitHub-evaluated merge-SHA marker and successful checkout-verification
step. Every reusable suite explicitly checks out that immutable merge SHA.

The planner executable is never taken from the PR merge tree. CI checks out
`scripts/ci/reuse_tests.py` from `${{ github.event.pull_request.base.sha }}`
(or the event commit on push/dispatch) into a separate directory and runs that
copy with isolated Python (`python3 -I`). Fork pull requests cannot skip
required suites by editing the planner: GitHub loads the workflow from the
base branch, and the planner bytes come from that same trusted ref. The
`scripts/**` path filter still forces suites on when helpers change, but it is
not the trust boundary — fabricated `reuse_*` outputs from an untrusted
planner would otherwise override those jobs. If the planner is absent on the
base (first introduction of this file), reuse is skipped and suites run fresh.

The planner independently fetches and fingerprints the actual historical merge
tree and compares it with the current checkout. PR head/base fields from old REST
run records are not used as historical commit evidence: those fields can reflect
the PR's current state. Caches and downloadable artifact markers never authorize
reuse. Malformed data, denied API access, missing objects or ambiguous evidence
cause fresh tests instead of assumed success.

GitHub force-updates `refs/pull/N/merge`, so an older merge SHA recorded in a
prior job name may no longer be fetchable. That fetch failure is fail-closed:
that candidate is skipped and, if no other reachable run proves the suite,
tests run fresh. The first reusable baseline after publication therefore needs a
still-reachable merge commit.

The required `CI` gate rejects an applicable skipped backend, coverage, frontend,
or plugins suite unless the planner provided verified reuse and a prior-run
link. A failed/canceled current job still fails the gate even when reuse
metadata exists. Keep `CI` as the required check; individual test jobs now show
the full input fingerprint and can legitimately skip when earlier successful
tests are reused.

`reuse_cli` / `evidence_cli` are computed and written to the changes-job summary
and the required-gate summary, but they do not gate `CI`. The Windows suite is
`continue-on-error` and is not a `build-and-push` dependency. If that job later
becomes required, add `require_tests cli …` to the gate.

## Force fresh and limitations

Use **Re-run all jobs**, manually dispatch the CI workflow, or apply the
`ci-force-fresh` label before triggering a new PR run. Merely adding the label
does not trigger a run. “Re-run failed jobs” can retain previously successful
planner outputs and does not guarantee fresh execution of all suites.

The first run after this workflow change has no matching prior evidence and runs
normally. Later identical-input runs can reuse it. This optimization must be
observed in hosted CI after publication; local tests cannot demonstrate a hosted
reuse event in advance.

The fingerprint describes repository-controlled inputs, not a hermetic machine
image. Hosted runner images, external services, and minor toolchain releases can
change within the 24-hour window. Main/tag/manual/full-rerun freshness and ongoing
security scans provide refresh paths. A reused result is a link to the original
successful run, not a claim that tests executed again on the latest commit.
