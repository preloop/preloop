"""Per-account storage budget for runtime-session screenshots and recordings.

A new artifact that would exceed the account budget evicts the oldest unheld
bytes first. Held artifacts are never evicted. There is no per-account
override of the budget yet (global setting only).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, load_only

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import (
    crud_runtime_session,
    crud_runtime_session_activity,
    crud_runtime_session_artifact,
)
from preloop.services.account_realtime import (
    ACCOUNT_TOPIC_RUNTIME_SESSIONS,
    build_account_event,
    emit_account_event,
)

logger = logging.getLogger(__name__)


def enforce_account_budget(
    db: Session,
    *,
    account_id: UUID,
    incoming_bytes: int,
) -> list[models.RuntimeSessionArtifact]:
    """Evict oldest unheld artifacts until ``incoming_bytes`` fits.

    Recordings are chosen before screenshots, then oldest ``created_at``.
    Alphabetical ``kind DESC`` would evict screenshots first (``screenshot``
    sorts after ``recording``), so the query ranks ``recording`` ahead
    explicitly. Each eviction clears ciphertext, writes an
    ``artifact_evicted`` activity, and when the evicted artifact is a
    screenshot with an ``activity_id`` updates that browser step's
    ``metadata.screenshot.availability``. Recording evictions do not stamp
    the screenshot field. Realtime events are not published here: a failed
    store rolls this transaction back, so :func:`notify_evicted` runs only
    after ``store`` commits.

    Args:
        db: Database session. Eviction writes are flushed, not committed.
        account_id: Account whose budget is enforced.
        incoming_bytes: Plaintext size about to be stored.

    Returns:
        Artifacts whose ciphertext was cleared, oldest eviction first.

    Raises:
        ValueError: ``storage_budget_exhausted`` when nothing unheld remains
            and the budget is still exceeded. Nothing is published.
    """
    budget = int(settings.runtime_session_artifact_account_max_bytes)
    used = crud_runtime_session_artifact.account_bytes(db, account_id=account_id)
    if used + incoming_bytes <= budget:
        return []
    evicted: list[models.RuntimeSessionArtifact] = []
    for victim in _evictable(db, account_id=account_id):
        if used + incoming_bytes <= budget:
            break
        cleared = crud_runtime_session_artifact.mark_unavailable(
            db,
            account_id=account_id,
            artifact_id=victim.id,
            availability="evicted",
            commit=False,
        )
        if not cleared:
            continue
        used -= int(victim.size_bytes)
        crud_runtime_session_activity.log_artifact_evicted(
            db,
            account_id=account_id,
            runtime_session_id=victim.runtime_session_id,
            artifact_id=victim.id,
            kind=victim.kind,
            commit=False,
        )
        if victim.kind == "screenshot" and victim.activity_id is not None:
            crud_runtime_session_activity.set_browser_step_screenshot_availability(
                db,
                account_id=account_id,
                activity_id=victim.activity_id,
                availability="evicted",
                commit=False,
            )
        evicted.append(victim)
    if used + incoming_bytes > budget:
        raise ValueError("storage_budget_exhausted")
    return evicted


def notify_evicted(
    db: Session,
    *,
    account_id: UUID,
    artifacts: list[models.RuntimeSessionArtifact],
) -> None:
    """Publish one ``runtime_session_updated`` per session that was evicted.

    Call this only after the eviction transaction has committed.

    Args:
        db: Database session.
        account_id: Account that owns the artifacts.
        artifacts: Rows cleared by :func:`enforce_account_budget`.
    """
    session_ids: list[UUID] = []
    for artifact in artifacts:
        if artifact.runtime_session_id not in session_ids:
            session_ids.append(artifact.runtime_session_id)
    _emit_updated(db, account_id=account_id, session_ids=session_ids)


def account_usage(db: Session, *, account_id: UUID) -> dict[str, Any]:
    """Return the account's session-artifact usage against the budget.

    ``evicted_count_30d`` counts rows stored in the last 30 days whose
    availability is ``evicted``. The table has no eviction timestamp, so the
    count follows ``created_at``.

    Args:
        db: Database session.
        account_id: Account to total.

    Returns:
        ``used_bytes``, ``budget_bytes``, ``by_kind`` plaintext bytes, and
        ``evicted_count_30d``.
    """
    since = datetime.now(UTC) - timedelta(days=30)
    evicted = (
        db.query(func.count(models.RuntimeSessionArtifact.id))
        .filter(
            models.RuntimeSessionArtifact.account_id == account_id,
            models.RuntimeSessionArtifact.availability == "evicted",
            models.RuntimeSessionArtifact.created_at >= since,
        )
        .scalar()
    )
    return {
        "used_bytes": crud_runtime_session_artifact.account_bytes(
            db, account_id=account_id
        ),
        "budget_bytes": int(settings.runtime_session_artifact_account_max_bytes),
        "by_kind": {
            "screenshot": crud_runtime_session_artifact.account_bytes(
                db, account_id=account_id, kind="screenshot"
            ),
            "recording": crud_runtime_session_artifact.account_bytes(
                db, account_id=account_id, kind="recording"
            ),
        },
        "evicted_count_30d": int(evicted or 0),
    }


def _evictable(db: Session, *, account_id: UUID) -> list[models.RuntimeSessionArtifact]:
    """Available unheld artifacts, recordings first, then oldest."""
    session_held = (
        select(models.RuntimeSession.id)
        .where(
            models.RuntimeSession.id
            == models.RuntimeSessionArtifact.runtime_session_id,
            models.RuntimeSession.legal_hold.is_(True),
        )
        .exists()
    )
    return (
        db.query(models.RuntimeSessionArtifact)
        .options(
            load_only(
                models.RuntimeSessionArtifact.id,
                models.RuntimeSessionArtifact.runtime_session_id,
                models.RuntimeSessionArtifact.activity_id,
                models.RuntimeSessionArtifact.kind,
                models.RuntimeSessionArtifact.size_bytes,
            )
        )
        .filter(
            models.RuntimeSessionArtifact.account_id == account_id,
            models.RuntimeSessionArtifact.availability == "available",
            models.RuntimeSessionArtifact.legal_hold.is_(False),
            ~session_held,
        )
        .order_by(
            case(
                (models.RuntimeSessionArtifact.kind == "recording", 0),
                else_=1,
            ),
            models.RuntimeSessionArtifact.created_at.asc(),
            models.RuntimeSessionArtifact.id.asc(),
        )
        .all()
    )


def _emit_updated(db: Session, *, account_id: UUID, session_ids: list[UUID]) -> None:
    """Publish one session update per affected session."""
    for runtime_session_id in session_ids:
        session = crud_runtime_session.get_account_session(
            db,
            account_id=str(account_id),
            runtime_session_id=str(runtime_session_id),
        )
        last_activity_at = None
        payload: dict[str, Any] = {
            "runtime_session_id": str(runtime_session_id),
            "activity_type": "artifact_evicted",
        }
        if session is not None:
            if session.last_activity_at is not None:
                last_activity_at = session.last_activity_at.isoformat()
            payload.update(
                {
                    "session_source_type": session.session_source_type,
                    "session_source_id": session.session_source_id,
                    "session_reference": session.session_reference,
                    "runtime_principal_type": session.runtime_principal_type,
                    "runtime_principal_id": session.runtime_principal_id,
                    "runtime_principal_name": session.runtime_principal_name,
                    "last_activity_at": last_activity_at,
                }
            )
        try:
            emit_account_event(
                build_account_event(
                    account_id=str(account_id),
                    topic=ACCOUNT_TOPIC_RUNTIME_SESSIONS,
                    event_type="runtime_session_updated",
                    payload=payload,
                    runtime_session_id=runtime_session_id,
                )
            )
        except Exception:
            logger.exception(
                "Failed to emit runtime_session_updated for session %s",
                runtime_session_id,
            )
