# preloop-skyvern

Import a finished [Skyvern](https://github.com/Skyvern-AI/skyvern) task into
a Preloop runtime session: each Skyvern step becomes a browser step on the
session timeline with its screenshot, and the task's HAR, Playwright trace
and video are stored as session artifacts.

Pinned to the Skyvern REST API v1 task routes (`/api/v1/tasks/{id}`,
`/steps`, `/steps/{step_id}/artifacts`, `x-api-key` auth). The tests replay
recorded responses in that shape; no live Skyvern is called.

## Install

```bash
pip install ./runtime-plugins/skyvern-preloop
```

## Import one task

```bash
export SKYVERN_API_KEY=...            # Skyvern key
export SKYVERN_BASE_URL=https://api.skyvern.com   # or your self-hosted URL
export PRELOOP_URL=https://preloop.example.com
export PRELOOP_AGENT_KEY=...          # session-bound agent key
preloop-skyvern-import --task tsk_123 --session <runtime_session_id>
```

Mint `PRELOOP_AGENT_KEY` and the session id with
`POST /api/v1/auth/runtime-sessions/token` (see the
[artifacts guide](../../docs/guide/artifacts.md#get-a-session-bound-key)).
The command prints a JSON summary and exits 1 if any step was refused.
`--no-files` imports steps and screenshots only.

Re-running the import is safe: steps dedupe on
`source_step_id = <task_id>:<step_id>` and each file deposit carries
`Idempotency-Key: skyvern:<task_id>:<artifact_id>`.

## Webhook setup

Skyvern calls `webhook_callback_url` when a task ends, with the task JSON
as the body and `x-skyvern-signature` (HMAC-SHA256 of the raw body keyed
with your Skyvern API key). `handle_webhook` verifies the signature,
ignores tasks that have not finished, and imports the rest. Example with
FastAPI:

```python
import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from preloop_skyvern import PreloopClient, PreloopTarget, SkyvernClient, handle_webhook

app = FastAPI()
SKYVERN_KEY = os.environ["SKYVERN_API_KEY"]
skyvern = SkyvernClient(SKYVERN_KEY, base_url=os.environ["SKYVERN_BASE_URL"])

def preloop_for_task(task: dict):
    # Map the task to the session it belongs to. Here: one session per
    # deployment from env; return None to ignore a task.
    sid = os.environ.get("PRELOOP_RUNTIME_SESSION_ID")
    if not sid:
        return None
    return PreloopClient(PreloopTarget(
        os.environ["PRELOOP_URL"], os.environ["PRELOOP_AGENT_KEY"], sid))

@app.post("/skyvern/webhook")
async def skyvern_webhook(request: Request):
    # handle_webhook blocks (fetches and downloads); keep it off the loop.
    status, body = await run_in_threadpool(
        handle_webhook, await request.body(), dict(request.headers),
        skyvern=skyvern, skyvern_api_key=SKYVERN_KEY,
        preloop_for_task=preloop_for_task)
    return JSONResponse(body, status_code=status)
```

Then create Skyvern tasks with
`"webhook_callback_url": "https://your-host/skyvern/webhook"`. Responses:
401 bad signature, 400 bad payload, 202 ignored (still running or no
session), 200 imported, 502 Skyvern unreachable or its response changed
shape.

`handle_webhook` is synchronous and can take a while for a large
recording; run it in a thread pool as above, or hand the task id to a job
queue and return 202. The signature covers the body only, so a captured
request can be replayed; that is harmless here because the handler only
re-reads the task from Skyvern and every write is idempotent.

## What is sent

| Skyvern | Preloop |
| --- | --- |
| step (ordered by `order`, `retry_index`) | browser step, `source="skyvern"` |
| first action type | `action` (`click`, `input_text` to `type`, `select_option` to `select`, `complete` to `done`, unknown to `other`) |
| action `reasoning` (or `intention`) | `reasoning` |
| step `status` and action results | `status` (`failed` when a result failed); the first exception type (not its message) in `extra.error` |
| `screenshot_action` (else `screenshot_llm`) | step `screenshot` (skipped above 2 MiB; if Preloop still refuses it the step is re-sent without it and `extra.screenshot_omitted` names why) |
| `har` | `trace` artifact, HAR wrapped in a zip (`application/zip`) |
| `trace` | `trace` artifact as is |
| `recording` | `recording` artifact (`video/webm` or `video/mp4`) |

Typed text and other action parameters are never sent. LLM prompts,
responses and HTML scrapes are not imported. On a Preloop server without
the artifact deposit API (before preloop/preloop#1080) the files are
skipped with a log line naming #1080.
