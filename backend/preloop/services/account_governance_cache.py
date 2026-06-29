"""Short-lived cache for account subject-governance meta_data.

Gateway context optimization reads ``account.meta_data`` on every model
request. Most accounts never configure subject governance; this module caches
negative lookups and populated stores for a few seconds so the hot path can
skip repeated DB round-trips.
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.models.crud import crud_account
from preloop.services.subject_governance import normalize_subject_governance_store

_NEGATIVE = object()
_CACHE: dict[str, tuple[Any, float]] = {}
_TTL_SECONDS = 30.0
_LOCK = Lock()


def account_subject_governance_store_is_empty(
    meta_data: Optional[dict[str, Any]],
) -> bool:
    """Return True when no subject has any governance config stored."""
    store = normalize_subject_governance_store(meta_data)
    for bucket in store.values():
        if not isinstance(bucket, dict):
            continue
        for config in bucket.values():
            if isinstance(config, dict) and config:
                return False
    return True


def invalidate_account_governance_cache(account_id: str) -> None:
    """Drop cached governance meta_data for one account after a write."""
    with _LOCK:
        _CACHE.pop(str(account_id), None)


def clear_account_governance_cache() -> None:
    """Clear the entire cache (for tests)."""
    with _LOCK:
        _CACHE.clear()


def get_cached_account_meta_data(
    db: Session, account_id: str
) -> Optional[dict[str, Any]]:
    """Return account meta_data, or ``None`` when governance store is empty.

    ``None`` is the fast-path sentinel: callers can skip context optimization
    entirely. Non-empty stores are cached briefly to avoid per-request DB
    fetches for active accounts.

    Args:
        db: Database session.
        account_id: Owning account id.

    Returns:
        Account ``meta_data`` dict when any subject governance exists, else
        ``None``.
    """
    key = str(account_id)
    now = time.monotonic()
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is not None:
            value, expires_at = entry
            if now < expires_at:
                return None if value is _NEGATIVE else value
            _CACHE.pop(key, None)

    account = crud_account.get(db, id=account_id)
    meta_data = (
        account.meta_data
        if account is not None and isinstance(account.meta_data, dict)
        else {}
    )

    if account_subject_governance_store_is_empty(meta_data):
        cached: Any = _NEGATIVE
        result: Optional[dict[str, Any]] = None
    else:
        cached = meta_data
        result = meta_data

    with _LOCK:
        _CACHE[key] = (cached, now + _TTL_SECONDS)
    return result
