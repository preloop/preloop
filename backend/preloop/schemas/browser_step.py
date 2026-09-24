"""Request and response shapes for browser step ingestion.

A browser step is an observation an agent attaches to its runtime session.
It records what the agent says it did in a browser. It is not an approval,
a dispatch, or proof that the page reached the state the step describes.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

BrowserStepSource = Literal["api", "browser_use", "skyvern", "playwright_mcp"]
BrowserStepAction = Literal[
    "navigate",
    "click",
    "type",
    "select",
    "scroll",
    "screenshot",
    "extract",
    "wait",
    "done",
    "other",
]
BrowserStepStatus = Literal["success", "failed", "unknown"]

#: ``json.dumps(extra)`` larger than this is rejected per row, not as a 422
#: for the rest of the batch.
MAX_BROWSER_STEP_EXTRA_BYTES = 4096
ERROR_EXTRA_TOO_LARGE = "extra_too_large"
ERROR_EXTRA_NOT_JSON = "extra_not_json"


def browser_step_extra_error(extra: dict[str, Any]) -> str | None:
    """Return a stable error code when ``extra`` cannot be stored.

    Args:
        extra: Caller-supplied metadata for one step.

    Returns:
        ``extra_too_large`` when the default JSON encoding exceeds 4096
        bytes, ``extra_not_json`` when it cannot be encoded, or ``None``
        when the value is acceptable.
    """
    try:
        encoded = json.dumps(extra).encode("utf-8")
    except (TypeError, ValueError):
        return ERROR_EXTRA_NOT_JSON
    if len(encoded) > MAX_BROWSER_STEP_EXTRA_BYTES:
        return ERROR_EXTRA_TOO_LARGE
    return None


class BrowserStepIn(BaseModel):
    """One browser action observed by an agent."""

    source: BrowserStepSource = "api"
    source_step_id: str = Field(min_length=1, max_length=200)
    step_index: int = Field(ge=0)
    action: BrowserStepAction
    url: str | None = Field(default=None, max_length=2048)
    target: str | None = Field(default=None, max_length=1024)
    reasoning: str | None = Field(default=None, max_length=8000)
    status: BrowserStepStatus = "success"
    occurred_at: datetime | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class BrowserStepBatchIn(BaseModel):
    """A batch of browser steps from one agent flush."""

    steps: list[BrowserStepIn] = Field(min_length=1, max_length=200)


class BrowserStepBatchOut(BaseModel):
    """How many steps in a batch were stored, repeated, or refused."""

    accepted: int
    duplicates: int
    rejected: list[dict[str, Any]]
