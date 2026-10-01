"""Deposit and list session artifacts.

``POST`` takes the agent bearer with the browser-step binding rule: a key
pinned to one session gets 403 for any other. ``GET`` also accepts that
pinned key, or any account credential whose user holds
``view_runtime_sessions``. Bytes stay on the existing
``GET /runtime-sessions/{id}/artifacts/{artifact_id}`` route.

The request body is read here with a hard size bound (largest kind cap plus
1 MiB), checked against ``Content-Length`` before any byte is read and again
while streaming, so an oversized upload is refused without buffering it.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from anyio import from_thread
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from preloop.api.gateway_auth_dependency import (
    authenticate_request,
    get_browser_step_auth_context,
)
from preloop.models import models
from preloop.models.db.session import get_db_session
from preloop.schemas.runtime_session_artifact import (
    ArtifactDepositIn,
    ArtifactDepositMetadata,
    RuntimeSessionArtifactListOut,
    RuntimeSessionArtifactOut,
)
from preloop.services import artifact_deposit
from preloop.services.analytics_history import require_session_history
from preloop.services.artifact_deposit import ArtifactDepositError
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.utils.permissions import require_permission

router = APIRouter()

_ERRORS = {
    401: {"description": "Missing or invalid bearer token"},
    403: {"description": "Credential is bound to a different runtime session"},
    404: {"description": "Runtime session not found in this account"},
}

_DEPOSIT_BODY = {
    "required": True,
    "content": {
        "application/json": {"schema": ArtifactDepositIn.model_json_schema()},
        "multipart/form-data": {
            "schema": {
                "type": "object",
                "required": ["file"],
                "properties": {
                    "file": {"type": "string", "format": "binary"},
                    "metadata": {
                        "type": "string",
                        "description": (
                            "JSON object: "
                            + ", ".join(ArtifactDepositMetadata.model_fields)
                        ),
                    },
                },
            }
        },
    },
}


@router.post(
    "/runtime-sessions/{runtime_session_id}/artifacts",
    status_code=201,
    response_model=RuntimeSessionArtifactOut,
    openapi_extra={"requestBody": _DEPOSIT_BODY},
    responses={
        **_ERRORS,
        409: {"description": "artifact_audio_storage_disabled"},
        413: {"description": "artifact_too_large"},
        415: {
            "description": "artifact_media_type_invalid or artifact_content_mismatch"
        },
        422: {"description": "artifact_labels_invalid and other request errors"},
        507: {"description": "storage_budget_exhausted"},
    },
)
def deposit_runtime_session_artifact(
    runtime_session_id: str,
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db_session),
    auth: ModelGatewayAuthContext = Depends(get_browser_step_auth_context),
) -> Any:
    """Store one artifact on a runtime session and add it to the timeline.

    JSON bodies carry one MCP ``ContentBlock`` in ``content``; multipart
    bodies carry a ``file`` part and an optional JSON ``metadata`` part.
    A repeated ``Idempotency-Key`` returns the stored artifact with
    ``Idempotent-Replayed: true``.
    """
    # Sync handler on the threadpool; the ASGI stream is read on the loop.
    body = from_thread.run(_read_bounded_body, request)
    content_type = request.headers.get("content-type", "").lower()
    try:
        if content_type.startswith("multipart/form-data"):
            fields, payload = from_thread.run(_parse_multipart, request, body)
        else:
            fields, payload = _parse_json(body)
        result = artifact_deposit.deposit(
            db,
            auth=auth,
            runtime_session_id=runtime_session_id,
            payload=payload,
            name=fields.name,
            labels=fields.labels,
            activity_id=fields.activity_id,
            parent_artifact_id=fields.parent_artifact_id,
            tool_name=fields.tool_name,
            idempotency_key=idempotency_key,
        )
    except ArtifactDepositError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from None
    headers = {"Idempotent-Replayed": "true"} if result.replayed else {}
    return JSONResponse(
        status_code=201,
        content=result.artifact.model_dump(mode="json", by_alias=True),
        headers=headers,
    )


@require_permission("view_runtime_sessions")
def _require_view_runtime_sessions(*, current_user: Any, db: Session) -> None:
    """Endpoint-level permission gate, evaluated for user credentials."""


async def _list_auth_context(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db_session),
) -> ModelGatewayAuthContext:
    """Any account bearer; the route decides what it may list."""
    return await authenticate_request(
        authorization, db, allow_ended_runtime_session=True
    )


@router.get(
    "/runtime-sessions/{runtime_session_id}/artifacts",
    response_model=RuntimeSessionArtifactListOut,
    responses={**_ERRORS, 422: {"description": "Bad kind, label, limit or cursor"}},
)
def list_runtime_session_artifacts(
    runtime_session_id: str,
    kind: Optional[str] = Query(None),
    label: list[str] = Query(
        default_factory=list, description="Repeatable `key:value` label filter."
    ),
    limit: int = Query(artifact_deposit.LIST_LIMIT_DEFAULT),
    cursor: Optional[str] = Query(None),
    db: Session = Depends(get_db_session),
    auth: ModelGatewayAuthContext = Depends(_list_auth_context),
) -> RuntimeSessionArtifactListOut:
    """List a session's artifacts, newest first, with cursor pagination.

    A key pinned to this session may list it. Any other credential needs a
    user with ``view_runtime_sessions`` in the session's account.
    """
    try:
        session = artifact_deposit.require_session(
            db, auth=auth, runtime_session_id=runtime_session_id
        )
        if auth.runtime_session_id is None:
            user = db.get(models.User, auth.user.id)
            account = db.get(models.Account, auth.account_id)
            _require_view_runtime_sessions(current_user=user, db=db)
            require_session_history(
                db,
                account=account,
                summary={
                    "started_at": session.started_at,
                    "last_activity_at": session.last_activity_at,
                    "ended_at": session.ended_at,
                },
            )
        return artifact_deposit.list_artifacts(
            db,
            account_id=auth.account_id,
            runtime_session_id=session.id,
            kind=kind,
            labels=label,
            limit=limit,
            cursor=cursor,
        )
    except ArtifactDepositError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from None


async def _read_bounded_body(request: Request) -> bytes:
    """Read the body, refusing it once it passes the request limit."""
    limit = artifact_deposit.max_request_bytes()
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise HTTPException(413, artifact_deposit.ERROR_TOO_LARGE)
        except ValueError:
            raise HTTPException(400, "invalid_content_length") from None
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise HTTPException(413, artifact_deposit.ERROR_TOO_LARGE)
        data.extend(chunk)
    return bytes(data)


def _parse_json(body: bytes):
    try:
        fields = ArtifactDepositIn.model_validate_json(body)
    except ValidationError:
        raise ArtifactDepositError(422, "artifact_request_invalid") from None
    payload = artifact_deposit.payload_from_content_block(
        fields.content, kind=fields.kind
    )
    return fields, payload


async def _parse_multipart(request: Request, body: bytes):
    async def replay() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    form = await Request(request.scope, replay).form(max_files=1, max_fields=1)
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ArtifactDepositError(422, artifact_deposit.ERROR_CONTENT_REQUIRED)
        raw_meta = form.get("metadata")
        if isinstance(raw_meta, UploadFile):
            raw_meta = (await raw_meta.read()).decode("utf-8", "replace")
        try:
            fields = ArtifactDepositMetadata.model_validate(
                json.loads(raw_meta) if raw_meta else {}
            )
        except (ValueError, ValidationError):
            raise ArtifactDepositError(422, "artifact_request_invalid") from None
        name = fields.name or upload.filename
        if not name:
            raise ArtifactDepositError(422, "artifact_name_invalid")
        fields = fields.model_copy(update={"name": name})
        payload = artifact_deposit.payload_from_file(
            await upload.read(),
            content_type=upload.content_type,
            kind=fields.kind,
            name=name,
        )
        return fields, payload
    finally:
        await form.close()
