"""Stable import-fingerprint helpers for usage attribution (#112 / #123).

The fingerprint algorithm must byte-match PR #137's ``event_fingerprint`` so
merge/rekey can recompute stored ``meta_data.import_fingerprint`` values
without reopening double-count on re-import. Keep this as the single shared
helper; do not copy the payload assembly elsewhere.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Optional


def event_fingerprint(
    *,
    source: str,
    agent_principal_id: str,
    timestamp: datetime,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    cost: Optional[float],
    session_id: Optional[str],
) -> str:
    """Compute a stable dedupe fingerprint for one normalized import event.

    Args:
        source: Import source label (for example ``"cursor_csv"``).
        agent_principal_id: Attribution target ``session_source_id``.
        timestamp: Event timestamp.
        model: Model alias / identifier.
        prompt_tokens: Prompt token count.
        completion_tokens: Completion token count.
        total_tokens: Total token count.
        cache_read_tokens: Cache-read token count.
        cache_creation_tokens: Cache-creation token count.
        cost: Estimated cost in USD, or None.
        session_id: Optional upstream session id.

    Returns:
        Hex-encoded sha256 digest.
    """
    payload = "|".join(
        [
            source,
            agent_principal_id,
            timestamp.isoformat(),
            model,
            str(prompt_tokens),
            str(completion_tokens),
            str(total_tokens),
            str(cache_read_tokens),
            str(cache_creation_tokens),
            f"{cost:.6f}" if cost is not None else "None",
            session_id or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fingerprint_from_usage_row(row: Any, *, agent_principal_id: str) -> Optional[str]:
    """Recompute an import fingerprint from a persisted ApiUsage row.

    Args:
        row: ApiUsage ORM row (or duck-typed object with the same fields).
        agent_principal_id: Survivor/rekeyed principal id to embed.

    Returns:
        Recomputed fingerprint, or None when the row lacks import metadata.
    """
    meta = dict(getattr(row, "meta_data", None) or {})
    source = meta.get("import_source")
    if not source:
        return None
    timestamp = getattr(row, "timestamp", None)
    if timestamp is None:
        return None
    return event_fingerprint(
        source=str(source),
        agent_principal_id=agent_principal_id,
        timestamp=timestamp,
        model=str(getattr(row, "model_alias", None) or ""),
        prompt_tokens=int(getattr(row, "prompt_tokens", None) or 0),
        completion_tokens=int(getattr(row, "completion_tokens", None) or 0),
        total_tokens=int(getattr(row, "total_tokens", None) or 0),
        cache_read_tokens=int(getattr(row, "cache_read_tokens", None) or 0),
        cache_creation_tokens=int(getattr(row, "cache_creation_tokens", None) or 0),
        cost=(
            float(row.estimated_cost)
            if getattr(row, "estimated_cost", None) is not None
            else None
        ),
        session_id=meta.get("source_session_id"),
    )
