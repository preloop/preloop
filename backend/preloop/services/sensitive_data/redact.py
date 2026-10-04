"""Value-level redaction built on the shared detectors (#1123).

``redact_text`` replaces each detected span with ``[REDACTED:<type>]`` and
reports how many spans of each type were replaced. ``redact_structure``
walks dicts, lists and tuples and rewrites string leaves only; keys are
never touched so a row keeps its shape.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple

from preloop.services.sensitive_data.detectors import DetectorConfig, Match, detect

REDACTION_PREFIX = "[REDACTED:"
REDACTION_SUFFIX = "]"

Counts = Dict[str, int]


def redaction_token(type_id: str) -> str:
    """The placeholder written for one match of ``type_id``."""
    return f"{REDACTION_PREFIX}{type_id}{REDACTION_SUFFIX}"


def _apply(text: str, matches: Sequence[Match]) -> str:
    """Rebuild the text left to right from non-overlapping, start-sorted spans."""
    pieces = []
    cursor = 0
    for match in matches:  # sorted by start, non-overlapping
        pieces.append(text[cursor : match.start])
        pieces.append(redaction_token(match.type))
        cursor = match.end
    pieces.append(text[cursor:])
    return "".join(pieces)


def redact_text(
    text: Optional[str], config: Optional[DetectorConfig] = None
) -> Tuple[Optional[str], Counts]:
    """Return ``text`` with every match replaced and the counts by type.

    ``None`` and empty strings pass through unchanged with empty counts so
    callers can apply this without branching.
    """
    if not text or not isinstance(text, str):
        return text, {}
    matches = detect(text, config)
    if not matches:
        return text, {}
    counts: Counts = {}
    for match in matches:
        counts[match.type] = counts.get(match.type, 0) + 1
    return _apply(text, matches), counts


def redact_structure(
    value: Any, config: Optional[DetectorConfig] = None
) -> Tuple[Any, Counts]:
    """Redact every string leaf of ``value``. Keys and non-strings are kept.

    Returns a new structure (the input is not mutated) and the counts by
    type summed over all leaves.
    """
    counts: Counts = {}

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            redacted, found = redact_text(item, config)
            for name, count in found.items():
                counts[name] = counts.get(name, 0) + count
            return redacted
        if isinstance(item, dict):
            return {key: walk(child) for key, child in item.items()}
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, tuple):
            return tuple(walk(child) for child in item)
        return item

    return walk(value), counts


def merge_counts(*parts: Counts) -> Counts:
    """Sum several count dicts."""
    total: Counts = {}
    for part in parts:
        for name, count in (part or {}).items():
            total[name] = total.get(name, 0) + count
    return total
