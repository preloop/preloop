"""Runner-only, execution-bound GitHub publication credential renewal."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import jwt
from anyio import from_thread
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import crud_flow_execution, crud_tracker
from preloop.models.db.session import get_db_session
from preloop.services.publication_credentials import mint_repository_lease
from preloop.services.trusted_publisher import PublicationError, PublicationLease

router = APIRouter()
AUDIENCE = "execution-publication-credential"


def mint_publication_capability(
    *, account_id: str, execution_id: str, tracker_id: str, repository_url: str
) -> str:
    """Bind publication authority to trusted startup topology, never caller input."""
    return jwt.encode(
        {
            "aud": AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(hours=24, minutes=5),
            "account_id": account_id,
            "execution_id": execution_id,
            "tracker_id": tracker_id,
            "repository_url": repository_url,
        },
        settings.security.secret_key,
        algorithm="HS256",
    )


def publication_claims(authorization: str = Header(default="")) -> dict[str, Any]:
    """Accept only the dedicated runner capability and validate claim types."""
    try:
        if not authorization.startswith("Bearer "):
            raise ValueError("missing capability")
        claims = jwt.decode(
            authorization[7:],
            settings.security.secret_key,
            algorithms=["HS256"],
            audience=AUDIENCE,
            options={
                "require": [
                    "exp",
                    "account_id",
                    "execution_id",
                    "tracker_id",
                    "repository_url",
                ]
            },
        )
        for key in ("account_id", "execution_id", "tracker_id"):
            if not isinstance(claims[key], str):
                raise ValueError("invalid identity claim")
            claims[key] = UUID(claims[key])
        if claims["aud"] != AUDIENCE or not isinstance(claims["repository_url"], str):
            raise ValueError("invalid repository")
        return claims
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise HTTPException(401, "invalid_publication_capability") from exc


@router.post(
    "/flows/executions/{execution_id}/publication-credential", include_in_schema=False
)
def refresh_publication_credential(
    execution_id: UUID,
    response: Response,
    claims: dict[str, Any] = Depends(publication_claims),
    db: Session = Depends(get_db_session),
) -> dict[str, str]:
    """Issue fresh exact-repository authority only while the execution is running."""
    if claims["execution_id"] != execution_id:
        raise HTTPException(403, "publication_scope_mismatch")
    execution = crud_flow_execution.get(
        db, id=execution_id, account_id=str(claims["account_id"]), refresh=True
    )
    if execution is None:
        raise HTTPException(404, "publication_execution_missing")
    if execution.status != "RUNNING" or getattr(execution, "stop_requested_at", None):
        raise HTTPException(409, "publication_execution_closed")
    tracker = crud_tracker.get(
        db, id=claims["tracker_id"], account_id=str(claims["account_id"])
    )
    if tracker is None:
        raise HTTPException(404, "publication_tracker_missing")
    # Resolve the lazy relationship on the worker too. The async issuer reads
    # only loaded attributes; no synchronous SQL runs on the ASGI event loop.
    _ = tracker.oauth_installation

    async def issue() -> PublicationLease:
        async with httpx.AsyncClient() as client:
            return await mint_repository_lease(
                tracker,
                claims["repository_url"],
                write=True,
                client=client,
                allow_legacy_oauth_app=True,
            )

    try:
        lease = from_thread.run(issue)
        if lease.expires_at <= datetime.now(UTC) + timedelta(seconds=30):
            raise PublicationError("Credential lifetime insufficient")
    except (PublicationError, httpx.HTTPError) as exc:
        raise HTTPException(502, "publication_credential_unavailable") from exc
    # A stop can race the provider request. Do not return newly issued
    # authority to a run which closed or received emergency stop meanwhile.
    execution = crud_flow_execution.get(
        db, id=execution_id, account_id=str(claims["account_id"]), refresh=True
    )
    if (
        execution is None
        or execution.status != "RUNNING"
        or getattr(execution, "stop_requested_at", None)
    ):
        raise HTTPException(409, "publication_execution_closed")
    response.headers["Cache-Control"] = "no-store"
    return {"token": lease.token, "expires_at": lease.expires_at.isoformat()}
