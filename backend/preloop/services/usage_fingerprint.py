"""Row-level import-fingerprint recompute for merge/rekey (#112).

Delegates to the canonical ``event_fingerprint`` landed with usage ingest
(#137) in ``preloop.services.usage_import``. Do not reimplement the payload
assembly here — merge/rekey must stay byte-compatible with ingest dedupe.
"""

from __future__ import annotations

from typing import Any, Optional

from preloop.schemas.usage_import import UsageImportEvent
from preloop.services.usage_import import event_fingerprint


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
    cost = getattr(row, "estimated_cost", None)
    event = UsageImportEvent(
        timestamp=timestamp,
        model=str(getattr(row, "model_alias", None) or ""),
        prompt_tokens=getattr(row, "prompt_tokens", None),
        completion_tokens=getattr(row, "completion_tokens", None),
        total_tokens=getattr(row, "total_tokens", None),
        cache_read_tokens=getattr(row, "cache_read_tokens", None),
        cache_creation_tokens=getattr(row, "cache_creation_tokens", None),
        cost_usd=float(cost) if cost is not None else None,
        session_id=meta.get("source_session_id"),
    )
    return event_fingerprint(
        event, source=str(source), agent_principal_id=agent_principal_id
    )
