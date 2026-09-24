# Browser steps

An agent that drives a browser can attach what it did to its runtime
session. Each step is an observation: the action the agent reports, the
URL or target it names, and the reasoning it gives. A stored step is not
an approval, a dispatch, or proof that the browser reached that state.

Screenshots are not accepted yet. The `screenshot` field on a stored step
is always `null`.

## Sending steps

Authenticate with the agent key, the same bearer the model gateway accepts.
Post one batch of 1 to 200 steps:

```bash
curl -X POST \
  "https://preloop.example.com/api/v1/runtime-sessions/$SESSION_ID/browser-steps" \
  -H "Authorization: Bearer $AGENT_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "steps": [
      {
        "source": "playwright_mcp",
        "source_step_id": "step-1",
        "step_index": 0,
        "action": "navigate",
        "url": "https://app.example.com/inbox",
        "reasoning": "Open the inbox the task named.",
        "status": "success"
      }
    ]
  }'
```

`source` is `api` (the default), `browser_use`, `skyvern`, or
`playwright_mcp`. `source_step_id` is the idempotency key together with
the session and `source`: posting the same key again returns the original
row and counts it as a duplicate. A batch may mix new steps, duplicates,
and rows that are refused individually.

The response is:

```json
{"accepted": 1, "duplicates": 0, "rejected": []}
```

`rejected` entries are `{"index": 0, "error": "extra_too_large"}`. `extra`
is refused when `json.dumps(extra)` is larger than 4096 bytes
(`extra_too_large`) or cannot be encoded as JSON (`extra_not_json`). The
other rows in the batch are still stored. More than 200 steps, or an
empty batch, is a 422 for the whole request.

A missing or unknown bearer is 401. A session that belongs to another
account is 404. When the key is pinned to a runtime session and the path
names a different one, the response is 403. A session that has already
ended is accepted, including a key pinned to that session, so an adapter
can flush after the run. The model gateway still rejects that key for
inference.

## What is stored

Each accepted step is a `browser_step` activity on the session. It shows
up on the activity timeline next to tool calls, ordered by timestamp, and
its reasoning is searchable with session search. URL query secrets, and
credential-shaped text in the target and reasoning, are masked before the
row is stored.
