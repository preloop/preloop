# Preloop repository issue readiness policy

This policy names the labels used to assess issues in `preloop/preloop`.
It is project policy, not a label taxonomy imposed by the generic triage preset.
An authorized maintainer creates or updates labels; triage only proposes observed
existing labels. Priority and component labels remain independent.

## Assessment labels

Apply at most one label from each family, preserving unrelated human labels.
Unknown evidence remains explicit in the assessment and prevents dispatch; do not
force an unsupported low estimate merely to fill a label family.

| Family | Values and meaning |
| --- | --- |
| Complexity | `complexity:low`: localized, known approach and decisive tests. `complexity:medium`: interacting components or nontrivial edge cases. `complexity:high`: architecture, migration, concurrency, trust boundaries or broad unresolved acceptance. |
| Readiness | `readiness:ready`: bounded remaining behavior and validation, no unresolved design/dependency. `readiness:needs-spec`: scope or design decisions missing. `readiness:blocked`: known dependency prevents pickup. `readiness:in-progress`: overlapping implementation underway. `readiness:needs-verification`: code has landed but acceptance needs reconciliation. |
| Risk | `risk:low`: limited, reversible impact. `risk:medium`: meaningful user or workflow impact requiring careful regression coverage. `risk:high`: security, data integrity, privileged publication, durable scheduling or difficult recovery. |
| Automation | `automation:flash-candidate`: every condition below is evidenced. `automation:expert`: ready implementation with higher complexity/risk. `automation:hold`: no safe implementation dispatch yet. |

Description quality is a qualitative assessment with reasons, not a numerical
completeness score. Complexity describes the remaining implementation. When an
umbrella retains its historical feature estimate while acceptance is reconciled,
state `complexity_scope: historical_umbrella`; a short closure audit never turns
that issue into a low-complexity implementation candidate.

## Inexpensive implementation conditions

Recommend `automation:flash-candidate` only when every condition holds:

- Complexity and risk are both low, with evidence for the estimate.
- Readiness is ready, and remaining scope fits one focused implementation.
- The issue has observable acceptance, real code pointers and runnable local
  validation appropriate to the behavior.
- Current source and known PR activity have been reconciled; no active overlapping
  implementation or unverified landed solution remains.
- There is no unresolved design, credential/migration dependency, cross-component
  contract or required environment setup that turns the task into investigation.

A `needs-spec`, `blocked`, `in-progress` or `needs-verification` issue always uses
`automation:hold`, even when an expert could clarify it. Specify that investigation
as separate work if needed. Missing evidence also means hold. Ready work of medium
or high complexity or risk uses `automation:expert`.

`automation:flash-candidate` is a reviewable recommendation, not a live trigger.
The existing `agent-ready` label can start implementation in a configured flow;
apply it only after explicit assignment to that flow and confirmation of its
current model routing, supported publication policy and repository test profile.
Do not add it during assessment. A new issue update, changed source baseline or
new overlapping PR invalidates stale pickup evidence and requires refresh.

For an operator-configured inexpensive-model rule, use [model routing](model-routing.md)
with `labels.all` matching `automation:flash-candidate`, `complexity:low`,
`risk:low` and `readiness:ready`, then select an available account model and harness.
These candidate labels remain inert until a separate explicit `agent-ready`
assignment to the configured implementation flow; this policy installs no rule
and chooses no model by default.

## Implementation and verification

An issue body should separate landed behavior, remaining work, active PRs and
acceptance still awaiting evidence. Link the source revision and relevant PR state
with the observation date. A PR title alone is not completion evidence.

Use the model/CRUD layer for backend persistence and Lit for frontend components.
Choose focused behavioral regressions and repository-required checks; security,
migration and cross-cutting changes can require broader suites. Run tests with
`PRELOOP_DISABLE_TELEMETRY=true`, and run changed-file pre-commit before committing.
Keep public examples generic. Preserve the configured trusted verification and
publication boundaries regardless of model cost.
