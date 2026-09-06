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
from preloop.services.flow_artifacts import get_artifact, put_artifact

router = APIRouter()

# Must cover the longest allowed execution (24h) plus a short buffer so the
# final prepublication PUT is not rejected after a long coding run.
ARTIFACT_CAPABILITY_TTL = timedelta(hours=24, minutes=5)


def mint_artifact_capability(
    *,
    account_id: UUID,
    flow_id: UUID,
    thread_id: str,
    execution_id: UUID,
    kind: Literal["workspace", "native_session"],
    operation: Literal["put", "get"],
    reference: ArtifactReference | None = None,
) -> str:
    """Mint a bounded capability from trusted orchestration context only."""
    return jwt.encode(
        {
            "aud": "flow-artifact",
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
            audience="flow-artifact",
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
        raise HTTPException(401, "invalid_artifact_capability") from exc


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
    resume = trigger.get("_resume") or {}
    thread_id = str(
        trigger.get("_session_thread_id")
        or resume.get("thread_id")
        or resume.get("execution_id")
        or execution.id
    )
    if claims["thread_id"] != thread_id:
        raise HTTPException(403, "artifact_thread_mismatch")
    if operation == "put" and execution.status not in {
        "PENDING",
        "INITIALIZING",
        "RUNNING",
    }:
        raise HTTPException(409, "artifact_execution_closed")


@router.put(
    "/flows/executions/{execution_id}/artifacts", response_model=ArtifactReference
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
        limit = settings.workspace_snapshot_max_bytes
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
        raise HTTPException(422, str(exc)) from exc


@router.get("/flows/executions/{execution_id}/artifacts")
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
