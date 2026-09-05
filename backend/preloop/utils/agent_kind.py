"""Shared normalization for durable managed-agent kinds (issue #123).

An agent kind records *which product* an agent is (``cursor``), as opposed
to ``session_source_type``, which records *how it connects*
(``desktop_agent``). Kinds arrive from three places -- the runtime-session
token endpoint, the ``POST /api/v1/agents`` request schema, and the managed
agent CRUD layer -- and every one of them must agree on the accepted shape,
because kinds are compared for equality during default-agent resolution and
echoed verbatim into comma-separated filter query strings.

Keeping one definition here means a kind normalized on the way in always
matches the same kind normalized on the way out.
"""

from __future__ import annotations

import re
from typing import Optional

#: Kinds are echoed into comma-separated filter query strings, so keep them
#: to a conservative bare-identifier shape.
AGENT_KIND_PATTERN = re.compile(r"[a-z0-9_]+")

#: Human-readable constraint, reused verbatim in API error messages.
AGENT_KIND_SHAPE_ERROR = "agent_kind must contain only letters, digits, and underscores"


def normalize_agent_kind(value: Optional[str]) -> str:
    """Fold one raw agent-kind token into its canonical form.

    Case, surrounding whitespace, and the space/hyphen/underscore split are
    all treated as insignificant, so ``"Gemini CLI"``, ``"gemini-cli"``, and
    ``"gemini_cli"`` resolve to the same kind.

    Args:
        value: Raw kind as supplied by a client, or None.

    Returns:
        The normalized kind, or an empty string when nothing was supplied.
        The result is not guaranteed to match ``AGENT_KIND_PATTERN``;
        callers that accept untrusted input must check it with
        ``is_valid_agent_kind``.
    """
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def is_valid_agent_kind(value: str) -> bool:
    """Report whether a normalized kind is a bare identifier."""
    return bool(AGENT_KIND_PATTERN.fullmatch(value))
