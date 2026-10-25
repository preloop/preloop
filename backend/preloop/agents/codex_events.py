"""Render Codex JSON events without treating prompt/tool echoes as completion.

The standalone filter runs inside the agent image, which need not have Preloop
installed. Keep it stdlib-only. Only the last assistant message in a completed
turn becomes plain text; all other output remains escaped, single-line JSON.
"""

import shlex

CODEX_EVENT_FILTER_SCRIPT = r"""
import json
import sys

last_message = None
for line in sys.stdin:
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        print(json.dumps({"codex_diagnostic": line.rstrip("\n")}), flush=True)
        continue
    print(json.dumps(event, ensure_ascii=True), flush=True)
    if not isinstance(event, dict):
        continue
    kind = event.get("type")
    if kind in ("turn.started", "turn.failed"):
        last_message = None
    elif kind == "item.completed":
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                last_message = text
    elif kind == "turn.completed":
        if last_message is not None:
            print(last_message, flush=True)
        last_message = None
        usage = event.get("usage")
        if isinstance(usage, dict):
            counts = [usage.get("input_tokens", 0), usage.get("output_tokens", 0)]
            if all(type(n) is int and n >= 0 for n in counts):
                print("tokens used", flush=True)
                print(format(sum(counts), ","), flush=True)
"""


def codex_event_filter_command() -> str:
    """Return a shell-safe unbuffered filter command for the agent image."""
    return "python3 -u -c " + shlex.quote(CODEX_EVENT_FILTER_SCRIPT)
