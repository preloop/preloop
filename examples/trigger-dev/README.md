# trigger.dev + Preloop example

A trigger.dev task (TypeScript, Vercel AI SDK) that answers a support ticket.
Its model calls go through the Preloop gateway (cost per run and per agent,
budgets, session timeline) and its `refund` tool waits for a Preloop human
approval (mobile, Slack, Mattermost or webhook, recorded) before it runs.

Guide: [docs/guide/integrations/trigger-dev.md](../../docs/guide/integrations/trigger-dev.md)

## Files

| File | What it does |
|---|---|
| `src/preloop.ts` | `preloopModel()` (AI SDK model on the Preloop gateway) and `requestApproval()` (Preloop permission check) |
| `src/agent.ts` | The agent: `streamText` with a `refund` tool gated by `requestApproval()` |
| `src/trigger/governed-agent.ts` | The trigger.dev task, one Preloop session per run |
| `src/smoke.ts` | Runs the same agent as a plain Node script, without trigger.dev |
| `trigger.config.ts` | trigger.dev project config |

## 1. Prepare Preloop

1. Configure a model in Preloop and enable gateway routing for it.
2. Register a custom agent and mint its credential (the guide has the two
   `curl` calls). Copy the `agt_...` token.
3. Make sure the account's approval workflow notifies someone (the account
   owner by default).

## 2. Configure

```bash
npm install
cp .env.example .env   # fill PRELOOP_URL, PRELOOP_AGENT_TOKEN, PRELOOP_MODEL
npm run typecheck      # no trigger.dev account needed
```

## 3. Run

Without trigger.dev, to check the Preloop side (Node 22.6 or later):

```bash
npm run smoke
```

The run pauses at the refund until you approve or decline it in Preloop, then
prints the reply and the token usage. The session shows up in Preloop under
the agent you registered.

As a trigger.dev task: set `TRIGGER_PROJECT_REF`, add the three `PRELOOP_*`
variables to your trigger.dev environment, then run `npm run dev` (`npx trigger.dev@latest dev`) and
trigger `governed-support-agent` with:

```json
{ "ticketId": "T-1001", "customerMessage": "Order A-42 arrived broken. Please refund the 49.00 EUR." }
```

## Notes

- No lockfile is committed: `npm install` resolves the current releases of the
  SDKs, so you get their latest security fixes.

- The approval call fails closed: an HTTP error or an expired approval is a
  deny, and the tool tells the model the refund did not happen.
- Keep `maxDuration` in `trigger.config.ts` above the approval workflow
  timeout (300 seconds by default).
