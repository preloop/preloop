"""Ingest browser step observations onto a runtime session.

Steps are records of what an agent reports. Writing one does not approve
a tool call, dispatch work, or assert that the browser reached a state.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_runtime_session,
    crud_runtime_session_activity,
)
from preloop.schemas.browser_step import (
    BrowserStepBatchIn,
    BrowserStepBatchOut,
    browser_step_extra_error,
)
from preloop.services.account_realtime import (
    ACCOUNT_TOPIC_RUNTIME_SESSIONS,
    build_account_event,
    emit_account_event,
)
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.session_search_index import index_browser_step, request_embedding

logger = logging.getLogger(__name__)


class BrowserStepsError(Exception):
    """A batch that cannot be applied to the named session.

    Attributes:
        status_code: HTTP status the endpoint should return.
        detail: Safe message for the client. Never names another account.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def ingest_batch(
    db: Session,
    *,
    auth: ModelGatewayAuthContext,
    runtime_session_id: str,
    batch: BrowserStepBatchIn,
) -> BrowserStepBatchOut:
    """Store a batch of browser steps on one session.

    Rows that fail validation are listed in ``rejected`` and are not
    written. Valid rows are inserted in one transaction. Created rows are
    indexed, and one ``runtime_session_updated`` event is emitted when at
    least one row was created. A session that has ended is accepted so an
    adapter can flush after the run.

    Args:
        db: Database session.
        auth: Authenticated agent credential.
        runtime_session_id: Session named in the request path.
        batch: Steps to store.

    Returns:
        Counts of accepted and duplicate steps, plus per-row rejections.

    Raises:
        BrowserStepsError: 403 when the credential is pinned to a different
            session, 404 when the session is not in the credential's account.
    """
    _require_session(db, auth=auth, runtime_session_id=runtime_session_id)

    accepted = 0
    duplicates = 0
    rejected: list[dict[str, Any]] = []
    created_rows: list[Any] = []
    for index, step in enumerate(batch.steps):
        error = browser_step_extra_error(step.extra)
        if error is not None:
            rejected.append({"index": index, "error": error})
            continue
        row, created = crud_runtime_session_activity.log_browser_step(
            db,
            account_id=auth.account_id,
            runtime_session_id=runtime_session_id,
            api_key_id=auth.api_key_id,
            step=step,
            commit=False,
        )
        if created:
            accepted += 1
            created_rows.append(row)
        else:
            duplicates += 1

    for row in created_rows:
        index_browser_step(db, activity=row, commit=False)

    if created_rows:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
        _emit_session_updated(
            db,
            auth=auth,
            runtime_session_id=runtime_session_id,
        )
        request_embedding(auth.account_id)

    return BrowserStepBatchOut(
        accepted=accepted,
        duplicates=duplicates,
        rejected=rejected,
    )


def _require_session(
    db: Session,
    *,
    auth: ModelGatewayAuthContext,
    runtime_session_id: str,
) -> None:
    """Reject a path the credential may not write, or that is not owned."""
    try:
        canonical = str(UUID(str(runtime_session_id).strip()))
    except ValueError:
        raise BrowserStepsError(404, "Runtime session not found") from None
    pinned = auth.runtime_session_id
    if pinned is not None and pinned.lower() != canonical.lower():
        raise BrowserStepsError(
            403,
            "Credential is bound to a different runtime session",
        )
    session = crud_runtime_session.get_account_session(
        db,
        account_id=str(auth.account_id),
        runtime_session_id=str(runtime_session_id),
    )
    if session is None:
        raise BrowserStepsError(404, "Runtime session not found")


def _emit_session_updated(
    db: Session,
    *,
    auth: ModelGatewayAuthContext,
    runtime_session_id: str,
) -> None:
    """Publish one session update after a batch created at least one row."""
    session = crud_runtime_session.get_account_session(
        db,
        account_id=str(auth.account_id),
        runtime_session_id=str(runtime_session_id),
    )
    if session is None:
        return
    last_activity_at = (
        session.last_activity_at.isoformat() if session.last_activity_at else None
    )
    try:
        emit_account_event(
            build_account_event(
                account_id=str(auth.account_id),
                topic=ACCOUNT_TOPIC_RUNTIME_SESSIONS,
                event_type="runtime_session_updated",
                payload={
                    "runtime_session_id": str(session.id),
                    "session_source_type": session.session_source_type,
                    "session_source_id": session.session_source_id,
                    "session_reference": session.session_reference,
                    "runtime_principal_type": session.runtime_principal_type,
                    "runtime_principal_id": session.runtime_principal_id,
                    "runtime_principal_name": session.runtime_principal_name,
                    "last_activity_at": last_activity_at,
                    "activity_type": "browser_step",
                },
                runtime_session_id=session.id,
            )
        )
    except Exception:
        logger.exception(
            "Failed to emit runtime_session_updated for session %s",
            runtime_session_id,
        )
