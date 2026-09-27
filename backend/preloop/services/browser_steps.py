"""Ingest browser step observations onto a runtime session.

Steps are records of what an agent reports. Writing one does not approve
a tool call, dispatch work, or assert that the browser reached a state.
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import (
    crud_runtime_session,
    crud_runtime_session_activity,
    crud_runtime_session_artifact,
)
from preloop.schemas.browser_step import (
    ERROR_SCREENSHOT_INVALID,
    ERROR_SCREENSHOT_TOO_LARGE,
    ERROR_STORAGE_BUDGET_EXHAUSTED,
    BrowserScreenshotIn,
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


def _is_declared_image(content_type: str, data: bytes) -> bool:
    """Return True when ``data`` starts with the signature of ``content_type``.

    The artifact route serves stored bytes with the stored media type, so a
    payload that is not the declared image is refused at ingest.
    """
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def decode_screenshot(
    screenshot: BrowserScreenshotIn,
) -> tuple[bytes | None, str | None]:
    """Decode one step's screenshot, or return the row's error code.

    The encoded length is checked before decoding so an oversized payload
    is refused without allocating its decoded copy.

    Args:
        screenshot: Base64 payload and declared media type.

    Returns:
        ``(data, None)`` for an acceptable image. ``(None, code)`` where
        ``code`` is ``screenshot_too_large`` when the decoded size exceeds
        ``runtime_session_screenshot_max_bytes``, or ``screenshot_invalid``
        for bad base64, an empty payload, or bytes that are not the
        declared image type.
    """
    max_bytes = int(settings.runtime_session_screenshot_max_bytes)
    encoded = screenshot.data_base64.strip()
    # Base64 carries 3 bytes per 4 characters. Anything longer than the
    # encoding of max_bytes plus padding cannot decode to an allowed size.
    if len(encoded) > 4 * ((max_bytes + 2) // 3):
        return None, ERROR_SCREENSHOT_TOO_LARGE
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None, ERROR_SCREENSHOT_INVALID
    if len(data) > max_bytes:
        return None, ERROR_SCREENSHOT_TOO_LARGE
    if not data or not _is_declared_image(screenshot.content_type, data):
        return None, ERROR_SCREENSHOT_INVALID
    return data, None


def attach_screenshot(
    db: Session,
    *,
    account_id: UUID,
    runtime_session_id: UUID,
    activity: models.RuntimeSessionActivity,
    content_type: str,
    data: bytes,
    source: str,
    source_ref: str,
) -> models.RuntimeSessionArtifact:
    """Store a screenshot for a browser step and point the step at it.

    Writes are flushed, not committed. The step's
    ``metadata.screenshot`` gets the artifact id, availability, media type
    and size so the console can render or mark the image without a join.

    Args:
        db: Database session.
        account_id: Owning account.
        runtime_session_id: Session the step belongs to.
        activity: The ``browser_step`` row the image illustrates.
        content_type: Media type of ``data``.
        data: Decoded image bytes.
        source: Step source, for example ``playwright_mcp``.
        source_ref: Step ``source_step_id``. Keeps the artifact idempotent.

    Returns:
        The stored artifact, or the existing one for this source reference.

    Raises:
        ValueError: ``storage_budget_exhausted`` from
            :func:`crud_runtime_session_artifact.store` when the account
            budget cannot fit the image.
    """
    metadata = dict(activity.metadata_ or {})
    artifact = crud_runtime_session_artifact.store(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        kind="screenshot",
        source=source,
        source_ref=source_ref,
        content_type=content_type,
        plaintext=data,
        manifest={
            "step_index": metadata.get("step_index"),
            "action": metadata.get("action"),
        },
        activity_id=activity.id,
        commit=False,
    )
    # Reassign the dict: in-place edits of a JSONB value are not tracked.
    activity.metadata_ = {
        **metadata,
        "screenshot": {
            "artifact_id": str(artifact.id),
            "availability": artifact.availability,
            "content_type": artifact.content_type,
            "size_bytes": artifact.size_bytes,
        },
    }
    db.flush()
    return artifact


def enforce_session_screenshot_bound(
    db: Session,
    *,
    account_id: UUID,
    runtime_session_id: UUID,
) -> list[UUID]:
    """Evict the oldest screenshots above the per-session bound.

    Age follows the browser step ``timestamp`` the screenshot illustrates,
    then the artifact ``created_at``. An evicted screenshot keeps its
    artifact row and metadata; its ciphertext is cleared and the step's
    ``metadata.screenshot.availability`` becomes ``evicted``. Artifacts
    under legal hold, or in a session under legal hold, are never evicted,
    so a held session can stay above the bound. Writes are flushed, not
    committed.

    Args:
        db: Database session.
        account_id: Owning account.
        runtime_session_id: Session to bound.

    Returns:
        Ids of the artifacts that were evicted, oldest first.
    """
    limit = int(settings.runtime_session_screenshots_per_session_max)
    artifact = models.RuntimeSessionArtifact
    available = (
        db.query(func.count(artifact.id))
        .filter(
            artifact.account_id == account_id,
            artifact.runtime_session_id == runtime_session_id,
            artifact.kind == "screenshot",
            artifact.availability == "available",
        )
        .scalar()
    )
    excess = int(available or 0) - limit
    if excess <= 0:
        return []
    session_held = (
        db.query(models.RuntimeSession.legal_hold)
        .filter(
            models.RuntimeSession.id == runtime_session_id,
            models.RuntimeSession.account_id == account_id,
        )
        .scalar()
    )
    if session_held:
        return []
    activity = models.RuntimeSessionActivity
    victims = (
        db.query(artifact.id, artifact.activity_id)
        .outerjoin(activity, activity.id == artifact.activity_id)
        .filter(
            artifact.account_id == account_id,
            artifact.runtime_session_id == runtime_session_id,
            artifact.kind == "screenshot",
            artifact.availability == "available",
            artifact.legal_hold.is_(False),
        )
        .order_by(
            activity.timestamp.asc().nulls_first(),
            artifact.created_at.asc(),
            artifact.id.asc(),
        )
        .limit(excess)
        .all()
    )
    evicted: list[UUID] = []
    for artifact_id, activity_id in victims:
        cleared = crud_runtime_session_artifact.mark_unavailable(
            db,
            account_id=account_id,
            artifact_id=artifact_id,
            availability="evicted",
            commit=False,
        )
        if not cleared:
            continue
        if activity_id is not None:
            crud_runtime_session_activity.set_browser_step_screenshot_availability(
                db,
                account_id=account_id,
                activity_id=activity_id,
                availability="evicted",
                commit=False,
            )
        evicted.append(artifact_id)
    return evicted


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
    written. A step may carry a screenshot, which is stored as an encrypted
    ``screenshot`` artifact for created rows only; a repeated step never
    stores a second image. Once the batch is attached, the session's
    available screenshots are bounded by
    ``runtime_session_screenshots_per_session_max`` and the oldest lose
    their bytes. Valid rows are inserted in one transaction. Created rows are
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
    session_uuid = UUID(str(runtime_session_id).strip())

    accepted = 0
    duplicates = 0
    rejected: list[dict[str, Any]] = []
    created_rows: list[Any] = []
    attached = False
    for index, step in enumerate(batch.steps):
        error = browser_step_extra_error(step.extra)
        if error is not None:
            rejected.append({"index": index, "error": error})
            continue
        image: bytes | None = None
        if step.screenshot is not None:
            image, error = decode_screenshot(step.screenshot)
            if error is not None:
                rejected.append({"index": index, "error": error})
                continue
        if image is None:
            row, created = crud_runtime_session_activity.log_browser_step(
                db,
                account_id=auth.account_id,
                runtime_session_id=runtime_session_id,
                api_key_id=auth.api_key_id,
                step=step,
                commit=False,
            )
        else:
            # The step and its image land together or not at all, so a full
            # account budget rejects this row and keeps its siblings.
            try:
                with db.begin_nested():
                    row, created = crud_runtime_session_activity.log_browser_step(
                        db,
                        account_id=auth.account_id,
                        runtime_session_id=runtime_session_id,
                        api_key_id=auth.api_key_id,
                        step=step,
                        commit=False,
                    )
                    if created:
                        attach_screenshot(
                            db,
                            account_id=auth.account_id,
                            runtime_session_id=session_uuid,
                            activity=row,
                            content_type=step.screenshot.content_type,
                            data=image,
                            source=step.source,
                            source_ref=step.source_step_id,
                        )
                        attached = True
            except ValueError as exc:
                if str(exc) != ERROR_STORAGE_BUDGET_EXHAUSTED:
                    raise
                rejected.append(
                    {"index": index, "error": ERROR_STORAGE_BUDGET_EXHAUSTED}
                )
                continue
        if created:
            accepted += 1
            created_rows.append(row)
        else:
            duplicates += 1

    if attached:
        enforce_session_screenshot_bound(
            db,
            account_id=auth.account_id,
            runtime_session_id=session_uuid,
        )

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
