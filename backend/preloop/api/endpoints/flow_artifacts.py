"""Execution-scoped direct artifact transport; no storage-wide credentials."""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import jwt
from anyio import from_thread
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import crud_flow, crud_flow_execution, flow_artifact
from preloop.models.db.session import get_db_session
from preloop.models.schemas.flow_artifact import ArtifactReference
from preloop.services.flow_artifacts import (
    artifact_max_bytes,
    artifact_thread_id,
    get_artifact,
    put_artifact,
)

router = APIRouter()

# Must cover the longest allowed execution (24h) plus a short buffer so the
# final prepublication PUT is not rejected after a long coding run.
ARTIFACT_CAPABILITY_TTL = timedelta(hours=24, minutes=5)

CAPABILITY_AUDIENCE = "flow-artifact"
# What a caller gets instead of a bare "invalid_artifact_capability". These
# routes are the runner's transport: the only credential that works is a
# capability the orchestrator mints for one execution, one kind and one
# operation, and it is handed to the container, never to a person. A user or
# API token reading everything else about the execution still gets 401 here,
# which reads as a permission bug (round 2 CRA rerun, P9). The reply now says
# which door this is and which door the operator wants. It names no artifact
# and confirms nothing about the execution, so it is safe to return
# unauthenticated.
ARTIFACT_CAPABILITY_HELP = (
    "This route is the flow runner's artifact transport. It accepts only a "
    "scoped capability token minted by the orchestrator for a single "
    "execution; account bearer tokens and API keys are refused by design. To "
    "read a completed run's evidence pack use GET "
    "/api/v1/flows/executions/{execution_id}/evidence, and for its metadata "
    "GET /api/v1/flows/executions/{execution_id}/evidence-status."
)
ARTIFACT_CAPABILITY_ERROR = "invalid_artifact_capability"
CAPABILITY_CHALLENGE = f'Bearer realm="{CAPABILITY_AUDIENCE}"'


def mint_artifact_capability(
    *,
    account_id: UUID,
    flow_id: UUID,
    thread_id: str,
    execution_id: UUID,
    kind: Literal["workspace", "native_session", "evidence"],
    operation: Literal["put", "get"],
    reference: ArtifactReference | None = None,
) -> str:
    """Mint a bounded capability from trusted orchestration context only."""
    return jwt.encode(
        {
            "aud": CAPABILITY_AUDIENCE,
            "exp": datetime.now(UTC) + ARTIFACT_CAPABILITY_TTL,
            "account_id": str(account_id),
            "flow_id": str(flow_id),
            "thread_id": thread_id,
            "execution_id": str(execution_id),
            "kind": kind,
            "operation": operation,
            "reference": reference.model_dump(mode="json") if reference else None,
        },
        settings.security.secret_key,
        algorithm="HS256",
    )


def artifact_claims(authorization: str = Header(default="")) -> dict[str, Any]:
    """Reject normal API/JWT credentials; only the narrow artifact audience works."""
    try:
        if not authorization.startswith("Bearer "):
            raise ValueError("missing capability")
        claims = jwt.decode(
            authorization[7:],
            settings.security.secret_key,
            algorithms=["HS256"],
            audience=CAPABILITY_AUDIENCE,
            options={
                "require": [
                    "exp",
                    "account_id",
                    "flow_id",
                    "thread_id",
                    "execution_id",
                    "kind",
                    "operation",
                ]
            },
        )
        for key in ("account_id", "flow_id", "execution_id"):
            claims[key] = UUID(claims[key])
        return claims
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise HTTPException(
            401,
            {
                "error": ARTIFACT_CAPABILITY_ERROR,
                "message": ARTIFACT_CAPABILITY_HELP,
                "audience": CAPABILITY_AUDIENCE,
            },
            headers={"WWW-Authenticate": CAPABILITY_CHALLENGE},
        ) from exc


def authorize(
    db: Session, claims: dict[str, Any], execution_id: UUID, operation: str
) -> None:
    """Recheck execution tenancy and active upload permission on every request."""
    if claims["execution_id"] != execution_id or claims["operation"] != operation:
        raise HTTPException(403, "artifact_scope_mismatch")
    execution = crud_flow_execution.get(
        db, id=execution_id, account_id=str(claims["account_id"])
    )
    flow = crud_flow.get(db, id=claims["flow_id"])
    if (
        execution is None
        or flow is None
        or execution.flow_id != flow.id
        or flow.account_id != claims["account_id"]
    ):
        raise HTTPException(404, "artifact_execution_missing")
    trigger = execution.trigger_event_details or {}
    thread_id = artifact_thread_id(trigger, execution.id)
    if claims["thread_id"] != thread_id:
        raise HTTPException(403, "artifact_thread_mismatch")
    if operation == "put" and execution.status not in {
        "PENDING",
        "INITIALIZING",
        "RUNNING",
    }:
        raise HTTPException(409, "artifact_execution_closed")


@router.put(
    "/flows/executions/{execution_id}/artifacts",
    response_model=ArtifactReference,
    # Kept out of the published schema: the OpenAPI document is the operator
    # and SDK contract, and it listed this pair under bearerAuth as though an
    # account token could call them. Nothing outside the runner does, and the
    # runner is handed its URL and capability directly. The transport is
    # documented in docs/guide/flows/evidence-storage.md.
    include_in_schema=False,
)
def upload_artifact(
    execution_id: UUID,
    request: Request,
    claims: dict[str, Any] = Depends(artifact_claims),
    db: Session = Depends(get_db_session),
) -> ArtifactReference:
    """Read a bounded gzip archive and commit only after complete validation."""
    authorize(db, claims, execution_id, "put")

    async def read_archive() -> bytes:
        """Consume the ASGI stream on its loop with a strict size bound."""
        limit = artifact_max_bytes(str(claims.get("kind") or "workspace"))
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > limit:
                raise HTTPException(413, "artifact_oversized")
            data.extend(chunk)
        return bytes(data)

    archive = from_thread.run(read_archive)
    try:
        return put_artifact(
            db,
            account_id=claims["account_id"],
            flow_id=claims["flow_id"],
            execution_id=execution_id,
            thread_id=claims["thread_id"],
            kind=claims["kind"],
            archive=archive,
        )
    except ValueError as exc:
        flow_artifact.rollback(db)
        code = str(exc)
        if code == "artifact_execution_closed":
            raise HTTPException(409, code) from exc
        if code == "artifact_execution_missing":
            raise HTTPException(404, code) from exc
        raise HTTPException(422, code) from exc


@router.get(
    "/flows/executions/{execution_id}/artifacts",
    # See the PUT above: capability transport, not an operator read path.
    include_in_schema=False,
)
def download_artifact(
    execution_id: UUID,
    claims: dict[str, Any] = Depends(artifact_claims),
    db: Session = Depends(get_db_session),
) -> Response:
    """Download the one immutable reference named by a scoped capability."""
    authorize(db, claims, execution_id, "get")
    try:
        reference = ArtifactReference.model_validate(claims["reference"])
        body = get_artifact(
            db,
            account_id=claims["account_id"],
            flow_id=claims["flow_id"],
            thread_id=claims["thread_id"],
            reference=reference,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(410, str(exc)) from exc
    return Response(
        body, media_type="application/gzip", headers={"Cache-Control": "no-store"}
    )
