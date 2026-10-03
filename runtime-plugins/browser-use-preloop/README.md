# preloop-browser-use

Report each [Browser Use](https://github.com/browser-use/browser-use) step,
with its screenshot, to a Preloop runtime session. The steps show up on the
session timeline next to the model turns that produced them.

Supported Browser Use versions: `>=0.5,<0.8`. The tests replay a 12-step
history dump in the 0.7 `AgentHistoryList` shape.

## Install

```bash
pip install ./runtime-plugins/browser-use-preloop
```

## Use

```python
from browser_use import Agent
from preloop_browser_use import PreloopBrowserUseReporter

# Reads PRELOOP_URL, PRELOOP_AGENT_KEY, PRELOOP_RUNTIME_SESSION_ID.
reporter = PreloopBrowserUseReporter.from_env()
agent = Agent(task="Draft a reorder for SKU WH-7781", llm=llm)
history = await reporter.run(agent, max_steps=30)  # flushes at the end
```

If you call `agent.run` yourself, pass the hook and flush afterwards:

```python
history = await agent.run(on_step_end=reporter.on_step_end)
await reporter.flush()
```

## Configuration

| Variable | Meaning |
| --- | --- |
| `PRELOOP_URL` | Preloop base URL, with or without `/api/v1`. |
| `PRELOOP_AGENT_KEY` | The agent key (falls back to `PRELOOP_API_KEY`). The same bearer the model gateway accepts. |
| `PRELOOP_RUNTIME_SESSION_ID` | Runtime session the steps attach to. Use the `runtime_session_id` returned by `POST /api/v1/auth/runtime-sessions/token`, or the id shown on the session page. |

When any variable is missing the reporter logs one warning and does
nothing; the agent still runs.

## What is sent

Each history item becomes one step with `source="browser_use"`:

- `action`: the first action, mapped (`go_to_url` to `navigate`,
  `click_element_by_index` to `click`, `input_text` to `type`, and so on;
  unknown actions are `other`). All action names are kept in
  `extra.browser_use_actions`.
- `url`, `target` (the interacted element's label, name or XPath),
  `reasoning` (`thinking` and `next_goal`), `status` (`failed` when a result
  carries an error), `occurred_at` (step end time).
- `screenshot`: the PNG, JPEG or WebP Browser Use captured.
- `source_step_id`: `<agent id>:<step number>`, so posting the same run
  again is counted as duplicates.

Typed text and other action parameters are never sent; they can contain
credentials.

## Failure behaviour

`on_step_end` only queues. Batches (10 steps by default) are posted in a
worker thread, three tries with backoff (0.5 s, 1 s) on connection errors,
408, 425, 429 and 5xx. A batch that still fails is dropped with one warning;
the agent run is never stopped or slowed by Preloop.

See [Browser steps](../../docs/guide/browser-agents.md) for the API and how
steps appear in the console.
