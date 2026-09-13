# Preloop repository issue readiness policy

This policy names the labels used to assess issues in `preloop/preloop`.
It is project policy, not a label taxonomy imposed by the generic triage preset.
Triage updates the issue assessment and applies its complexity label through the
scoped provider tool. Readiness and risk remain assessments in the issue body;
this repository also uses the labels below for maintainer-led reviews. Priority
and component labels remain independent.

The generic triage preset reuses an existing project complexity scheme. It creates
the standard complexity family below only when none exists in a complete catalogue.
Ambiguous schemes or missing evidence remain explicit; they do not authorize a
new competing taxonomy.

## Assessment labels

Apply at most one label from each family, preserving unrelated human labels.
Unknown evidence remains explicit in the assessment; do not
force an unsupported low estimate merely to fill a label family.

| Family | Values and meaning |
| --- | --- |
| Complexity | `complexity:low`: localized, known approach and decisive tests. `complexity:medium`: interacting components or nontrivial edge cases. `complexity:high`: architecture, migration, concurrency, trust boundaries or broad unresolved acceptance. |
| Readiness | `readiness:ready`: bounded remaining behavior and validation, no unresolved design/dependency. `readiness:needs-spec`: scope or design decisions missing. `readiness:blocked`: known dependency prevents pickup. `readiness:in-progress`: overlapping implementation underway. `readiness:needs-verification`: code has landed but acceptance needs reconciliation. |
| Risk | `risk:low`: limited, reversible impact. `risk:medium`: meaningful user or workflow impact requiring careful regression coverage. `risk:high`: security, data integrity, privileged publication, durable scheduling or difficult recovery. |

Description quality is a qualitative assessment with reasons, not a numerical
completeness score. Complexity describes the remaining implementation. When an
umbrella retains its historical feature estimate while acceptance is reconciled,
state `complexity_scope: historical_umbrella`; a short closure audit never turns
that issue into a low-complexity implementation candidate.

## Readiness assessment

Record the remaining behavior, actual code pointers, observable acceptance and
runnable validation appropriate to the change. Reconcile current source and known
PR activity so already-landed work and active overlapping implementation remain
separate from the remaining scope.

Missing design decisions use `readiness:needs-spec`; known blocking dependencies
use `readiness:blocked`; active overlapping implementation uses
`readiness:in-progress`; landed behavior awaiting acceptance evidence uses
`readiness:needs-verification`. Do not force an unknown field into one of these
categories without evidence. Readiness is independent of complexity and risk:
a well-specified high-complexity issue can be ready, while a small change can
still lack the information needed to implement it.

A new issue update, changed source baseline or new overlapping PR requires
refreshing the affected assessment. End users decide how to assign the issue;
this policy describes the work and does not select an implementation flow or model.

## Implementation and verification

An issue body should separate landed behavior, remaining work, active PRs and
acceptance still awaiting evidence. Link the source revision and relevant PR state
with the observation date. A PR title alone is not completion evidence.

Use the model/CRUD layer for backend persistence and Lit for frontend components.
Choose focused behavioral regressions and repository-required checks; security,
migration and cross-cutting changes can require broader suites. Run tests with
`PRELOOP_DISABLE_TELEMETRY=true`, and run changed-file pre-commit before committing.
Keep public examples generic. Preserve the configured trusted verification and
publication boundaries.
