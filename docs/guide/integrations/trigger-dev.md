# trigger.dev Integration

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

After this page you have a [trigger.dev](https://trigger.dev) task, written with the Vercel AI SDK, whose model calls go through the Preloop gateway and whose risky tool call (a refund) waits for a Preloop human approval before it runs.

trigger.dev runs the agents you build: durable execution, queues, retries, checkpoints. Preloop governs the agents you run: model spend, budgets, approvals and the audit trail, across trigger.dev tasks and the coding agents your team already uses. The two fit together with two environment variables and one HTTP call.

A runnable example lives in the repository at [`examples/trigger-dev`](https://github.com/preloop/preloop/tree/main/examples/trigger-dev).

---

## Who this is for

- Teams that ship AI agents as trigger.dev tasks and want token cost per task and per agent, not only compute time.
- Teams that need a budget that stops a runaway task before the provider bill does.
- Teams whose agents can move money, data or infrastructure, and who want a named human to approve those calls from their phone, Slack or a webhook, with the decision on record.

## What you get

| | Where it shows up in Preloop |
|---|---|
| Cost per task run and per agent: tokens and estimated spend for every model call | **Cost** (usage by model and by session, each session naming its agent) |
| Budgets: account, API key and managed-agent budgets with soft and hard limits; a hard limit denies the call before it reaches the provider | [Model Gateway: Budget and Allowed Models](../concepts/model-gateway.md#budget-and-allowed-models) |
| Session timeline: one runtime session per trigger.dev run, with every model request | **Audit > Sessions** |
| Approval ledger: who approved or declined which tool call, with the arguments, the agent's reason, the channel and the time | The approval request and its history (`GET /api/v1/approval-requests/{id}/history`), and the [`approval.decided` event webhook](../webhooks.md) |
| Provider keys stay in Preloop: the task holds one Preloop credential, never an OpenAI or Anthropic key | **Models** |

---

## Step 1: register the task as an agent

Each trigger.dev project (or each task, if you want them budgeted separately) becomes one **custom managed agent** in Preloop. Its credential authenticates both the model gateway and approval requests, and every call made with it is attributed to that agent.

```bash
PRELOOP_URL=https://<your-preloop>          # or Preloop Cloud
PRELOOP_TOKEN=<your user API token>

# 1. Register the agent
AGENT_ID=$(curl -s -X POST "$PRELOOP_URL/api/v1/agents" \
  -H "Authorization: Bearer $PRELOOP_TOKEN" -H "Content-Type: application/json" \
  -d '{"display_name": "trigger.dev support agent"}' | jq -r .id)

# 2. Mint its durable credential (the token is shown once)
curl -s -X POST "$PRELOOP_URL/api/v1/agents/$AGENT_ID/credentials" \
  -H "Authorization: Bearer $PRELOOP_TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "trigger.dev task"}' | jq -r .token
```

The token starts with `agt_`. Store it as a trigger.dev environment variable (dashboard **Environment Variables**, or `.env` for local dev):

```bash
PRELOOP_URL=https://<your-preloop>
PRELOOP_AGENT_TOKEN=agt_...
PRELOOP_MODEL=gpt-4o-mini
```

`PRELOOP_MODEL` is a model you configured in Preloop with **gateway routing enabled**; `GET $PRELOOP_URL/openai/v1/models` with the agent token lists the names the agent may call.

## Step 2: route model calls through the gateway

Point the AI SDK provider at Preloop. Nothing else in your `streamText` or `generateText` call changes.

```ts
import { createOpenAI } from "@ai-sdk/openai";

const preloop = createOpenAI({
  baseURL: `${process.env.PRELOOP_URL}/openai/v1`,
  apiKey: process.env.PRELOOP_AGENT_TOKEN,
  // One Preloop session per trigger.dev run
  headers: { "X-Preloop-Session-Id": ctx.run.id },
});

const model = preloop.chat(process.env.PRELOOP_MODEL!);
```

- The gateway serves `/openai/v1/chat/completions`, `/openai/v1/responses` and `/anthropic/v1/messages`. With `@ai-sdk/anthropic`, use `baseURL: ${PRELOOP_URL}/anthropic/v1` and the same token as `apiKey` (sent as `x-api-key`, which the gateway accepts).
- `X-Preloop-Session-Id` groups the run's calls into one runtime session. Without it, calls are still attributed to the agent and the credential, but not split per run.
- Cost is attributed by the credential: the managed agent, the API key, the model and the session. There is no extra cost header to set.

## Step 3: gate the risky tool on a Preloop approval

Inside the tool's `execute`, ask Preloop before doing anything irreversible:

```ts
const res = await fetch(`${process.env.PRELOOP_URL}/api/v1/agents/permission-check`, {
  method: "POST",
  headers: {
    Authorization: `Bearer ${process.env.PRELOOP_AGENT_TOKEN}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    tool_name: "refund",
    tool_input: { orderId, amountCents },
    agent_reasoning: reason,      // shown to the approver
    session_id: ctx.run.id,       // links the approval to the run's session
    source: "trigger_dev",
  }),
});
const { decision, reason: why, request_id, timed_out } = await res.json();
if (decision !== "allow") return { refunded: false, why };
```

What Preloop does with that call:

1. Your rules for the tool run first: a blocked tool is denied, a matching rule decides without a human.
2. Otherwise Preloop creates an approval request and notifies the approvers of the agent's approval workflow (the account default, or one pinned on the agent): mobile and watch push, email, Slack, Mattermost or a [signed webhook](../approvals/notifications.md#webhooks).
3. The HTTP call stays open until someone decides or the workflow timeout expires (300 seconds unless the workflow sets another value). An expiry returns `decision: "deny"` with `timed_out: true`.
4. Any HTTP error should be treated as a deny. The example does that.

Set the task's `maxDuration` above the approval workflow timeout so trigger.dev does not stop the run while a human is deciding.

### With trigger.dev's own `needsApproval`

trigger.dev's chat agents support AI SDK `needsApproval`, which asks the **end user** in your app's UI before a tool runs. The Preloop check asks the **operator** your policy names, out of band. They answer different questions and can be stacked: keep `needsApproval: true` for the user's confirmation, and call the Preloop check inside `execute` for the policy decision and the ledger entry. Background tasks with no user in the loop use the Preloop check alone.

---

## Self-hosted

Both sides self-host. Point `PRELOOP_URL` at your Preloop instance; the task needs to reach it over HTTPS from wherever trigger.dev runs the task (Cloud workers or your own). In a Kubernetes install the gateway runs as its own Service; from outside the cluster use the public origin, as described in [Which gateway URL an agent calls](../../operations/model-gateway-url.md).

## Limits

- Preloop sees what goes through it: the model calls routed to the gateway and the tool calls you send to `permission-check`. It does not see the rest of the task's code, other tools' execution, or trigger.dev's runs, retries and queues. Use trigger.dev's run view for those.
- The approval wait is a held HTTP request, not a trigger.dev waitpoint, so the run stays active while a human decides. For decisions that may take hours, keep the workflow timeout short and let the agent tell the user a human will follow up.
- `needsApproval` decisions made in your app are not recorded in Preloop unless you also call `permission-check`.
- Token counts and cost come from the provider response as seen by the gateway. Calls a task makes directly to a provider, bypassing the gateway, are not counted.

## What we verified

!!! note "Executed for this page (2026-10-09)"
    - The example type-checks (`npx tsc --noEmit`) against `@trigger.dev/sdk` 4.7.3, `ai` 7.0.136 and `@ai-sdk/openai` 4.0.91, with no trigger.dev account.
    - The agent code from the example ran as a plain Node script (`npm run smoke`) against a local Preloop stack with a mock OpenAI-compatible upstream: the gateway recorded 2 requests in one runtime session attributed to the registered agent, and the refund tool raised an approval that was approved once and declined once through the API, with the decision returned to the tool.
    - **Not executed end to end inside trigger.dev**: the task was not run by `trigger.dev dev`, trigger.dev Cloud or a self-hosted trigger.dev. The task wrapper is a thin `task()` around the same code. No real model provider was called.

## Related

- [Model Gateway](../concepts/model-gateway.md)
- [Approval notifications](../approvals/notifications.md)
- [Event webhooks](../webhooks.md)
- [Runtime Sessions](../concepts/runtime-sessions.md)
