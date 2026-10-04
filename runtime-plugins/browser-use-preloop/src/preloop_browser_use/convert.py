"""Convert Browser Use ``AgentHistory`` items into Preloop browser steps.

Fields are read by name from either the pydantic objects Browser Use
passes to ``on_step_end`` or their ``model_dump()`` dicts, so the same code
handles a live agent and a recorded history file. Typed text and other
action parameters are never copied: they can hold credentials.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOURCE = "browser_use"
#: Internal key for a screenshot file read later, off the event loop.
DEFERRED_SCREENSHOT_PATH = "_screenshot_path"

#: Browser Use action names (0.5 to 0.7) mapped to Preloop step actions.
ACTION_MAP = {
    "go_to_url": "navigate",
    "navigate": "navigate",
    "open_tab": "navigate",
    "switch_tab": "navigate",
    "go_back": "navigate",
    "search_google": "navigate",
    "search": "navigate",
    "click_element_by_index": "click",
    "click": "click",
    "input_text": "type",
    "input": "type",
    "send_keys": "type",
    "upload_file": "type",
    "select_dropdown_option": "select",
    "get_dropdown_options": "select",
    "scroll_down": "scroll",
    "scroll_up": "scroll",
    "scroll": "scroll",
    "scroll_to_text": "scroll",
    "extract_content": "extract",
    "extract_structured_data": "extract",
    "extract": "extract",
    "wait": "wait",
    "done": "done",
    "screenshot": "screenshot",
}

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _action_entries(model_output: Any) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(name, params)`` for each action, skipping unset ones."""
    entries = []
    for action in _get(model_output, "action") or []:
        data = action
        if not isinstance(data, dict) and hasattr(data, "model_dump"):
            data = data.model_dump(exclude_unset=True)
        if not isinstance(data, dict):
            continue
        for name, params in data.items():
            if params is None:
                continue
            entries.append((name, params if isinstance(params, dict) else {}))
    return entries


def _target(element: Any) -> str | None:
    if element is None:
        return None
    attrs = _get(element, "attributes") or {}
    for key in ("aria-label", "name", "placeholder", "title", "id", "href"):
        value = attrs.get(key) if isinstance(attrs, dict) else None
        if value:
            tag = _get(element, "tag_name") or "element"
            return f'{tag}[{key}="{value}"]'[:1024]
    xpath = _get(element, "xpath")
    return str(xpath)[:1024] if xpath else None


def _encoded_screenshot(encoded: Any) -> dict[str, str] | None:
    """Wrap base64 image text as ``BrowserScreenshotIn`` after sniffing it."""
    if not encoded or not isinstance(encoded, str):
        return None
    try:
        head = base64.b64decode(encoded[:24] + "=" * (-len(encoded[:24]) % 4))
    except (binascii.Error, ValueError):
        return None
    for magic, content_type in _MAGIC:
        if head.startswith(magic):
            return {"content_type": content_type, "data_base64": encoded}
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return {"content_type": "image/webp", "data_base64": encoded}
    return None


def resolve_screenshot_path(step: dict[str, Any]) -> dict[str, Any]:
    """Read a deferred ``screenshot_path`` into the step's ``screenshot``.

    Called from the worker thread so file I/O never runs on the agent's
    event loop. Steps without a deferred path are returned unchanged.
    """
    path = step.pop(DEFERRED_SCREENSHOT_PATH, None)
    if path and Path(path).is_file():
        shot = _encoded_screenshot(
            base64.b64encode(Path(path).read_bytes()).decode("ascii")
        )
        if shot:
            step["screenshot"] = shot
    return step


def _screenshot(state: Any) -> dict[str, str] | None:
    """Return an inline screenshot as ``BrowserScreenshotIn``, else ``None``.

    Browser Use 0.5+ keeps screenshots on disk (``screenshot_path``;
    ``get_screenshot()`` just reads that file). Those are deferred to the
    worker thread by the caller, so nothing here touches the disk.
    """
    return _encoded_screenshot(_get(state, "screenshot"))


def _occurred_at(metadata: Any) -> str | None:
    stamp = _get(metadata, "step_end_time") or _get(metadata, "step_start_time")
    if not isinstance(stamp, (int, float)):
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()


def history_item_to_step(
    item: Any, *, run_id: str, index: int, read_files: bool = True
) -> dict[str, Any]:
    """Map one ``AgentHistory`` item to a ``BrowserStepIn`` JSON object.

    Args:
        item: ``AgentHistory`` object or its dict dump.
        run_id: Stable id of the agent run; with the step number it forms
            ``source_step_id`` so a re-post is a duplicate, not a new row.
        index: Zero-based position in the run, used when metadata has no
            step number.
        read_files: Read a ``screenshot_path`` file now. The reporter passes
            ``False`` and reads it in its worker thread instead.
    """
    model_output = _get(item, "model_output")
    state = _get(item, "state")
    metadata = _get(item, "metadata")
    results = _get(item, "result") or []
    entries = _action_entries(model_output)
    names = [name for name, _ in entries]
    first_name, first_params = entries[0] if entries else (None, {})
    action = ACTION_MAP.get(first_name or "", "other")

    url = _get(state, "url")
    if action == "navigate" and isinstance(first_params.get("url"), str):
        url = first_params["url"]
    elements = _get(state, "interacted_element") or []
    target = next((t for t in (_target(e) for e in elements) if t), None)

    reasoning_parts = [
        _get(model_output, "thinking"),
        _get(model_output, "next_goal"),
    ]
    reasoning = "\n".join(str(p) for p in reasoning_parts if p) or None

    errors = [_get(r, "error") for r in results if _get(r, "error")]
    failed = bool(errors) or any(
        _get(r, "is_done") and _get(r, "success") is False for r in results
    )
    step_number = _get(metadata, "step_number")
    number = step_number if isinstance(step_number, int) else index + 1

    extra: dict[str, Any] = {"browser_use_actions": names[:20]}
    if isinstance(step_number, int):
        extra["browser_use_step_number"] = step_number
    if errors:
        # Error text can quote the action's input, so only a flag is sent.
        extra["error"] = "action_failed"

    step: dict[str, Any] = {
        "source": SOURCE,
        "source_step_id": f"{run_id}:{number}"[:200],
        "step_index": index,
        "action": action,
        "status": "failed" if failed else "success",
        "extra": extra,
    }
    if isinstance(url, str) and url:
        step["url"] = url[:2048]
    if target:
        step["target"] = target
    if reasoning:
        step["reasoning"] = reasoning[:8000]
    occurred_at = _occurred_at(metadata)
    if occurred_at:
        step["occurred_at"] = occurred_at
    shot = _screenshot(state)
    if shot:
        step["screenshot"] = shot
    elif _get(state, "screenshot_path"):
        step[DEFERRED_SCREENSHOT_PATH] = str(_get(state, "screenshot_path"))
        if read_files:
            resolve_screenshot_path(step)
    return step


def history_to_steps(history: Any, *, run_id: str) -> list[dict[str, Any]]:
    """Convert an ``AgentHistoryList`` (or its dump, or a list) to steps."""
    items = _get(history, "history", history) or []
    return [
        history_item_to_step(item, run_id=run_id, index=i)
        for i, item in enumerate(items)
    ]
