# Portfolio review fixtures

Synthetic fixtures for `backend/presets/017-portfolio-review.yaml` and
`backend/tests/test_portfolio_review_preset.py`. Nothing here comes from
a real deployment.

- `repos/<name>/` is a tiny repository holding several independently
  built projects, plus the directories discovery has to skip.
- `results/result-<scenario>.json` is the
  `preloop.review.portfolio/v1` result a run over that repository is
  contracted to produce.
- `children/children-<scenario>.json` is the delegation side of the same
  run: the `run_flow` calls the parent made, the A2A delegation request
  each one carried, and the completion record of every child execution
  it got back. The files pair by name, `result-x.json` with
  `children-x.json`.
- `schemas/portfolio-v1.json` is the JSON Schema for that envelope. The
  child records validate against the frozen delegation schemas in
  `backend/schemas/delegation_request.schema.json` and
  `backend/schemas/delegation_task.schema.json`, which is what the tests
  assert: the fixtures are the platform's own shapes, not a restatement
  of them.

## The repositories

`repos/five-projects` holds exactly five projects, plus the traps a
walk has to refuse:

| path | what it is |
| --- | --- |
| `legacy/inventory-web` | stale, undocumented, end-of-life runtime: the highest triage band |
| `services/billing-api` | stale and unsupported, but documented and tested |
| `services/notifications` | maintained, supported runtime, no tests and no CI |
| `libs/shared-utils` | fresh, tested, no architecture note and no licence |
| `tools/report-cli` | fresh, documented, tested, licensed, and deliberately badly written |
| `build/Cargo.toml` | trap: a manifest in an excluded build directory |
| `libs/vendor/pom.xml` | trap: a vendored manifest |
| `.venv/pyproject.toml` | trap: a manifest in an excluded virtualenv |
| `node_modules/left-pad/package.json` | trap: an installed dependency |
| `services/notifications/node_modules/` | trap: an excluded directory inside a project, not counted in its file count |
| `services/notifications/ui-kit/package.json` | trap: a directory inside a discovered project |
| `platform/edge/gateway/proxy/go.mod` | trap: a manifest below the depth cap of 3 |
| `services/billing-api/sbom.cdx.json` | the only SBOM in the repository: the one project the security lens can be run against |

`services/billing-api` is the fixture behind the SBOM gate: it is the
only project shipping an SBOM, so every other project's security row is
`not_checkable` with the reason `no SBOM available` and buys exactly one
"add SBOM generation to this project's build" follow up. No child
execution is spent on a lens whose input is missing.

`tools/report-cli` is the fixture behind the triage rule: it is the
worst written project in the repository and it ranks last, because the
hint is computed from paths, manifests and git history and never from
the code.

`repos/two-projects` holds two projects, below the auto select threshold
of three, so a run over it asks no question at all.

## The scenarios

| result | what it exercises |
| --- | --- |
| `result-five-selected.json` | five discovered, one batched question with five rows, three reviewed, two follow ups approved |
| `result-two-auto-selected.json` | two discovered, below the threshold: no question, both reviewed, a clean portfolio |
| `result-first-question-expired.json` | the selection question expired: inventory only, zero reviews, zero issues, a completed run |
| `result-second-question-expired.json` | the follow up question expired: the full report lands with every candidate unapproved |
| `result-child-failed.json` | three children, one succeeded, one failed and one still running when the child wait expired: two projects uncovered, none of them healthy |
| `result-child-cap.json` | five projects and two lenses, ten planned calls against a child cap of 5: the projects the cap never reached are listed under coverage, one call is refused for budget, and the report still lands |
| `result-security-lenses.json` | two lenses including the security one, a fourth lens refused for being off the callable list, one project without an SBOM and one with |

`repos/.gitignore` re-includes the `.venv`, `node_modules` and `build`
traps: the repository root ignores those names because a real one is a
local artifact, and a trap nobody checks in is a trap the walk is never
tested against.

Child execution ids, states and costs live only in the `children/`
files, and the result fixtures repeat them on the lens rows. The tests
read the cost off the child record's `preloop.ai/cost` metadata and add
it up per project, so a report cannot quietly invent a cost or upgrade a
child's state.

Git facts (`last_commit_date`, `commits_12m`) are recorded in the result
fixtures rather than in the trees: the trees are checked-in directories,
not repositories with a history. Everything else a result claims about a
project (its path, stacks, manifests, declared runtimes, file count, its
SBOM paths and the five documentation and tooling booleans) is
recomputed from the tree
by the test, so a fixture cannot quietly stop being true.
