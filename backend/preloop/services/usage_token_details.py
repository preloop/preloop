"""Normalize provider cache and reasoning token counts from raw usage payloads.

Three usage shapes are supported:

- OpenAI Chat Completions: ``prompt_tokens_details.cached_tokens`` (plus
  ``cache_creation_tokens`` / ``cache_creation_input_tokens`` /
  ``cache_creation.ephemeral_5m_input_tokens``) and
  ``completion_tokens_details.reasoning_tokens``.
- OpenAI Responses: ``input_tokens_details.cached_tokens`` (plus
  ``cache_creation_tokens`` when a provider emits it) and
  ``output_tokens_details.reasoning_tokens``.
- Anthropic Messages: top-level ``cache_read_input_tokens`` /
  ``cache_creation_input_tokens``.

For each normalized field the first candidate carrying a valid count wins. An
explicit ``0`` is a valid count (the provider reported no caching), while a
missing or malformed value is unknown and stays ``None``. Malformed values
(negative numbers, booleans, fractional floats, non-numeric strings) are
skipped, never clamped and never raised.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DIGITS = re.compile(r"\d+")

#: Usage keys whose presence means a payload carries cache/reasoning detail.
DETAIL_CONTAINER_KEYS = (
    "prompt_tokens_details",
    "completion_tokens_details",
    "input_tokens_details",
    "output_tokens_details",
)
TOP_LEVEL_CACHE_KEYS = ("cache_read_input_tokens", "cache_creation_input_tokens")


def coerce_token_count(value: Any) -> Optional[int]:
    """Return ``value`` as a non-negative token count, or None if unusable.

    Accepts non-negative ints, integral non-negative floats (``80.0``) and
    strings of ASCII digits (``"80"``). Rejects booleans, negatives,
    fractional or non-finite floats, other strings and every other type.

    Args:
        value: Raw value from a provider usage payload.

    Returns:
        The count, or None when the value is not a valid count.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer() or value < 0:
            return None
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        return int(text) if _DIGITS.fullmatch(text) else None
    return None


def _nested(container: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = container.get(key)
    return value if isinstance(value, dict) else {}


def extract_token_details(
    usage_details: Optional[Dict[str, Any]],
) -> Dict[str, Optional[int]]:
    """Extract cache/reasoning token counts from a provider usage payload.

    Args:
        usage_details: Raw provider usage dict, possibly empty or None.

    Returns:
        Dict with ``cache_read_tokens``, ``cache_creation_tokens`` and
        ``reasoning_tokens``; each is None when the provider reported no
        valid value.
    """
    usage = usage_details if isinstance(usage_details, dict) else {}
    prompt_details = _nested(usage, "prompt_tokens_details")
    prompt_cache_creation = _nested(prompt_details, "cache_creation")
    input_details = _nested(usage, "input_tokens_details")
    completion_details = _nested(usage, "completion_tokens_details")
    output_details = _nested(usage, "output_tokens_details")

    rejected: list[str] = []

    def first(*candidates: tuple[str, Any]) -> Optional[int]:
        for name, value in candidates:
            if value is None:
                continue
            count = coerce_token_count(value)
            if count is not None:
                return count
            rejected.append(name)
        return None

    result = {
        "cache_read_tokens": first(
            (
                "prompt_tokens_details.cached_tokens",
                prompt_details.get("cached_tokens"),
            ),
            ("input_tokens_details.cached_tokens", input_details.get("cached_tokens")),
            ("cache_read_input_tokens", usage.get("cache_read_input_tokens")),
        ),
        "cache_creation_tokens": first(
            (
                "prompt_tokens_details.cache_creation_tokens",
                prompt_details.get("cache_creation_tokens"),
            ),
            (
                "prompt_tokens_details.cache_creation_input_tokens",
                prompt_details.get("cache_creation_input_tokens"),
            ),
            (
                "prompt_tokens_details.cache_creation.ephemeral_5m_input_tokens",
                prompt_cache_creation.get("ephemeral_5m_input_tokens"),
            ),
            (
                "input_tokens_details.cache_creation_tokens",
                input_details.get("cache_creation_tokens"),
            ),
            ("cache_creation_input_tokens", usage.get("cache_creation_input_tokens")),
        ),
        "reasoning_tokens": first(
            (
                "completion_tokens_details.reasoning_tokens",
                completion_details.get("reasoning_tokens"),
            ),
            (
                "output_tokens_details.reasoning_tokens",
                output_details.get("reasoning_tokens"),
            ),
        ),
    }
    if rejected:
        # Field names only: usage payloads never reach the log.
        logger.debug(
            "Ignored %d malformed usage token count(s): %s",
            len(rejected),
            ", ".join(rejected),
        )
    return result


def has_token_detail_fields(usage_details: Any) -> bool:
    """Return True when a raw usage payload carries any cache/reasoning field.

    Args:
        usage_details: Raw provider usage payload.

    Returns:
        True if any detail container or top-level cache key is present.
    """
    if not isinstance(usage_details, dict):
        return False
    return any(
        isinstance(usage_details.get(key), dict) for key in DETAIL_CONTAINER_KEYS
    ) or any(usage_details.get(key) is not None for key in TOP_LEVEL_CACHE_KEYS)
