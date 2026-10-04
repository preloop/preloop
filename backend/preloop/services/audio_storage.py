"""Per-account opt-in for storing raw audio artifacts (#1102).

Raw audio raises budget, storage and biometric questions, so it is not stored
unless an account admin opts in. Transcripts are stored either way. The
setting lives in ``Account.meta_data["artifacts"]``:

``audio_storage_enabled``
    bool, default False. Read by
    :func:`preloop.services.artifact_deposit.audio_storage_enabled`.
``audio_retention_days``
    int, default :data:`DEFAULT_AUDIO_RETENTION_DAYS`. Audio older than this
    is expired by :func:`expire_audio` (bytes dropped, row kept, as budget
    eviction does). Never longer than the account's runtime-session
    retention.
``updated_by_user_id`` / ``updated_at``
    Who last changed the setting and when. The audit log keeps the history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.services import retention_policy

META_KEY = "artifacts"
DEFAULT_AUDIO_RETENTION_DAYS = 30
#: Upper bound when session retention is unlimited.
MAX_AUDIO_RETENTION_DAYS = 3650
AUDIT_ACTION = "artifact_settings_updated"
ERROR_RETENTION_INVALID = "audio_retention_days_invalid"


@dataclass(frozen=True)
class AudioStorageSettings:
    """Resolved audio storage settings for one account."""

    audio_storage_enabled: bool
    audio_retention_days: int
    audio_retention_max_days: int
    updated_by_user_id: Optional[str]
    updated_at: Optional[str]


def _store(meta_data: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    raw = (meta_data or {}).get(META_KEY) if isinstance(meta_data, Mapping) else None
    return raw if isinstance(raw, Mapping) else {}


def max_retention_days(meta_data: Optional[Mapping[str, Any]]) -> int:
    """Longest audio retention allowed: the runtime-session retention."""
    session_days = retention_policy.resolve_retention(
        meta_data, record_class=retention_policy.CLASS_RUNTIME_SESSIONS
    ).days
    if session_days is None or session_days < 0:
        return MAX_AUDIO_RETENTION_DAYS
    return min(int(session_days), MAX_AUDIO_RETENTION_DAYS)


def resolve(meta_data: Optional[Mapping[str, Any]]) -> AudioStorageSettings:
    """Read the setting with defaults; malformed values fall back safely."""
    store = _store(meta_data)
    ceiling = max_retention_days(meta_data)
    days = store.get("audio_retention_days")
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        days = DEFAULT_AUDIO_RETENTION_DAYS
    return AudioStorageSettings(
        # Only a literal True opts in; anything else is the safe default.
        audio_storage_enabled=store.get("audio_storage_enabled") is True,
        audio_retention_days=min(days, ceiling),
        audio_retention_max_days=ceiling,
        updated_by_user_id=store.get("updated_by_user_id"),
        updated_at=store.get("updated_at"),
    )


def is_enabled(account: Any) -> bool:
    """Whether ``account`` stores raw audio. False for anything unexpected."""
    return resolve(getattr(account, "meta_data", None)).audio_storage_enabled


def apply_update(
    meta_data: Optional[Mapping[str, Any]],
    *,
    audio_storage_enabled: Optional[bool],
    audio_retention_days: Optional[int],
    user_id: UUID,
    now: datetime,
) -> dict[str, Any]:
    """Return new ``meta_data`` with the change applied; other keys untouched.

    Raises:
        ValueError: :data:`ERROR_RETENTION_INVALID` when the retention is
            below one day or above the session retention.
    """
    updated = dict(meta_data or {})
    store = dict(_store(meta_data))
    if audio_storage_enabled is None and audio_retention_days is None:
        # Nothing asked to change: leave who/when as they were.
        return updated
    if audio_storage_enabled is not None:
        store["audio_storage_enabled"] = bool(audio_storage_enabled)
    if audio_retention_days is not None:
        if not 1 <= audio_retention_days <= max_retention_days(meta_data):
            raise ValueError(ERROR_RETENTION_INVALID)
        store["audio_retention_days"] = int(audio_retention_days)
    store["updated_by_user_id"] = str(user_id)
    store["updated_at"] = now.isoformat()
    updated[META_KEY] = store
    return updated


def expire_audio(db: Session, *, now: datetime) -> int:
    """Expire audio older than each account's audio retention.

    Bytes are dropped and the row is kept with ``availability="expired"``,
    so the byte route answers 410 ``expired``. Artifacts or sessions under
    legal hold are skipped, as :func:`crud_runtime_session_artifact.cleanup`
    does.

    Returns:
        Rows whose bytes were dropped.
    """
    from preloop.models.crud import crud_runtime_session_artifact

    artifact = models.RuntimeSessionArtifact
    account_ids = db.scalars(
        select(artifact.account_id)
        .where(artifact.kind == "audio", artifact.availability == "available")
        .distinct()
    ).all()
    session_held = (
        select(models.RuntimeSession.id)
        .where(
            models.RuntimeSession.id == artifact.runtime_session_id,
            models.RuntimeSession.legal_hold.is_(True),
        )
        .exists()
    )
    total = 0
    for account_id in account_ids:
        account = db.get(models.Account, account_id)
        days = resolve(getattr(account, "meta_data", None)).audio_retention_days
        cutoff = now - timedelta(days=days)
        total += int(
            db.query(artifact)
            .filter(
                artifact.account_id == account_id,
                artifact.kind == "audio",
                artifact.availability == "available",
                artifact.created_at < cutoff,
                artifact.legal_hold.is_(False),
                ~session_held,
            )
            .update(
                {artifact.ciphertext: None, artifact.availability: "expired"},
                synchronize_session=False,
            )
            or 0
        )
    if total:
        crud_runtime_session_artifact._drop_unavailable_search_chunks(db)
    db.commit()
    return total
