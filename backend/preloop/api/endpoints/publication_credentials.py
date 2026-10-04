"""Runner-only, execution-bound publication credential renewal.

One capability, minted at launch from trusted startup topology, is exchanged
for fresh publication authority immediately before a late push or PR call:

* GitHub App trackers receive a repository-scoped installation token.
* Managed Bitbucket Cloud trackers (``auth_type == "managed_oauth"``) receive
  the current access token from the provider resolver, after the repository
  in the capability is checked against the tracker's workspace binding.

Both paths require a RUNNING execution without a stop request before and
after acquisition. Refresh tokens and consumer secrets never leave the
control plane; the runner only ever sees an access credential and the git
username it pairs with.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import jwt
from anyio import from_thread
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import crud_flow_execution, crud_tracker
from preloop.models.db.session import get_db_session
from preloop.services.managed_credentials import (
    ManagedCredential,
    ManagedCredentialError,
    ManagedCredentialPermissionError,
    ManagedReconnectRequiredError,
    is_managed_tracker,
    resolve_managed_credential,
)
from preloop.services.publication_credentials import mint_repository_lease
from preloop.services.trusted_publisher import PublicationError, PublicationLease
from preloop.utils.bitbucket import BITBUCKET_HOST

router = APIRouter()
AUDIENCE = "execution-publication-credential"
MIN_CREDENTIAL_LIFETIME = timedelta(seconds=30)


def managed_repository_binding(tracker: Any, repository_url: str) -> str:
    """Return the repository slug the capability names, within the tracker binding.

    Raises:
        HTTPException: 403 when the destination is not a Bitbucket Cloud
            repository inside the tracker's workspace (and bound repository).
    """
    parsed = urlsplit(repository_url)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != BITBUCKET_HOST
        # hostname drops the port; the runner keys its store by netloc, so a
        # non-default port would release the token for a different endpoint.
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or len(parts) != 2
    ):
        raise HTTPException(403, "publication_destination_mismatch")
    workspace, slug = parts[0], parts[1].removesuffix(".git")
    details = dict(getattr(tracker, "connection_details", None) or {})
    bound_workspace = str(details.get("workspace") or "")
    bound_repository = str(details.get("repository") or "")
    if (
        not bound_workspace
        or workspace.casefold() != bound_workspace.casefold()
        or (bound_repository and slug.casefold() != bound_repository.casefold())
    ):
        raise HTTPException(403, "publication_destination_mismatch")
    return slug


def _managed_http(exc: ManagedCredentialError) -> HTTPException:
    if isinstance(exc, ManagedReconnectRequiredError):
        return HTTPException(409, "publication_reconnect_required")
    if isinstance(exc, ManagedCredentialPermissionError):
        return HTTPException(403, "publication_destination_forbidden")
    return HTTPException(502, "publication_credential_unavailable")


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
    username = "x-access-token"
    if is_managed_tracker(tracker):
        # Destination binding is checked before any resolver call, so a
        # capability for another repository cannot obtain the grant's token.
        repository = managed_repository_binding(tracker, claims["repository_url"])
        account_id, tracker_id = claims["account_id"], claims["tracker_id"]
        provider = str(tracker.tracker_type).lower()

        async def resolve(force_refresh: bool) -> ManagedCredential:
            return await resolve_managed_credential(
                account_id=account_id,
                tracker_id=tracker_id,
                provider=provider,
                repository=repository,
                force_refresh=force_refresh,
            )

        try:
            credential = from_thread.run(resolve, False)
            if credential.expires_at <= datetime.now(UTC) + MIN_CREDENTIAL_LIFETIME:
                credential = from_thread.run(resolve, True)
            if credential.expires_at <= datetime.now(UTC) + MIN_CREDENTIAL_LIFETIME:
                raise HTTPException(502, "publication_credential_unavailable")
        except ManagedCredentialError as exc:
            raise _managed_http(exc) from exc
        lease = PublicationLease(
            token=credential.access_token,
            repository_url=claims["repository_url"],
            expires_at=credential.expires_at,
        )
        username = credential.git_username
    else:
        # Resolve the lazy relationship on the worker too. The async issuer
        # reads only loaded attributes; no synchronous SQL runs on the ASGI
        # event loop.
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
            if lease.expires_at <= datetime.now(UTC) + MIN_CREDENTIAL_LIFETIME:
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
    return {
        "token": lease.token,
        "expires_at": lease.expires_at.isoformat(),
        "username": username,
    }
