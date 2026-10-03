"""Deposit and list session artifacts on behalf of an agent or a user.

The REST route (``api/endpoints/runtime_session_artifacts.py``) stays thin
and calls :func:`deposit` and :func:`list_artifacts`. The MCP
``deposit_artifact`` tool (#1081) and the CLI (#1089) reuse the same entry
points. Text indexing (#1082) registers a callback in :data:`ON_STORED`.

Content arrives as an MCP ``ContentBlock`` (or raw file bytes) and is turned
into an :class:`~preloop.services.artifact_shapes.ArtifactPayload` by the
#1078 shape module; storage rules (kinds, caps, media allowlist, labels,
legal hold, budget) live in ``crud_runtime_session_artifact.store`` (#1079).
"""

from __future__ import annotations

import base64
import binascii
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_runtime_session, crud_runtime_session_activity
from preloop.models.crud import runtime_session_artifact as crud_artifact
from preloop.schemas.runtime_session_artifact import (
    McpResourceLink,
    RuntimeSessionArtifactListOut,
    RuntimeSessionArtifactOut,
)
from preloop.services import artifact_shapes as shapes
from preloop.services.account_realtime import (
    ACCOUNT_TOPIC_RUNTIME_SESSIONS,
    build_account_event,
    emit_account_event,
)
from preloop.services.artifact_media import ARTIFACT_KINDS
from preloop.services.model_gateway_auth import (
    ModelGatewayAuthContext,
    resolve_managed_agent_id_for_context,
)

logger = logging.getLogger(__name__)

PRODUCER_DEPOSIT_API = "deposit_api"
SOURCE_DEPOSIT = "deposit"
LIST_LIMIT_MAX = 200
LIST_LIMIT_DEFAULT = 50
IDEMPOTENCY_KEY_MAX_CHARS = 255
_REQUEST_ID_KEY = "deposit_request_id"

ERROR_AUDIO_DISABLED = "artifact_audio_storage_disabled"
ERROR_TOO_LARGE = "artifact_too_large"
ERROR_CONTENT_REQUIRED = "artifact_content_required"
ERROR_ACTIVITY_INVALID = "artifact_activity_invalid"
ERROR_IDEMPOTENCY_KEY_INVALID = "artifact_idempotency_key_invalid"
ERROR_LABEL_FILTER_INVALID = "artifact_label_filter_invalid"
ERROR_CURSOR_INVALID = "artifact_cursor_invalid"
ERROR_KIND_INVALID = "artifact_kind_invalid"

_STATUS_BY_CODE: dict[str, int] = {
    ERROR_TOO_LARGE: 413,
    shapes.ERROR_BLOCK_TOO_LARGE: 413,
    "artifact_media_type_invalid": 415,
    "artifact_content_mismatch": 415,
    ERROR_AUDIO_DISABLED: 409,
    "storage_budget_exhausted": 507,
}

OnStored = Callable[[Session, models.RuntimeSessionArtifact], None]


def index_text_on_stored(db: Session, artifact: models.RuntimeSessionArtifact) -> None:
    """Index the artifact into session search (#1082), synchronously.

    Extraction is bounded by the per-kind cap and the 1 MiB text cap.
    """
    from preloop.services.session_search_index import index_artifact_text

    index_artifact_text(db, artifact, commit=True)


ON_STORED: list[OnStored] = [index_text_on_stored]
"""Callbacks run after a deposit commits, in order, with the new row.

A failing callback is logged and does not fail the deposit. Replays of an
``Idempotency-Key`` do not run them again.
"""


class ArtifactDepositError(Exception):
    """A deposit or list request refused with a stable error code."""

    def __init__(self, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code

    @classmethod
    def from_code(cls, code: str) -> ArtifactDepositError:
        """Map a storage or shape ``ValueError`` code to its HTTP status."""
        if code == shapes.ERROR_BLOCK_TOO_LARGE:
            code = ERROR_TOO_LARGE
        return cls(_STATUS_BY_CODE.get(code, 422), code)


@dataclass(frozen=True)
class DepositResult:
    """Outcome of :func:`deposit`."""

    artifact: RuntimeSessionArtifactOut
    replayed: bool


def max_request_bytes() -> int:
    """Largest accepted request body: the largest kind cap plus 1 MiB."""
    return max(crud_artifact._max_bytes(kind) for kind in ARTIFACT_KINDS) + 1024**2


def audio_storage_enabled(account: Any) -> bool:
    """Whether the account stores audio artifacts.

    Always False until #1102 adds the per-account opt-in setting.
    """
    return False


def artifact_uri(runtime_session_id: Any, artifact_id: Any) -> str:
    """Path of the existing byte route for one artifact."""
    return f"/api/v1/runtime-sessions/{runtime_session_id}/artifacts/{artifact_id}"


def describe(artifact: models.RuntimeSessionArtifact) -> RuntimeSessionArtifactOut:
    """Build the public descriptor, with an MCP ``ResourceLink`` to the bytes."""
    uri = artifact_uri(artifact.runtime_session_id, artifact.id)
    meta = {
        shapes.META_KEY: {
            "artifact_id": str(artifact.id),
            "kind": artifact.kind,
            "labels": dict(artifact.labels or {}),
            "sha256": artifact.sha256,
            "producer": artifact.producer,
        }
    }
    return RuntimeSessionArtifactOut(
        id=str(artifact.id),
        runtime_session_id=str(artifact.runtime_session_id),
        activity_id=_str_or_none(artifact.activity_id),
        kind=artifact.kind,
        name=artifact.name,
        content_type=artifact.content_type,
        size_bytes=int(artifact.size_bytes),
        sha256=artifact.sha256,
        labels=dict(artifact.labels or {}),
        producer=artifact.producer,
        agent_id=_str_or_none(artifact.agent_id),
        tool_name=artifact.tool_name,
        parent_artifact_id=_str_or_none(artifact.parent_artifact_id),
        text_status=artifact.text_status,
        availability=artifact.availability,
        legal_hold=bool(artifact.legal_hold),
        created_at=artifact.created_at,
        content_block=McpResourceLink(
            uri=uri,
            name=artifact.name or str(artifact.id),
            mimeType=artifact.content_type,
            size=int(artifact.size_bytes),
            meta=meta,
        ),
    )


def payload_from_content_block(
    block: Mapping[str, Any], *, kind: str | None
) -> shapes.ArtifactPayload:
    """Parse one MCP ``ContentBlock`` into a payload that carries bytes.

    Raises:
        ArtifactDepositError: 413 when the decoded content is over every
            cap, 422 for an unsupported block or one without bytes.
    """
    try:
        payload = shapes.from_mcp_content_block(
            block, kind=kind, max_bytes=max_request_bytes()
        )
    except ValueError as exc:
        raise ArtifactDepositError.from_code(str(exc)) from None
    if payload.data is None and payload.text is None:
        raise ArtifactDepositError(422, ERROR_CONTENT_REQUIRED)
    return payload


def payload_from_file(
    data: bytes, *, content_type: str | None, kind: str | None, name: str | None
) -> shapes.ArtifactPayload:
    """Build a payload from an uploaded file part."""
    content_type = (content_type or "").strip() or None
    return shapes.make_payload(
        kind=kind or (shapes.infer_kind(content_type) if content_type else None),
        name=name,
        content_type=content_type,
        data=data,
    )


def require_session(
    db: Session, *, auth: ModelGatewayAuthContext, runtime_session_id: str
) -> models.RuntimeSession:
    """Return the session a credential may use, or refuse.

    A credential pinned to another session gets 403; a session outside the
    credential's account (or a malformed id) gets 404.
    """
    canonical = _uuid_or_none(runtime_session_id)
    if canonical is None:
        raise ArtifactDepositError(404, "runtime_session_not_found")
    pinned = auth.runtime_session_id
    if pinned is not None and pinned.strip().lower() != str(canonical):
        raise ArtifactDepositError(403, "runtime_session_binding_mismatch")
    session = crud_runtime_session.get_account_session(
        db, account_id=str(auth.account_id), runtime_session_id=str(canonical)
    )
    if session is None:
        raise ArtifactDepositError(404, "runtime_session_not_found")
    return session


def deposit(
    db: Session,
    *,
    auth: ModelGatewayAuthContext,
    runtime_session_id: str,
    payload: shapes.ArtifactPayload,
    name: str | None,
    labels: dict[str, Any] | None = None,
    activity_id: str | None = None,
    parent_artifact_id: str | None = None,
    tool_name: str | None = None,
    idempotency_key: str | None = None,
    producer: str = PRODUCER_DEPOSIT_API,
) -> DepositResult:
    """Store one artifact and its timeline row, then notify listeners.

    The artifact row and an ``activity_type="artifact"`` timeline row are
    committed together. A repeated ``idempotency_key`` (per session and kind)
    returns the stored artifact and writes nothing.

    Raises:
        ArtifactDepositError: with the HTTP status and stable code.
    """
    session = require_session(db, auth=auth, runtime_session_id=runtime_session_id)
    if idempotency_key is not None and (
        not idempotency_key.strip() or len(idempotency_key) > IDEMPOTENCY_KEY_MAX_CHARS
    ):
        raise ArtifactDepositError(422, ERROR_IDEMPOTENCY_KEY_INVALID)
    kind = payload.kind
    if kind == shapes.KIND_AUDIO:
        account = db.get(models.Account, auth.account_id)
        if not audio_storage_enabled(account):
            raise ArtifactDepositError(409, ERROR_AUDIO_DISABLED)

    activity_uuid = _optional_uuid(activity_id, ERROR_ACTIVITY_INVALID)
    if activity_uuid is not None and not _activity_in_session(
        db, account_id=auth.account_id, session_id=session.id, activity_id=activity_uuid
    ):
        raise ArtifactDepositError(422, ERROR_ACTIVITY_INVALID)
    parent_uuid = _optional_uuid(parent_artifact_id, "artifact_parent_invalid")
    agent_id = _uuid_or_none(resolve_managed_agent_id_for_context(db, auth))
    plaintext = payload.data if payload.data is not None else payload.text.encode()
    # Labels from the block's ``_meta["preloop.dev/artifact"]`` count, like
    # its ``name``; top-level ``labels`` win per key.
    merged_labels = {**payload.labels, **(labels or {})}
    request_id = uuid.uuid4().hex

    try:
        artifact = crud_artifact.store(
            db,
            account_id=auth.account_id,
            runtime_session_id=session.id,
            kind=kind,
            source=SOURCE_DEPOSIT,
            source_ref=idempotency_key,
            content_type=payload.content_type,
            plaintext=plaintext,
            manifest={_REQUEST_ID_KEY: request_id},
            activity_id=activity_uuid,
            name=name or payload.name,
            labels=merged_labels,
            producer=producer,
            agent_id=agent_id,
            tool_name=tool_name,
            parent_artifact_id=parent_uuid,
            commit=False,
        )
    except ValueError as exc:
        db.rollback()
        raise ArtifactDepositError.from_code(str(exc)) from None

    if (artifact.manifest or {}).get(_REQUEST_ID_KEY) != request_id:
        db.rollback()
        return DepositResult(artifact=describe(artifact), replayed=True)

    try:
        activity = crud_runtime_session_activity.log_artifact(
            db,
            account_id=auth.account_id,
            runtime_session_id=session.id,
            api_key_id=auth.api_key_id,
            title=f"{artifact.kind} {artifact.name}",
            producer=producer,
            tool_name=tool_name,
            metadata={"artifact": _timeline_metadata(artifact)},
            commit=False,
        )
        if artifact.activity_id is None:
            artifact.activity_id = activity.id
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(artifact)
    _emit_session_updated(db, account_id=auth.account_id, session=session)
    _run_on_stored(db, artifact)
    return DepositResult(artifact=describe(artifact), replayed=False)


def parse_label_filters(values: Iterable[str]) -> dict[str, Any]:
    """Turn ``key:value`` query values into a JSONB containment filter.

    Repeated ``tags:x`` values collect into ``{"tags": [...]}``.

    Raises:
        ValueError: ``artifact_label_filter_invalid`` for a malformed value.
    """
    labels: dict[str, Any] = {}
    for raw in values:
        key, sep, value = str(raw).partition(":")
        if not sep or not key or not value:
            raise ValueError(ERROR_LABEL_FILTER_INVALID)
        if key == "tags":
            labels.setdefault("tags", []).append(value)
        else:
            labels[key] = value
    return labels


def encode_cursor(artifact: models.RuntimeSessionArtifact) -> str:
    """Opaque keyset cursor for the row after which the next page starts."""
    raw = f"{artifact.created_at.isoformat()}|{artifact.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Inverse of :func:`encode_cursor`.

    Raises:
        ValueError: ``artifact_cursor_invalid``.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        stamp, _, ident = (
            base64.urlsafe_b64decode(padded.encode()).decode().partition("|")
        )
        return datetime.fromisoformat(stamp), UUID(ident)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ValueError(ERROR_CURSOR_INVALID) from None


def list_artifacts(
    db: Session,
    *,
    account_id: Any,
    runtime_session_id: Any,
    kind: str | None = None,
    labels: Iterable[str] = (),
    limit: int = LIST_LIMIT_DEFAULT,
    cursor: str | None = None,
) -> RuntimeSessionArtifactListOut:
    """One page of a session's artifacts, newest first.

    Raises:
        ArtifactDepositError: 422 for a bad kind, label filter, cursor or
            limit. An unknown kind is an error, not an empty page, so a typo
            is not mistaken for "no artifacts of that kind".
    """
    if not 1 <= limit <= LIST_LIMIT_MAX:
        raise ArtifactDepositError(422, "artifact_limit_invalid")
    if kind is not None and kind not in ARTIFACT_KINDS:
        raise ArtifactDepositError(422, ERROR_KIND_INVALID)
    try:
        label_filter = parse_label_filters(labels)
        before = decode_cursor(cursor) if cursor else None
    except ValueError as exc:
        raise ArtifactDepositError(422, str(exc)) from None
    rows = crud_artifact.list_page_for_session(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        limit=limit + 1,
        kind=kind,
        labels=label_filter,
        before=before,
    )
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1]) if len(rows) > limit else None
    return RuntimeSessionArtifactListOut(
        items=[describe(row) for row in page], next_cursor=next_cursor
    )


def _timeline_metadata(artifact: models.RuntimeSessionArtifact) -> dict[str, Any]:
    return {
        "id": str(artifact.id),
        "kind": artifact.kind,
        "name": artifact.name,
        "content_type": artifact.content_type,
        "size_bytes": int(artifact.size_bytes),
        "labels": dict(artifact.labels or {}),
        "producer": artifact.producer,
    }


def _activity_in_session(
    db: Session, *, account_id: Any, session_id: Any, activity_id: UUID
) -> bool:
    row = db.get(models.RuntimeSessionActivity, activity_id)
    return (
        row is not None
        and str(row.account_id) == str(account_id)
        and str(row.runtime_session_id) == str(session_id)
    )


def _run_on_stored(db: Session, artifact: models.RuntimeSessionArtifact) -> None:
    for callback in list(ON_STORED):
        try:
            callback(db, artifact)
        except Exception:
            logger.exception("on_stored callback failed for artifact %s", artifact.id)
            db.rollback()


def _emit_session_updated(
    db: Session, *, account_id: Any, session: models.RuntimeSession
) -> None:
    """Publish ``runtime_session_updated`` the way browser steps do."""
    db.refresh(session)
    last_activity_at = (
        session.last_activity_at.isoformat() if session.last_activity_at else None
    )
    try:
        emit_account_event(
            build_account_event(
                account_id=str(account_id),
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
                    "activity_type": "artifact",
                },
                runtime_session_id=session.id,
            )
        )
    except Exception:
        logger.exception("Failed to emit runtime_session_updated for %s", session.id)


def _uuid_or_none(value: Any) -> UUID | None:
    if value is None or value == "":
        return None
    try:
        return UUID(str(value).strip())
    except ValueError:
        return None


def _optional_uuid(value: str | None, code: str) -> UUID | None:
    if value is None:
        return None
    parsed = _uuid_or_none(value)
    if parsed is None:
        raise ArtifactDepositError(422, code)
    return parsed


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)
