"""Read a native final answer, accepting one Markdown code fence around it.

Stdlib only and self-contained: the source of this module is embedded in the
in-container scripts (the repair checker and the result capture, see
``CodexAgent``), and the controller imports it, so every reader of a native
final answer decides the same way.

A model that is not held to the output schema by its provider sometimes
writes the JSON document inside a Markdown fence. The fence carries no
content, so exactly that shape is unwrapped: after trimming whitespace, the
whole answer is one fenced block (three backticks, an optional ``json``
language tag in any case, a newline, the body, three closing backticks) and
the body parses as one JSON value. Nothing else is tolerated: no JSON pulled
out of prose, no second block, no text after the fence, no repair of invalid
JSON. The unwrapped document still goes through every schema and integrity
check.
"""

import json
import re
from typing import Any, Optional

#: Recorded when a fence was removed from the answer.
MARKDOWN_FENCE = "markdown_fence"

_FENCE = re.compile(r"\A```(?:[jJ][sS][oO][nN])?[ \t]*\r?\n(.*?)\r?\n?```\Z", re.S)


def parse_native_answer(raw: Any) -> tuple[Any, Optional[str]]:
    """Parse a native final answer as JSON, unwrapping one Markdown fence.

    Args:
        raw: The answer as text or bytes.

    Returns:
        The parsed value and :data:`MARKDOWN_FENCE` when a fence was removed,
        otherwise None.

    Raises:
        ValueError: The answer is not JSON, fenced or not (the error of the
            unwrapped parse, so the message names what was wrong).
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8")
    text = str(raw)
    try:
        return json.loads(text), None
    except ValueError:
        match = _FENCE.match(text.strip())
        if match is None:
            raise
    return json.loads(match.group(1)), MARKDOWN_FENCE
