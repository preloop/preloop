"""Make arbitrary payloads safe to store in Postgres JSONB columns.

Gateway telemetry embeds request and response bodies into JSONB metadata.
Those bodies are attacker-shaped in practice: an agent that fetches a gzip or
otherwise binary URL hands us a body full of NUL and control bytes. Postgres
rejects U+0000 in JSONB outright (psycopg2 raises UntranslatableCharacter),
and because the telemetry insert shares the request's SQLAlchemy session, a
rejected insert used to poison that session and turn a successful model call
into a 502 for the customer.

This module is the single place that decides what is storable. It is
deliberately conservative: telemetry is lossy by nature, so we would rather
drop odd bytes than fail a customer request.

Tool evidence is not telemetry: it must keep text exactly. It uses
:func:`remove_unencodable`, which removes only lone surrogates and counts
them, instead of :func:`sanitize_for_jsonb`.
"""

from __future__ import annotations

from typing import Any

# Control characters are stripped except the three that carry real meaning in
# captured text. Tab, newline and carriage return survive so that transcripts
# and code blocks keep their shape in the UI.
_ALLOWED_CONTROL_CHARS = {"\t", "\n", "\r"}

# Guards against pathological or self-referential payloads. Real gateway
# bodies nest a handful of levels; anything past this is a bug or an attack,
# and either way it must not be able to hang the request path.
DEFAULT_MAX_DEPTH = 24

_TRUNCATION_MARKER = "... [truncated {dropped} bytes]"


def _is_disallowed_char(char: str) -> bool:
    code_point = ord(char)
    if char in _ALLOWED_CONTROL_CHARS:
        return False
    # C0 controls (includes U+0000, the one Postgres refuses) and DEL.
    if code_point < 0x20 or code_point == 0x7F:
        return True
    # C1 controls. These are invisible and routinely appear in binary bodies.
    if 0x80 <= code_point <= 0x9F:
        return True
    # Lone surrogates. Valid text never contains them and they break the
    # psycopg2 encode path with a UnicodeEncodeError before Postgres sees them.
    if 0xD800 <= code_point <= 0xDFFF:
        return True
    return False


#: Removal category for :func:`remove_unencodable`.
LONE_SURROGATES = "lone_surrogates"


def _is_lone_surrogate(char: str) -> bool:
    return 0xD800 <= ord(char) <= 0xDFFF


def remove_unencodable(value: Any) -> tuple[Any, dict[str, int]]:
    """Remove only what tool evidence cannot store, and count it.

    Tool evidence is stored as canonical JSON text, not as JSONB values.
    Canonical JSON escapes every C0 control including U+0000, and C1
    controls and DEL are valid JSON and JSONB, so all of those are kept
    exactly. Lone surrogates are the one exception: they have no UTF-8
    encoding, so the canonical bytes, the digest and the API response
    cannot hold them. They are removed and counted.

    Unlike :func:`sanitize_for_jsonb` this is not lossy for ordinary text,
    and it leaves every non-string value untouched.

    Args:
        value: A JSON-shaped value (dict, list, str or scalar).

    Returns:
        The value with lone surrogates removed from strings and keys, and
        ``{category: count}`` for what was removed (empty when nothing was).
    """
    counts: dict[str, int] = {}

    def text(item: str) -> str:
        found = sum(1 for char in item if _is_lone_surrogate(char))
        if not found:
            return item
        counts[LONE_SURROGATES] = counts.get(LONE_SURROGATES, 0) + found
        return "".join(char for char in item if not _is_lone_surrogate(char))

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            return text(item)
        if isinstance(item, dict):
            return {
                (text(key) if isinstance(key, str) else key): walk(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [walk(child) for child in item]
        return item

    cleaned = walk(value)
    return (cleaned if counts else value), counts


def sanitize_string(value: str, *, max_chars: int | None = None) -> str:
    """Strip JSONB-hostile characters from one string, optionally capping it.

    The cap is applied after stripping so the limit describes what we actually
    store, and it reports the number of characters dropped so an operator
    reading the row knows the value is partial rather than genuinely short.
    """
    if any(_is_disallowed_char(char) for char in value):
        value = "".join(char for char in value if not _is_disallowed_char(char))
    if max_chars is not None and len(value) > max_chars:
        dropped = len(value) - max_chars
        return value[:max_chars] + _TRUNCATION_MARKER.format(dropped=dropped)
    return value


def sanitize_for_jsonb(
    value: Any,
    *,
    max_string_chars: int | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> Any:
    """Return a copy of ``value`` that Postgres will accept in a JSONB column.

    Walks dicts (keys and values), lists, tuples and sets recursively. Scalars
    that are not strings pass through untouched. Structures deeper than
    ``max_depth``, and cycles, collapse to a marker string rather than
    recursing forever.
    """
    return _sanitize(
        value,
        max_string_chars=max_string_chars,
        max_depth=max_depth,
        depth=0,
        seen=frozenset(),
    )


def _sanitize(
    value: Any,
    *,
    max_string_chars: int | None,
    max_depth: int,
    depth: int,
    seen: frozenset[int],
) -> Any:
    if isinstance(value, str):
        return sanitize_string(value, max_chars=max_string_chars)

    # bool must be checked before int; it is a subclass of int in Python.
    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, (bytes, bytearray)):
        # Bytes are not JSON serializable at all. Decode leniently and let the
        # string path strip whatever survives.
        decoded = bytes(value).decode("utf-8", errors="replace")
        return sanitize_string(decoded, max_chars=max_string_chars)

    if isinstance(value, (dict, list, tuple, set, frozenset)):
        if depth >= max_depth:
            return "[truncated: max depth exceeded]"
        identity = id(value)
        if identity in seen:
            return "[truncated: circular reference]"
        next_seen = seen | {identity}

        if isinstance(value, dict):
            sanitized: dict[Any, Any] = {}
            for key, item in value.items():
                # Keys become JSON object names, so they need the same
                # treatment as values. A NUL in a key is just as fatal.
                safe_key = (
                    sanitize_string(key, max_chars=max_string_chars)
                    if isinstance(key, str)
                    else key
                )
                sanitized[safe_key] = _sanitize(
                    item,
                    max_string_chars=max_string_chars,
                    max_depth=max_depth,
                    depth=depth + 1,
                    seen=next_seen,
                )
            return sanitized

        sanitized_items = [
            _sanitize(
                item,
                max_string_chars=max_string_chars,
                max_depth=max_depth,
                depth=depth + 1,
                seen=next_seen,
            )
            for item in value
        ]
        # Sets and tuples are not JSON types; JSONB stores them as arrays, so
        # normalize here rather than leaving it to the driver.
        return sanitized_items

    # Anything else (datetime, UUID, model objects) is left for the existing
    # serializer to handle. Stringifying here would silently change stored
    # shapes that callers already depend on.
    return value
