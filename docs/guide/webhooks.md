# Outbound event webhooks

Preloop can POST signed governance events to a URL you own: an approval was
raised or decided, a policy denied a call, a runtime session closed, spend
crossed a budget, a flow execution finished. This is the integration path for
a SIEM, a GRC platform or any internal audit collector.

This page is about events Preloop **sends**. For webhooks Preloop **receives**
(flow triggers, tracker callbacks) see
[webhook triggers](../webhook-triggers.md).

## Register an endpoint

Console: **Settings > Webhooks > Add endpoint**. API:

```bash
curl -X POST https://<your-preloop>/api/v1/event-webhooks/endpoints \
  -H "Authorization: Bearer $PRELOOP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "url": "https://collector.example.com/preloop",
        "description": "SIEM",
        "event_types": ["approval.decided", "policy.denied"]
      }'
```

The response carries `secret` **once**. It is stored encrypted and there is no
endpoint that reads it back; if you lose it, rotate by creating a new endpoint
or setting a new URL and re-registering. Later reads show only `secret_hint`,
the last four characters.

An empty `event_types` list means every v1 event. An unknown event type is
rejected at create time rather than silently never firing. An account may
register up to 20 endpoints.

`GET /api/v1/event-webhooks/catalogue` returns the subscribable event types
with one-line descriptions, plus the signature header name, the verification
tolerance and the retry schedule this deployment is running. Read the contract
from there rather than hard-coding it.

## The envelope

Every request body is one JSON object with exactly these keys:

```json
{
  "id": "0f2f4b8e-2a6a-5c3f-9b71-6c0f2e4a51d3",
  "type": "approval.decided",
  "version": "1",
  "occurred_at": "2026-09-08T11:04:22.481000+00:00",
  "account_id": "6c1c2a30-9b0e-4d9a-9f52-3a2b1c0d4e5f",
  "data": { "...": "event specific" }
}
```

- `id` is stable for the fact it describes, not for the HTTP request. The same
  approval decision emitted twice produces the same `id`, so **deduplicate on
  `id`**. It is also sent as `X-Preloop-Event-Id`.
- `occurred_at` is when the fact happened, not when the POST was made. A
  delivery retried 40 minutes later still carries the original time.
- Adding a key inside `data` is not a version bump. Removing or renaming one
  is, and would arrive as `version: "2"` alongside a new event type family.
- Bodies are capped at 64 KB. A payload over the cap is replaced with
  `{"truncated": true, "reason": "..."}` rather than dropped.

### Events (v1)

| Type | Fires when | Notable `data` fields |
| --- | --- | --- |
| `approval.created` | An approval request is raised for a tool call | `approval_request_id`, `tool_name`, `summary`, `managed_agent_name`, `requested_at`, `expires_at` |
| `approval.decided` | The request reaches `approved`, `declined`, `expired` or `cancelled` | `decision`, `resolved_at`, `actor` (`kind`: `user`, `ai`, `bypass`, `system`, plus `id`), `rule` (the rule or policy that decided it), `comment` |
| `policy.denied` | A policy rule denies a tool call | `tool_name`, `rule_description`, `condition_matched`, `execution_id`, `user_id`, `correlation_id` |
| `session.ended` | A runtime session closes | `runtime_session_id`, `reason` (`execution_finished`, `idle`, `operator`), `runtime_principal_*`, `duration_seconds` |
| `budget.threshold` | Spend crosses a configured soft limit | `scope`, `scope_id`, `period`, `limit_amount`, `spent_amount`, `percent_used`, `threshold_percent` |
| `budget.exceeded` | Spend passes a configured hard limit | same as above |
| `flow.execution.finished` | A flow execution reaches a terminal status | `execution_id`, `flow_id`, `flow_name`, `status`, `failure_category`, `evidence_receipt` |
| `agent.note_sent` | An operator note is accepted for a running agent | `note_id`, `managed_agent_id`, `runtime_session_id`, `text`, `author` (`user_id`, `display`, `auth_method`), `created_at`, `expires_at` |
| `agent.note_delivered` | That note reaches the agent at a turn boundary | same fields plus `delivered_at`, `delivery_channel` (`gateway`, `hook`, `claude_channel`, `claude_message`), `turn_index` |

Operator note payloads do carry the note `text`, unlike approval payloads.
The text is the fact, and a receiver mirroring notes into a ticket or a
chat room has nothing without it. See [operator notes](operator-notes.md).

Approval payloads deliberately omit tool arguments. Those routinely carry the
payload the approval exists to guard, and a webhook target is not the audit
log. Fetch the request over the API if you need them.

`evidence_receipt` is present only when the execution captured one. It mirrors
the stored receipt (`status`, `transport`, `artifact_id`, `sha256`,
`manifest_sha256`, `size_bytes`, `retention_hours`, `object_lock`,
`legal_hold`, `integrity_verified`). What those fields do and do not prove is
covered in [evidence storage](flows/evidence-storage.md); the webhook repeats
the receipt, it does not add an assurance the receipt did not already carry.

Test sends use type `webhook.test` and are delivered to the endpoint you
tested regardless of its filter.

## Verifying the signature

Every request carries:

| Header | Value |
| --- | --- |
| `X-Preloop-Signature` | `t=<unix seconds>,v1=<hex hmac>` |
| `X-Preloop-Event-Id` | Event id, stable across retries and replays |
| `X-Preloop-Event-Type` | The event type |
| `X-Preloop-Delivery-Id` | This delivery row; differs per endpoint and per replay |
| `X-Preloop-Attempt` | 1-based attempt number |
| `User-Agent` | `Preloop-Webhook/1` |

The `v1` value is `HMAC-SHA256(secret, f"{t}.{raw_body}")` in lowercase hex.
Sign the **raw bytes** you received; do not parse and re-serialize the JSON
first. Reject a request whose `t` is more than 300 seconds from your clock.
Preloop stamps a fresh `t` on every attempt, so a delivery retried an hour
later still lands inside that window.

Parse the header by splitting on commas and taking the parts you know: a
future `v2=` may be added alongside `v1=`, and a receiver that ignores unknown
parts keeps working.

### Python

```python
import hashlib
import hmac
import time

TOLERANCE_SECONDS = 300


def verify(raw_body: bytes, header: str, secret: str) -> bool:
    parts = dict(
        part.strip().split("=", 1) for part in header.split(",") if "=" in part
    )
    timestamp = parts.get("t")
    signature = parts.get("v1")
    if not timestamp or not signature:
        return False
    if abs(time.time() - int(timestamp)) > TOLERANCE_SECONDS:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("ascii") + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
```

FastAPI or Flask: read the body with `await request.body()` or
`request.get_data()`, never `request.json`.

### Node

```js
const crypto = require("crypto");

const TOLERANCE_SECONDS = 300;

function verify(rawBody, header, secret) {
  const parts = Object.fromEntries(
    header.split(",").map((p) => p.trim().split("="))
  );
  const { t, v1 } = parts;
  if (!t || !v1) return false;
  if (Math.abs(Date.now() / 1000 - Number(t)) > TOLERANCE_SECONDS) return false;
  const expected = crypto
    .createHmac("sha256", secret)
    .update(Buffer.concat([Buffer.from(`${t}.`), rawBody]))
    .digest("hex");
  const a = Buffer.from(expected);
  const b = Buffer.from(v1);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
```

With Express, mount `express.raw({ type: "application/json" })` on the webhook
route so `req.body` is the original buffer.

## What the signature does not prove

The signature proves that whoever produced the request held your endpoint
secret at that timestamp, and that the body was not altered in transit. That
is all. Specifically:

- **The audit log is not signed.** These events are a notification of a fact,
  not an attested copy of Preloop's record of it. Nothing here is a signed
  extract of the audit trail, and the audit trail carries no counter-signature
  you can check against a delivered event.
- **The signature is not a receipt for the evidence bundle.** For a
  `flow.execution.finished` event the `evidence_receipt` fields are copied
  from storage. `integrity_verified` reflects a check Preloop ran, attested by
  Preloop; the HMAC does not independently confirm it.
- **The secret is symmetric.** Anyone holding it, including Preloop, can mint
  a valid signature. It authenticates the channel; it is not a non-repudiable
  signature by a key only Preloop holds.
- **Ordering is not guaranteed.** Retries mean a later event can arrive first.
  Order on `occurred_at`, not on arrival.
- **Delivery is at-least-once.** The same `id` can arrive more than once.
  Deduplicate.

## Delivery, retries and dead letters

Events are written to a database outbox inside the transaction that produced
them, and posted by a background worker. Nothing is sent on the request path,
so a slow receiver never slows an approval or a tool call.

- Up to **6 attempts**: immediately, then after roughly 10 s, 1 m, 5 m, 15 m
  and 40 m, each multiplied by jitter between 0.8 and 1.2. Total window is
  about one hour.
- Any 2xx is success. Everything else, including a timeout after 10 s, is a
  failed attempt. Response bodies are not stored, only the status code and a
  short error string.
- After the last attempt the delivery becomes **dead**. It stays visible at
  `GET /api/v1/event-webhooks/deliveries/dead-letter` and in the console.
- After **10 consecutive failures** an endpoint's circuit breaker opens and it
  stops being attempted for 15 minutes, after which one probe is allowed
  through. Editing the URL or re-enabling the endpoint closes the breaker
  immediately.
- Undelivered rows are bounded at 10,000 per account. Past that, new events
  are refused and the reason is stamped on the endpoint, so a dead receiver
  cannot grow the outbox without limit.
- Delivered and dead rows are purged after 14 days.

Replay one event to every endpoint that already received it:

```bash
curl -X POST \
  https://<your-preloop>/api/v1/event-webhooks/deliveries/<event-id>/replay \
  -H "Authorization: Bearer $PRELOOP_TOKEN"
```

A replay is a new generation of the same event id. The original rows are left
as they are, so a dead-lettered attempt stays on the record, and the receiver
sees the same `X-Preloop-Event-Id` with a new `X-Preloop-Delivery-Id`.

## Approval workflow webhooks

An approval workflow with `webhook_url` in its `approval_config` keeps
working, unchanged in shape: the same JSON body goes to the same URL. It is
now delivered through the outbox, which means it is **signed and retried**
where it previously was a single unsigned POST with no retry.

The signing secret is `approval_config.webhook_secret`. Set it yourself to
choose it; leave it out and Preloop generates one and writes it back beside
the URL, where you can read it. These endpoints appear in the console list
read-only, so you can see one failing, and are edited by changing the approval
workflow.

Two behaviour changes worth knowing: `webhook_posted_at` on the approval
request is now stamped when a receiver actually accepted the delivery rather
than when the POST was issued, and a receiver that is down no longer loses the
notification.

## Settings

| Setting | Default | Effect |
| --- | --- | --- |
| `WEBHOOK_DELIVERY_ENABLED` | `true` | Off records events in the outbox but posts nothing |
| `WEBHOOK_DELIVERY_POLL_SECONDS` | `5` | Outbox poll interval |
| `WEBHOOK_DELIVERY_BATCH_SIZE` | `50` | Deliveries claimed per pass |
| `WEBHOOK_DELIVERY_CONCURRENCY` | `8` | Concurrent POSTs per pass |
| `WEBHOOK_DELIVERY_TIMEOUT_SECONDS` | `10` | Per-attempt HTTP timeout |
| `WEBHOOK_MAX_PENDING_PER_ACCOUNT` | `10000` | Outbox bound per account |
| `WEBHOOK_CIRCUIT_FAILURE_THRESHOLD` | `10` | Consecutive failures that open the breaker |
| `WEBHOOK_CIRCUIT_COOLDOWN_SECONDS` | `900` | How long the breaker stays open |
| `WEBHOOK_DELIVERY_RETENTION_DAYS` | `14` | Retention for delivered and dead rows |
| `WEBHOOK_BLOCK_PRIVATE_TARGETS` | `false` | Refuse URLs resolving to loopback, link-local, private or reserved space |

`WEBHOOK_BLOCK_PRIVATE_TARGETS` is off by default because a self-hosted
deployment posting to a collector on the same private network is the normal
case. Turn it on for multi-tenant hosting. The check resolves the hostname
when the endpoint is registered, not on every attempt, so a name that later
re-resolves into private space is not caught by it.
