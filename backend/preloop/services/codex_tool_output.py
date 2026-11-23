"""Recognise Codex's own cut of a tool result, as the model received it.

Codex 0.153.4 cuts a tool result longer than its per-result limit in the
middle and prefixes a warning. The two shapes, verified against tag
``rust-v0.153.4``:

* Unified exec (``codex-rs/core/src/tools/context.rs``,
  ``ExecCommandToolOutput::response_text``): a header of ``Chunk ID``,
  ``Wall time``, exit or session, ``Original token count`` lines, then
  ``Output:``, then ``Warning: truncated output (original token count: N)``
  and a blank line. Below the collection cap ``truncated_output_with_policy``
  delegates to ``formatted_truncate_text``, which adds ``Total output lines:
  N`` before the blank line; above it an optional ``... N bytes omitted ...``
  line takes that place.
* Other text results (``codex-rs/utils/output-truncation/src/lib.rs``,
  ``formatted_truncate_text``): the warning is the first line, followed by
  ``Total output lines: N`` and a blank line.

In both, the kept text carries the middle marker ``…N tokens truncated…`` or
``…N chars truncated…`` (``codex-rs/utils/string/src/truncate.rs``). Only the
warning at that position, with a middle marker after it, counts: a file that
merely contains the sentence is not a clip.

Stdlib only.
"""

from __future__ import annotations

import re
from typing import Any

_WARNING = r"Warning: truncated output \(original token count: (?P<count>\d+)\)\n"
_EXEC_CLIP = re.compile(
    r"(?:Chunk ID: [0-9a-f]+\n)?"
    r"Wall time: \d+\.\d{4} seconds\n"
    r"(?:Process exited with code -?\d+\n)?"
    r"(?:Process running with session ID -?\d+\n)?"
    r"(?:Original token count: (?P<header>\d+)\n)?"
    r"Output:\n" + _WARNING + r"(?:(?P<lines>Total output lines: \d+\n)"
    r"|(?:\.\.\. \d+ bytes omitted \.\.\.\n)?)\n"
)
_FORMATTED_CLIP = re.compile(_WARNING + r"Total output lines: \d+\n\n")
_MIDDLE_MARKER = re.compile("…\\d+ (?:tokens|chars) truncated…")
_EXIT_CODE = re.compile(
    r"(?:Chunk ID: [0-9a-f]+\n)?Wall time: \d+\.\d{4} seconds\n"
    r"Process exited with code (?P<code>-?\d+)\n"
)


def result_text(value: Any) -> str | None:
    """The text of a tool result as sent to the model, or None.

    Args:
        value: ``output`` or ``content`` of a result item: a string, or a list
            of content parts with ``text``.

    Returns:
        The text, or None when the result carries no text.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = [
            part["text"]
            for part in value
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        return "\n".join(parts) if parts else None
    return None


def codex_clip(value: Any) -> int | None:
    """Codex's reported original token count when it cut this result.

    Args:
        value: The tool result as sent to the model.

    Returns:
        The original token count, or None when Codex did not cut the result.
    """
    text = result_text(value)
    if text is None:
        return None
    match = _EXEC_CLIP.match(text)
    if match is not None:
        # With omitted bytes Codex reuses the header's count in the warning;
        # the formatted path recounts the collected text, so they can differ.
        header = match.group("header")
        if (
            match.group("lines") is None
            and header is not None
            and header != match.group("count")
        ):
            return None
    else:
        match = _FORMATTED_CLIP.match(text)
    if match is None or not _MIDDLE_MARKER.search(text, match.end()):
        return None
    return int(match.group("count"))


def exec_exit_code(value: Any) -> int | None:
    """The exit code a unified exec result reports, or None while it runs.

    Args:
        value: The tool result as sent to the model.

    Returns:
        The exit code, or None when the result names none.
    """
    text = result_text(value)
    match = _EXIT_CODE.match(text) if text is not None else None
    return int(match.group("code")) if match is not None else None
