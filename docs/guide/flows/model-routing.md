# Model routing on a flow

A flow can choose a model and harness per execution from the issue's current labels. The flow's selected model and harness remain the default. This is not an automatic swap mid-conversation.

For Alibaba-hosted chat models, see [Alibaba Cloud Model Studio](../alibaba-model-studio.md)
for regional endpoints, live discovery, agent controls, and cost reporting.

## Configure rules

On Create / Edit Flow, under **Model routing rules**:

1. Add a rule and give it a stable id (recorded on the execution when it matches).
2. Enter comma-separated labels using the project's existing names. `any` matches if at least one listed label is present. `all` matches only if every listed label is present. Both constraints apply together when both are set.
3. Pick a harness and an account model from the same dropdowns as the flow default.
4. Reorder with Up / Down. The first matching rule wins.

If no rule matches, the flow's selected model and harness are used. Removing every rule deletes `agent_config.model_routing` and leaves the rest of `agent_config` (sandbox, image, iteration limits, and so on) unchanged.

Example stored document:

```json
{
  "version": 1,
  "rules": [
    {
      "id": "docs-fast",
      "labels": { "any": ["documentation"] },
      "ai_model_id": "11111111-1111-1111-1111-111111111111",
      "agent_type": "codex"
    },
    {
      "id": "backend-all",
      "labels": { "all": ["bug", "backend"] },
      "ai_model_id": "22222222-2222-2222-2222-222222222222",
      "agent_type": "opencode"
    }
  ]
}
```

Use whatever labels the project already has. Preloop does not ship a default label set for routing.

## Model by label

**Model by label**, under the rules above, is the short form of the same idea: one label, one model, one reasoning effort. Add a rule, type the label, pick a model, pick an effort. Leaving the model empty keeps the flow's own model, so a rule can say "same model, think harder" without naming one.

The list lives at `agent_config.model_by_label` and is empty by default:

```json
[
  { "label": "complexity:high", "ai_model_id": "11111111-1111-1111-1111-111111111111", "reasoning_effort": "high" },
  { "label": "complexity:low", "reasoning_effort": "low" }
]
```

First match wins, and the explicit `model_routing` rules above are evaluated first, so a detailed rule always beats the short form. A label that appears twice is rejected on save, because the second copy could never apply. The reasoning effort is `low`, `medium` or `high`; Codex receives it as `model_reasoning_effort` in its `config.toml`, and a harness that does not accept the value ignores it rather than failing the run.

When a label rule decides the run, the execution records the matched label, the rule id and the effort alongside the model and harness, and the execution log carries a `model_by_label` milestone. Labels still come only from the trusted label snapshot, so a webhook body cannot introduce a rule or select one that is not on the issue.

## What is recorded

Each execution stores the chosen rule or default, the label snapshot, model id, and harness under reserved `_model_routing` on `trigger_event_details`. Retries keep that selection even if you later edit the flow's rules. Native continuation of the same conversation also keeps it; a different model or harness requires an explicit new execution. A mismatch blocks the repair turn before agent launch. New executions record their default identity even without routing rules. Legacy runs without a complete recorded model and harness cannot be retried or resumed automatically because their original identity cannot be proven; start a new execution explicitly. Durable feedback threads show `model_identity_unavailable` when their pinned selection cannot be used.

Webhook bodies, tracker payloads, and authenticated trigger JSON are not authorized overrides (`_matrix`, `_model_routing`, `ai_model_id`, `_resume`, assessment). The reserved `matrix` eval grid remains separate from production routing. New matrix cells persist their effective defaults, so a later edit cannot change a retry. Historical partial cells without a complete identity require an explicit new execution. Retry and continuation pin the recorded selection from the persisted source execution after account and lineage checks; they never copy privileged fields from the request body.

Invalid or foreign models are rejected on save (HTTP 422) and at dispatch. There is no silent fallback to a more expensive model. Account budgets still apply.

## Not in this slice

Routing does not read structured assessment fields from triage output or from the event payload. Same-execution stage handoffs (for example "plan on one model, implement on another") need explicit new sessions later. Public benchmark ranking is not used as scheduling authority.

Current normalized label arrays take precedence, including an empty array. Provider issue or PR arrays are used only when no normalized array exists. Singular label-event deltas never count as current labels.


Private Cursor defaults require a valid named `host_exec_profile` and an explicit
private runner pool. Selected models must be account-visible LLM rows; the local
profile supplies credentials and the model mapping. This does not enable Cursor
rule targets, eval matrix entries, hosted execution, or native session resume.
The runner must independently support the native profile. Empty routing rule
sets behave like absent routing configuration.
