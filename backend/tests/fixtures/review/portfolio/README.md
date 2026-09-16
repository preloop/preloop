# Portfolio review fixtures

Synthetic fixtures for `backend/presets/017-portfolio-review.yaml` and
`backend/tests/test_portfolio_review_preset.py`. Nothing here comes from
a real deployment.

- `repos/<name>/` is a tiny repository holding several independently
  built projects, plus the directories discovery has to skip.
- `results/result-<scenario>.json` is the
  `preloop.review.portfolio/v1` result a run over that repository is
  contracted to produce.
- `schemas/portfolio-v1.json` is the JSON Schema for that envelope.

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

`repos/.gitignore` re-includes the `.venv`, `node_modules` and `build`
traps: the repository root ignores those names because a real one is a
local artifact, and a trap nobody checks in is a trap the walk is never
tested against.

Git facts (`last_commit_date`, `commits_12m`) are recorded in the result
fixtures rather than in the trees: the trees are checked-in directories,
not repositories with a history. Everything else a result claims about a
project (its path, stacks, manifests, declared runtimes, file count and
the five documentation and tooling booleans) is recomputed from the tree
by the test, so a fixture cannot quietly stop being true.
