# Browser steps

An agent that drives a browser can attach what it did to its runtime
session. Each step is an observation: the action the agent reports, the
URL or target it names, and the reasoning it gives. A stored step is not
an approval, a dispatch, or proof that the browser reached that state.

A step may carry a screenshot. It is stored encrypted as a session
artifact and served back to the console. A step without one has
`screenshot: null` in its stored metadata.

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
(`extra_too_large`) or cannot be encoded as JSON (`extra_not_json`). A
screenshot is refused as `screenshot_too_large`, `screenshot_invalid` or
`storage_budget_exhausted` (see below). The other rows in the batch are
still stored. More than 200 steps, or an
empty batch, is a 422 for the whole request.

A missing or unknown bearer is 401. A session that belongs to another
account is 404. When the key is pinned to a runtime session and the path
names a different one, the response is 403. A session that has already
ended is accepted, including a key pinned to that session, so an adapter
can flush after the run. The model gateway still rejects that key for
inference.

## Screenshots

Add a `screenshot` object to a step:

```json
{
  "source": "playwright_mcp",
  "source_step_id": "step-2",
  "step_index": 1,
  "action": "screenshot",
  "screenshot": {
    "content_type": "image/png",
    "data_base64": "iVBORw0KGgo..."
  }
}
```

`content_type` is `image/png`, `image/jpeg` or `image/webp`. The row is
refused, and nothing is stored for it, when:

- the decoded image is larger than `RUNTIME_SESSION_SCREENSHOT_MAX_BYTES`
  (2 MiB by default): `screenshot_too_large`;
- `data_base64` is not valid base64, is empty, or the bytes are not the
  declared image type: `screenshot_invalid`;
- the account's session-artifact budget
  (`RUNTIME_SESSION_ARTIFACT_ACCOUNT_MAX_BYTES`) cannot fit the image even
  after evicting older unheld artifacts: `storage_budget_exhausted`.

A repeated step (same `source_step_id`) is a duplicate and does not store
a second image.

The stored step's `metadata.screenshot` names the artifact:

```json
{
  "artifact_id": "7d0c...",
  "availability": "available",
  "content_type": "image/png",
  "size_bytes": 48213
}
```

Each session keeps at most `RUNTIME_SESSION_SCREENSHOTS_PER_SESSION_MAX`
(500 by default) available screenshots. Past that, the oldest by step
time lose their image bytes. The artifact row and the step's metadata
stay, and `availability` becomes `evicted`. Screenshots in a session under
legal hold are never evicted, so a held session can keep more than the
bound.

### Reading a screenshot

A console user with the `view_runtime_sessions` permission reads the bytes
with:

```
GET /api/v1/runtime-sessions/{runtime_session_id}/artifacts/{artifact_id}
```

The response is the image with its stored media type and
`Cache-Control: private, max-age=300`. It is 404 when the session or the
artifact is not in the caller's account, or the artifact belongs to
another session, and 410 with `{"availability": "evicted"}` or
`{"availability": "expired"}` when the bytes are gone.

## What is stored

Each accepted step is a `browser_step` activity on the session. It shows
up on the activity timeline next to tool calls, ordered by timestamp, and
its reasoning is searchable with session search. URL query secrets, and
credential-shaped text in the target and reasoning, are masked before the
row is stored.
