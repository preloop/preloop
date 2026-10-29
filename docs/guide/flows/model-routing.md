# Model routing on a flow

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

A flow can choose a model and harness per execution from the issue's current labels. The flow's selected model and harness remain the default. This is not an automatic swap mid-conversation.

For Alibaba-hosted chat models, see [Alibaba Cloud Model Studio](../alibaba-model-studio.md)
for regional endpoints, live discovery, agent controls, and cost reporting.
For [Amazon Bedrock](../providers/bedrock.md) and
[Azure OpenAI](../providers/azure-openai.md), see the provider guides for
credentials, a first request, and how the Cost page prices them.

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

The short form carries a model and an effort and nothing else. Switching harness per label is what the routing rules above are for. First match wins, and the explicit `model_routing` rules are evaluated first, so a detailed rule always beats the short form. A label that appears twice is rejected on save, because the second copy could never apply. The reasoning effort is `low`, `medium` or `high`; Codex receives it as `model_reasoning_effort` in its `config.toml`, and a harness that does not accept the value ignores it rather than failing the run.

When a label rule decides the run, the execution records the matched label, the rule id and the effort alongside the model and harness, and the execution log carries a `model_by_label` milestone. Labels still come only from the trusted label snapshot, so a webhook body cannot introduce a rule or select one that is not on the issue.

## What is recorded

Each execution stores the chosen rule or default, the label snapshot, model id, and harness under reserved `_model_routing` on `trigger_event_details`. Retries keep that selection even if you later edit the flow's rules. Native continuation of the same conversation also keeps it; a different model or harness requires an explicit new execution. A mismatch blocks the repair turn before agent launch. New executions record their default identity even without routing rules. Legacy runs without a complete recorded model and harness cannot be retried or resumed automatically because their original identity cannot be proven; start a new execution explicitly. Durable feedback threads show `model_identity_unavailable` when their pinned selection cannot be used.

Webhook bodies, tracker payloads, and authenticated trigger JSON are not authorized overrides (`_matrix`, `_model_routing`, `ai_model_id`, `_resume`, assessment). The reserved `matrix` eval grid remains separate from production routing. New matrix cells persist their effective defaults, so a later edit cannot change a retry. Historical partial cells without a complete identity require an explicit new execution. Retry and continuation pin the recorded selection from the persisted source execution after account and lineage checks; they never copy privileged fields from the request body.

Invalid or foreign models are rejected on save (HTTP 422) and at dispatch. There is no silent fallback to a more expensive model. Account budgets still apply.

## Not in this slice

Routing does not read structured assessment fields from triage output or from the event payload. Same-execution stage handoffs (for example "plan on one model, implement on another") need explicit new sessions later. Public benchmark ranking is not used as scheduling authority.

Current normalized label arrays take precedence, including an empty array. Provider issue or PR arrays are used only when no normalized array exists. Singular label-event deltas never count as current labels.


Private Cursor defaults require a valid named `host_exec_profile` and an explicit
private runner pool. The console does not use the Preloop model catalog for
Cursor. Leave Cursor model blank for Cursor Auto, or set
`agent_config.cursor_model` to a Cursor model id that the runner profile
maps. A saved catalog model still supplies the requested identifier when
`cursor_model` is empty. This does not enable Cursor
rule targets, eval matrix entries, hosted execution, or native session resume.
The runner must independently support the native profile. Empty routing rule
sets behave like absent routing configuration.

## Subscription credentials and execution identity

A subscription model is a Codex (ChatGPT sign-in) or Claude subscription
registration created when an agent was onboarded. Its credential lives in
Preloop, and the model gateway is the only component that uses or refreshes
it. By default it serves only the agent it was onboarded with.

An ordinary ephemeral flow runs with its own runtime credential, which carries
the flow and execution identity and no enrolled agent's identity. Selecting a
subscription model (including through a matrix cell), changing `runner_pool`,
or setting `target_agent_id` on an ephemeral flow does not transfer the
agent's identity.

### Use from my flows (owner opt-in)

The owner of a subscription can let flows they own use it from hosted
executions. Turn on **Use from my flows** on the model's page in the console,
or run `preloop models flow-use <model-id> on`. Three conditions must all
hold, and admission and the gateway check them independently:

- **Subscription owner.** The model's onboarded agent (every active agent
  bound to its credential) belongs to one user. That user is the
  subscription's owner. Only that user sees the switch and can change it;
  account admins cannot turn it on for someone else.
- **Opt-in.** The owner turned the setting on. Preloop stores who turned it
  on, so the setting stops applying if the agent later belongs to someone
  else.
- **Flow owner.** The flow was created by the same user, in the same account.
  The flow's owner is `created_by_user_id`, recorded when the flow is created
  or cloned from a preset (console, API or CLI). Flows that existed before
  this setting are owned by the account's only user in single-user accounts,
  and have no owner in multi-user accounts; a flow with no owner is never
  eligible. To use the subscription from such a flow, create a new flow (or
  copy the preset again) as the subscription's owner.

In a team account this means another member's flow never runs on your
subscription, including flows an admin created. Whoever can edit your flow
can still change what it does, so keep edit access to the flows you run on
your subscription limited to people you trust.

When the conditions hold, every admission site accepts the selection:
triggers, matrix cells, defaults and label rules, retries and continuations,
no-progress escalation, restricted CI preparation and orchestrator startup.
The gateway then serves the subscription to that execution's runtime
credential. Nothing changes for the onboarded agent: no binding is added or
removed, and other agents stay refused. Refreshes of the subscription token
are serialized on the stored credential, so the agent on your machine and a
hosted run never refresh it at the same time and the agent's sign-in keeps
working.

Usage from these runs is attributed to the flow execution and recorded with
the subscription cost model ($0 marginal spend, with the API-equivalent price
as an estimate). The rows carry `meta_data.subscription_use: "owner_flow"`,
and the model page shows "Used by your flows" totals for the last 30 days.
Hosted runs use the subscription's quota just like the agent does.

Before turning this on, check your provider's terms. For Codex, OpenAI's
documentation recommends API keys for automation. It also publishes a
supported guide, "Maintain Codex account auth in CI/CD", limited to trusted
private automation, which says not to use it for public or open-source
repositories. OpenAI's Sign in with ChatGPT terms (for apps that integrate
that sign-in) say tokens must be stored locally under the user's control and
requests must come from a runtime the user controls; whether those terms
cover tools that wrap Codex's own sign-in is not settled. Preloop already
holds the subscription token in the gateway for onboarded agents, so this
setting adds no new place where the token is stored; it adds a new runtime
(Preloop's hosted container). The account-sharing clauses are respected by
design: only the subscription's owner can turn this on, and only that owner's
flows can use it. If you want to stay within OpenAI's recommendation, run the
flow on a private runner you control instead, which matches OpenAI's own CI
pattern more closely, or use an API-key model.

### Admission and errors

Without the opt-in, trigger admission rejects a subscription selection with
HTTP 422 before creating an execution or launching a worker. Matrix admission
validates every cell before creating any execution. The same check covers
defaults, label rules, retries and restricted CI. Legacy queued executions
are checked again at authoritative startup before context preparation or
runtime launch; an incompatible selection ends as `FAILED` with category
`model_config` rather than remaining pending. The gateway enforces the same
rule on every request, so turning the setting off stops later requests of a
running execution with `model_not_authorized`. Successful admission does not
guarantee future credential health.

`credentials_status: active` describes the secret's state.
`supports_server_side_generation` describes Preloop's own generation
eligibility (for example summaries); it is not an executor compatibility API.
`GET /api/v1/ai-models/{id}/owner-flow-use` reports whether the opt-in applies
(`applicable`), is on (`enabled`) and whether you may change it
(`can_manage`).

Persistent execution with `agent_config.execution_path: persistent` and
`target_agent_id` uses the enrolled target's identity and existing permissions.
A private native `host_exec_profile` uses the host CLI's local authentication
and model map. Neither route borrows the model registration's credentials for
an ephemeral flow. They have distinct checkout, completion and permission
contracts, and neither is supported by the
[native restricted CI read-only review mode](restricted-ci.md#read-only-native-github-app-review).
