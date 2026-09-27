"""Derive browser step observations from Playwright MCP tool calls.

When the MCP firewall proxies a Playwright MCP server, every browser tool
call the agent makes is already recorded as a ``tool_call`` activity. This
module turns the same call into a ``browser_step`` row so the session
timeline shows what the agent did in the browser without an adapter on the
agent side. The derived step is an observation of the call the firewall
forwarded. It is not an approval, a dispatch, or proof that the page
reached the state the step describes.

Tool names and argument shapes follow ``@playwright/mcp@0.0.82``, the
package pinned in the browser environment profile
(``docs/guide/flows/environments-and-recovery.md``). A tool this table does
not know is not a Playwright tool for derivation purposes, so a newer
package that adds tools records them as plain ``tool_call`` rows until the
table is extended.

Argument values that can carry secrets (``text`` for ``browser_type``,
``key`` for ``browser_press_key``, ``values`` for ``browser_select_option``)
are never copied into a step. ``browser_type`` records only the typed
length under ``extra.typed_chars``.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, Final

from preloop.schemas.browser_step import (
    BrowserStepAction,
    BrowserStepIn,
    BrowserStepStatus,
)

#: Step ``source`` for rows derived here.
PLAYWRIGHT_STEP_SOURCE: Final = "playwright_mcp"

#: ``@playwright/mcp@0.0.82`` tool name to browser step action.
PLAYWRIGHT_TOOL_ACTIONS: Final[dict[str, BrowserStepAction]] = {
    "browser_navigate": "navigate",
    "browser_navigate_back": "navigate",
    "browser_click": "click",
    "browser_type": "type",
    "browser_press_key": "type",
    "browser_select_option": "select",
    "browser_hover": "other",
    "browser_take_screenshot": "screenshot",
    "browser_snapshot": "extract",
    "browser_wait_for": "wait",
    "browser_close": "done",
}

#: Argument keys that are copied into the step as ``url`` and ``target``.
#: Everything else stays out of the row.
_URL_ARGUMENT: Final = "url"
_TARGET_ARGUMENTS: Final = ("element", "ref")

#: ``tool_call`` activity status to browser step status. A refused call
#: never reached the browser, so it has no entry and derives no step.
_STATUS_BY_TOOL_CALL_STATUS: Final[dict[str, BrowserStepStatus]] = {
    "succeeded": "success",
    "success": "success",
    "failed": "failed",
}


def is_playwright_tool(name: str) -> bool:
    """Return whether ``name`` is a Playwright MCP browser tool this module maps.

    Args:
        name: Client-visible tool name, as the agent called it.

    Returns:
        True when a ``browser_step`` should be derived for the call.
    """
    return name in PLAYWRIGHT_TOOL_ACTIONS


def _as_text(value: Any, *, limit: int) -> str | None:
    """Return ``value`` as a bounded string, or ``None`` when it is not text."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


def derive_step(
    *,
    tool_name: str,
    arguments: dict[str, Any] | None,
    status: str,
    correlation_id: str,
    step_index: int,
) -> BrowserStepIn | None:
    """Build the browser step for one proxied Playwright tool call.

    Args:
        tool_name: Client-visible tool name.
        arguments: Arguments the agent passed. Only ``url``, ``element``
            and ``ref`` are copied; ``text`` contributes its length only.
        status: The ``tool_call`` activity status (``succeeded``,
            ``failed`` or ``refused``).
        correlation_id: Correlation id of the tool call. Becomes the step's
            ``source_step_id`` so the two rows can be joined and a retry of
            the derivation is idempotent.
        step_index: Position of the step in the session, starting at 0.

    Returns:
        The step to store, or ``None`` when ``tool_name`` is not a mapped
        Playwright tool or the call was refused before reaching the browser.
    """
    action = PLAYWRIGHT_TOOL_ACTIONS.get(tool_name)
    if action is None:
        return None
    step_status = _STATUS_BY_TOOL_CALL_STATUS.get(status)
    if step_status is None:
        return None
    args = arguments if isinstance(arguments, dict) else {}

    url = _as_text(args.get(_URL_ARGUMENT), limit=2048)
    target: str | None = None
    for key in _TARGET_ARGUMENTS:
        target = _as_text(args.get(key), limit=1024)
        if target is not None:
            break

    # ``tool`` keeps the raw tool name because two tools can map to one
    # action (``browser_navigate`` and ``browser_navigate_back``).
    extra: dict[str, Any] = {"tool": tool_name}
    if tool_name == "browser_type":
        typed = args.get("text")
        extra["typed_chars"] = len(typed) if isinstance(typed, str) else 0

    return BrowserStepIn(
        source=PLAYWRIGHT_STEP_SOURCE,
        source_step_id=correlation_id,
        step_index=max(int(step_index), 0),
        action=action,
        url=url,
        target=target,
        reasoning=None,
        status=step_status,
        extra=extra,
    )


def extract_screenshot(result: Any) -> tuple[str, bytes] | None:
    """Return the first image in an MCP tool result as decoded bytes.

    Args:
        result: The raw list of MCP content items the upstream server
            returned, before the firewall stringified it for the agent. A
            ``CallToolResult``-like object with a ``content`` list is also
            accepted.

    Returns:
        ``(media_type, data)`` for the first ``ImageContent`` item, or
        ``None`` when there is no image or its base64 payload is invalid.
        ``media_type`` falls back to ``image/png`` when the item names none,
        which is what Playwright MCP produces by default.
    """
    items = result
    if not isinstance(items, (list, tuple)):
        items = getattr(result, "content", None)
    if not isinstance(items, (list, tuple)):
        return None
    for item in items:
        if getattr(item, "type", None) != "image":
            continue
        data = getattr(item, "data", None)
        if not isinstance(data, str) or not data.strip():
            return None
        try:
            decoded = base64.b64decode(data.strip(), validate=True)
        except (binascii.Error, ValueError):
            return None
        if not decoded:
            return None
        media_type = getattr(item, "mimeType", None) or "image/png"
        return str(media_type), decoded
    return None
