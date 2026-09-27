"""OAuth Authorization Server endpoints for CLI and MCP client authentication.

Provides:
    GET  /oauth/authorize  — Redirects to the login/consent page
    POST /oauth/token      — Exchanges authorization code for tokens
    POST /oauth/register   — Dynamic client registration (MCP clients)
    POST /oauth/revoke     — Token revocation

The CLI uses /oauth/authorize + /oauth/token with either a loopback redirect
or an out-of-band copy/paste redirect (no PKCE).
MCP clients (Claude Desktop) discover these via /.well-known metadata.
"""

import logging
import os
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlencode

import jwt as pyjwt
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from preloop.schemas.auth import TokenData

logger = logging.getLogger(__name__)

router = APIRouter(tags=["OAuth Server"], include_in_schema=False)

CLI_JWT_REFRESH_TOKEN_EXPIRE_DAYS = int(
    os.getenv("CLI_JWT_REFRESH_TOKEN_EXPIRE_DAYS", "365")
)


def _oauth_error(
    error: str, error_description: str, status_code: int = 400
) -> JSONResponse:
    """Return an RFC 6749 §5.2 OAuth error response.

    MCP/OAuth clients expect {"error": "...", "error_description": "..."},
    NOT FastAPI's {"detail": "..."}.
    """
    return JSONResponse(
        {"error": error, "error_description": error_description},
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# Well-known metadata endpoints (MCP OAuth discovery)
# ---------------------------------------------------------------------------


class AuthorizationServerMetadata(BaseModel):
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: Optional[str] = None
    revocation_endpoint: Optional[str] = None
    response_types_supported: list[str] = ["code"]
    grant_types_supported: list[str] = ["authorization_code", "refresh_token"]
    token_endpoint_auth_methods_supported: list[str] = ["client_secret_post", "none"]
    code_challenge_methods_supported: list[str] = ["S256"]


@router.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server_metadata():
    """Return OAuth Authorization Server Metadata (RFC 8414)."""
    base_url = os.getenv("PRELOOP_URL", "http://localhost:8000").rstrip("/")

    return AuthorizationServerMetadata(
        issuer=base_url,
        authorization_endpoint=f"{base_url}/oauth/authorize",
        token_endpoint=f"{base_url}/oauth/token",
        registration_endpoint=f"{base_url}/oauth/register",
        revocation_endpoint=f"{base_url}/oauth/revoke",
    )


@router.get("/.well-known/oauth-protected-resource/{path:path}")
async def oauth_protected_resource_metadata_with_path(path: str):
    """Return OAuth Protected Resource Metadata (RFC 9728) for a specific path.

    MCP clients look for this at:
      /.well-known/oauth-protected-resource/{mcp_server_path}
    e.g. /.well-known/oauth-protected-resource/mcp/v1
    """
    base_url = os.getenv("PRELOOP_URL", "http://localhost:8000").rstrip("/")

    return {
        "resource": f"{base_url}/{path}",
        "authorization_servers": [base_url],
    }


@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource_metadata():
    """Return OAuth Protected Resource Metadata (RFC 9728) for root."""
    base_url = os.getenv("PRELOOP_URL", "http://localhost:8000").rstrip("/")

    return {
        "resource": f"{base_url}/mcp",
        "authorization_servers": [base_url],
    }


# ---------------------------------------------------------------------------
# Authorization endpoint
# ---------------------------------------------------------------------------


@router.get("/oauth/authorize")
async def authorize(
    request: "Request",
    response_type: str = Query("code"),
    client_id: str = Query(""),
    redirect_uri: str = Query(...),
    state: str = Query(""),
    code_challenge: str = Query(""),
    code_challenge_method: str = Query(""),
    scope: str = Query(""),
):
    """OAuth authorization endpoint — redirects to the login/consent page.

    Both the CLI and MCP clients hit this endpoint. It redirects to the
    consent page where the user authenticates with their Preloop credentials.
    """
    # Use "cli" as default client_id for the CLI tool
    effective_client_id = client_id or "cli"

    # Build redirect to the consent page
    params = {
        "client_id": effective_client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scopes": scope,
        "redirect_uri_provided_explicitly": "true",
    }
    if code_challenge:
        params["code_challenge"] = code_challenge

    # Forward resource parameter (RFC 8707) — required by MCP OAuth spec
    resource = request.query_params.get("resource", "")
    if resource:
        params["resource"] = resource

    consent_url = f"/mcp/authorize/consent?{urlencode(params)}"
    return RedirectResponse(url=consent_url, status_code=302)


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


@router.post("/oauth/token")
async def token_exchange(
    grant_type: str = Form(...),
    code: str = Form(""),
    redirect_uri: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    code_verifier: str = Form(""),
    refresh_token: str = Form(""),
    device_name: str = Form(""),
    request: Request = None,  # type: ignore[assignment]
):
    """Exchange an authorization code for access/refresh tokens.

    Supports both:
    - CLI flow (no PKCE): returns JWT tokens usable with the REST API
    - MCP flow (with PKCE): returns opaque OAuth tokens

    ``device_name`` is optional and only used by the CLI flow: the host name
    shown in ``preloop auth sessions list``.
    """
    logger.info(
        f"OAuth token request: grant_type={grant_type}, client_id={client_id!r}, "
        f"code={'***' if code else '(empty)'}, code_verifier={'***' if code_verifier else '(empty)'}"
    )

    user_agent = request.headers.get("user-agent") if request is not None else None
    if grant_type == "authorization_code":
        return await _handle_authorization_code(
            code=code,
            redirect_uri=redirect_uri,
            client_id=client_id,
            code_verifier=code_verifier,
            user_agent=user_agent,
            hostname=device_name or None,
        )
    elif grant_type == "refresh_token":
        return await _handle_refresh_token(
            refresh_token_str=refresh_token,
            client_id=client_id,
            user_agent=user_agent,
            hostname=device_name or None,
        )
    else:
        return _oauth_error(
            "unsupported_grant_type",
            f"Unsupported grant_type: {grant_type}",
        )


async def _handle_authorization_code(
    code: str,
    redirect_uri: str,
    client_id: str,
    code_verifier: str,
    user_agent: Optional[str] = None,
    hostname: Optional[str] = None,
):
    """Exchange authorization code for tokens."""
    from preloop.models.crud.oauth_mcp_token import crud_oauth_mcp_auth_code
    from preloop.models.db.session import get_db_session

    db = next(get_db_session())
    try:
        # Look up the auth code.
        # Some MCP clients (e.g. Codex CLI) omit client_id in the token request.
        # When client_id is provided, look up by both hash + client_id.
        # When absent, look up by hash only and use the stored client_id.
        if client_id:
            effective_client_id = client_id
            logger.info(
                f"Auth code exchange: client_id={effective_client_id!r}, "
                f"redirect_uri={redirect_uri!r}"
            )
            db_code = crud_oauth_mcp_auth_code.get_by_code(
                db, code=code, client_id=effective_client_id
            )
        else:
            logger.info(
                f"Auth code exchange: client_id not provided, "
                f"looking up by code hash only. redirect_uri={redirect_uri!r}"
            )
            db_code = crud_oauth_mcp_auth_code.get_by_code_hash(db, code=code)
            if db_code:
                effective_client_id = db_code.client_id
                logger.info(f"Found auth code for client_id={effective_client_id!r}")
            else:
                effective_client_id = "(unknown)"

        if not db_code:
            logger.warning(f"Auth code not found for client_id={effective_client_id!r}")
            return _oauth_error(
                "invalid_grant",
                f"Invalid authorization code (client_id={effective_client_id})",
            )

        if db_code.is_used:
            logger.warning("Auth code already used")
            return _oauth_error("invalid_grant", "Authorization code already used")

        if db_code.expires_at < time.time():
            logger.warning("Auth code expired")
            return _oauth_error("invalid_grant", "Authorization code expired")

        # Validate redirect_uri matches what was stored in the auth code.
        # Per RFC 6749 §4.1.3: if redirect_uri was included in the
        # authorization request, it MUST be required here and match exactly.
        if db_code.redirect_uri:
            if not redirect_uri:
                return _oauth_error(
                    "invalid_grant",
                    "redirect_uri is required (it was included in the authorization request)",
                )
            if db_code.redirect_uri != redirect_uri:
                logger.warning(
                    f"redirect_uri mismatch: expected={db_code.redirect_uri!r}, "
                    f"got={redirect_uri!r}"
                )
                return _oauth_error(
                    "invalid_grant",
                    "redirect_uri does not match the authorization request",
                )

        # Mark code as used
        crud_oauth_mcp_auth_code.mark_used(db, obj=db_code)

        # Decide token type based on whether PKCE was used
        has_pkce = bool(db_code.code_challenge)
        logger.info(f"Auth code valid: has_pkce={has_pkce}, user_id={db_code.user_id}")

        if has_pkce:
            # PKCE was used — code_verifier is REQUIRED
            if not code_verifier:
                return _oauth_error(
                    "invalid_grant",
                    "code_verifier is required when PKCE was used",
                )

            import hashlib
            import base64

            expected = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(code_verifier.encode()).digest()
                )
                .rstrip(b"=")
                .decode()
            )

            if expected != db_code.code_challenge:
                return _oauth_error(
                    "invalid_grant",
                    "Invalid code_verifier (PKCE validation failed)",
                )

            # MCP flow: issue opaque OAuth tokens
            return await _issue_opaque_tokens(db, db_code)
        else:
            # No PKCE (CLI flow): issue JWT tokens
            return await _issue_jwt_tokens(
                db, db_code, user_agent=user_agent, hostname=hostname
            )

    finally:
        db.close()


def _new_refresh_jti() -> str:
    """Return a fresh random refresh token id."""
    return uuid.uuid4().hex


def _cli_token_response(
    *,
    sub: str,
    scopes: list[str],
    generation: int,
    session_id: uuid.UUID,
    refresh_jti: str,
) -> JSONResponse:
    """Mint the CLI access/refresh JWT pair for one ``cli_session`` row.

    Both tokens carry ``sid`` so revoking the row rejects them. Only the
    refresh token carries ``jti``; it must match ``cli_session.refresh_jti``
    to rotate.
    """
    from datetime import timedelta

    from preloop.api.auth.jwt import ACCESS_TOKEN_EXPIRE_MINUTES, create_access_token

    sid = str(session_id)
    access_token = create_access_token(
        data={"sub": sub, "scopes": scopes, "sid": sid},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        auth_generation=generation,
    )
    refresh_token = create_access_token(
        data={
            "sub": sub,
            "scopes": scopes,
            "refresh": True,
            "sid": sid,
            "jti": refresh_jti,
        },
        expires_delta=timedelta(days=CLI_JWT_REFRESH_TOKEN_EXPIRE_DAYS),
        auth_generation=generation,
    )
    return JSONResponse(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }
    )


async def _issue_jwt_tokens(
    db: Any,
    db_code: Any,
    *,
    user_agent: Optional[str] = None,
    hostname: Optional[str] = None,
):
    """Issue JWT access/refresh tokens for CLI usage.

    Each login records a ``cli_session`` row so this login can be revoked on
    its own (``POST /oauth/revoke``, ``preloop auth logout``).
    """
    from preloop.api.auth.jwt import user_auth_generation
    from preloop.models.crud import crud_cli_session, crud_user

    user = crud_user.get(db, id=str(db_code.user_id))
    if not user:
        return _oauth_error("invalid_grant", "User not found")

    refresh_jti = _new_refresh_jti()
    cli_session = crud_cli_session.create(
        db,
        user_id=user.id,
        refresh_jti=refresh_jti,
        user_agent=user_agent,
        hostname=hostname,
    )
    return _cli_token_response(
        sub=str(user.id),
        scopes=[],
        generation=user_auth_generation(user),
        session_id=cli_session.id,
        refresh_jti=refresh_jti,
    )


async def _issue_opaque_tokens(db, db_code):
    """Issue opaque OAuth tokens for MCP clients."""
    from preloop.models.crud.oauth_mcp_token import (
        crud_oauth_mcp_access_token,
        crud_oauth_mcp_refresh_token,
        generate_token,
    )

    now = int(time.time())
    access_token_str = generate_token(32)
    refresh_token_str = generate_token(32)

    crud_oauth_mcp_access_token.create(
        db,
        token=access_token_str,
        client_id=db_code.client_id,
        user_id=db_code.user_id,
        account_id=db_code.account_id,
        scopes=db_code.scopes or [],
        expires_at=now + 3600,
        resource=db_code.resource,
    )

    crud_oauth_mcp_refresh_token.create(
        db,
        token=refresh_token_str,
        client_id=db_code.client_id,
        user_id=db_code.user_id,
        account_id=db_code.account_id,
        scopes=db_code.scopes or [],
        expires_at=now + 2592000,  # 30 days
    )

    return JSONResponse(
        {
            "access_token": access_token_str,
            "refresh_token": refresh_token_str,
            "token_type": "Bearer",
            "expires_in": 3600,
        }
    )


async def _handle_refresh_token(
    refresh_token_str: str,
    client_id: str,
    user_agent: Optional[str] = None,
    hostname: Optional[str] = None,
):
    """Exchange a refresh token for new tokens.

    Supports both:
    - Opaque OAuth refresh tokens (MCP clients) — looked up in DB
    - JWT refresh tokens (CLI): decoded and reissued against their
      ``cli_session`` row
    """
    # 1. Try opaque OAuth refresh token (MCP clients)
    try:
        from preloop.models.crud.oauth_mcp_token import (
            crud_oauth_mcp_refresh_token,
            crud_oauth_mcp_access_token,
            generate_token,
        )
        from preloop.models.db.session import get_db_session

        db = next(get_db_session())
        try:
            db_refresh = crud_oauth_mcp_refresh_token.get_by_token(
                db, token=refresh_token_str
            )
            if db_refresh and not db_refresh.is_revoked:
                if db_refresh.expires_at and db_refresh.expires_at < int(time.time()):
                    return _oauth_error("invalid_grant", "Refresh token expired")

                # Revoke the old refresh token (rotation)
                crud_oauth_mcp_refresh_token.revoke(db, obj=db_refresh)

                # Issue new opaque tokens
                now = int(time.time())
                new_access = generate_token(32)
                new_refresh = generate_token(32)

                crud_oauth_mcp_access_token.create(
                    db,
                    token=new_access,
                    client_id=db_refresh.client_id,
                    user_id=db_refresh.user_id,
                    account_id=db_refresh.account_id,
                    scopes=db_refresh.scopes or [],
                    expires_at=now + 3600,
                    resource=None,
                )

                crud_oauth_mcp_refresh_token.create(
                    db,
                    token=new_refresh,
                    client_id=db_refresh.client_id,
                    user_id=db_refresh.user_id,
                    account_id=db_refresh.account_id,
                    scopes=db_refresh.scopes or [],
                    expires_at=now + 2592000,  # 30 days
                )

                return JSONResponse(
                    {
                        "access_token": new_access,
                        "refresh_token": new_refresh,
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    }
                )
        finally:
            db.close()
    except Exception:
        # Opaque refresh failed; fall through to JWT refresh handling below.
        pass

    # 2. Try JWT refresh token (CLI path)
    try:
        from preloop.api.auth.jwt import (
            SESSION_REVOKED_DETAIL,
            decode_token,
            reject_stale_token_generation,
            user_auth_generation,
        )
        from preloop.models.crud import crud_cli_session, crud_user
        from preloop.models.db.session import get_db_session

        token_data = decode_token(refresh_token_str)
        if token_data.refresh and token_data.sub:
            db = next(get_db_session())
            try:
                user = crud_user.get(db, id=token_data.sub)
                if user is None or not user.is_active:
                    return _oauth_error("invalid_grant", "User not found or inactive")
                try:
                    reject_stale_token_generation(user, token_data)
                except HTTPException as exc:
                    return _oauth_error("invalid_grant", str(exc.detail))

                new_jti = _new_refresh_jti()
                if token_data.sid is not None:
                    # Rotate only if this token's jti is still the session's
                    # current one: a revoked session, or a refresh token that
                    # was already rotated away, is rejected.
                    try:
                        session_id = uuid.UUID(token_data.sid)
                    except ValueError:
                        return _oauth_error("invalid_grant", "Invalid refresh token")
                    if not token_data.jti or not crud_cli_session.rotate(
                        db,
                        session_id=session_id,
                        user_id=user.id,
                        old_jti=token_data.jti,
                        new_jti=new_jti,
                    ):
                        return _oauth_error("invalid_grant", SESSION_REVOKED_DETAIL)
                else:
                    # A CLI refresh token minted before cli_session existed.
                    # Move it onto a session row so the rotated pair can be
                    # revoked on its own. The old token itself stays covered
                    # by the generation check (logout --all).
                    session_id = crud_cli_session.create(
                        db,
                        user_id=user.id,
                        refresh_jti=new_jti,
                        user_agent=user_agent,
                        hostname=hostname,
                    ).id

                return _cli_token_response(
                    sub=token_data.sub,
                    scopes=token_data.scopes or [],
                    generation=user_auth_generation(user),
                    session_id=session_id,
                    refresh_jti=new_jti,
                )
            finally:
                db.close()
    except HTTPException:
        # decode_token raises 401 for a token that is not one of ours.
        return _oauth_error("invalid_grant", "Invalid refresh token")
    except Exception:
        # JWT refresh failed; return invalid_grant below.
        pass

    return _oauth_error("invalid_grant", "Invalid refresh token")


# ---------------------------------------------------------------------------
# Dynamic Client Registration (for MCP clients)
# ---------------------------------------------------------------------------


@router.post("/oauth/register")
async def register_client(request_body: dict):
    """Register an OAuth client dynamically (RFC 7591).

    MCP clients (like Claude Desktop) call this to register before
    starting the authorization flow.
    """
    from preloop.api.endpoints.oauth_consent import get_oauth_provider

    provider = get_oauth_provider()
    if not provider:
        raise HTTPException(status_code=501, detail="OAuth not configured")

    from mcp.shared.auth import OAuthClientInformationFull

    client_info = OAuthClientInformationFull(
        client_id="pending",
        redirect_uris=request_body.get("redirect_uris", []),
        grant_types=request_body.get(
            "grant_types", ["authorization_code", "refresh_token"]
        ),
        response_types=request_body.get("response_types", ["code"]),
        token_endpoint_auth_method=request_body.get(
            "token_endpoint_auth_method", "none"
        ),
        client_name=request_body.get("client_name"),
        client_uri=request_body.get("client_uri"),
        scope=request_body.get("scope"),
        contacts=request_body.get("contacts"),
        software_id=request_body.get("software_id"),
        software_version=request_body.get("software_version"),
    )

    await provider.register_client(client_info)

    return JSONResponse(
        {
            "client_id": client_info.client_id,
            "client_secret": client_info.client_secret,
            "client_id_issued_at": client_info.client_id_issued_at,
            "client_secret_expires_at": client_info.client_secret_expires_at,
            "redirect_uris": [str(u) for u in (client_info.redirect_uris or [])],
            "grant_types": client_info.grant_types,
            "response_types": client_info.response_types,
            "token_endpoint_auth_method": client_info.token_endpoint_auth_method,
            "client_name": client_info.client_name,
        }
    )


# ---------------------------------------------------------------------------
# Token Revocation
# ---------------------------------------------------------------------------


def _decode_expired_cli_jwt(token: str) -> Optional[TokenData]:
    """Decode a correctly signed but expired CLI JWT that carries a ``sid``.

    Only the expiry check is skipped; the signature is still verified.

    Returns:
        The token data, or None when the token is not ours, is invalid for
        another reason, or has no ``sid``.
    """
    from preloop.api.auth.jwt import ALGORITHM, SECRET_KEY

    try:
        payload = pyjwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"verify_exp": False},
        )
    except pyjwt.PyJWTError:
        return None
    sid = payload.get("sid")
    sub = payload.get("sub")
    if not isinstance(sid, str) or not sid or not isinstance(sub, str) or not sub:
        return None
    return TokenData(sub=sub, sid=sid)


def _revoke_cli_jwt(token: str) -> Optional[JSONResponse]:
    """Revoke the ``cli_session`` behind a CLI JWT.

    Returns:
        None when ``token`` is not one of our JWTs (the caller then tries
        the opaque MCP tables), otherwise the response to send.
    """
    from preloop.api.auth.jwt import decode_token

    try:
        token_data = decode_token(token)
    except HTTPException:
        # An expired CLI token still names its session. Revoke that row so
        # logging out with a stale token does not leave it listed as active.
        token_data = _decode_expired_cli_jwt(token)
        if token_data is None:
            return None

    if token_data.sid is None:
        # Console JWTs and CLI JWTs from before cli_session have no row to
        # revoke. RFC 7009 section 2.2.1 allows unsupported_token_type here;
        # a 200 would claim a revocation that did not happen.
        return _oauth_error(
            "unsupported_token_type",
            "This token has no revocable CLI session; revoke every session "
            "with POST /api/v1/auth/sessions/revoke-all",
        )

    from preloop.models.crud import crud_cli_session
    from preloop.models.db.session import get_db_session

    try:
        session_id = uuid.UUID(token_data.sid)
        user_id = uuid.UUID(token_data.sub or "")
    except ValueError:
        # Signed by us but malformed; nothing to revoke (RFC 7009: 200).
        return JSONResponse({"status": "revoked"})

    db = next(get_db_session())
    try:
        # Revoking an already revoked session is still a success.
        crud_cli_session.revoke(db, session_id=session_id, user_id=user_id)
    finally:
        db.close()
    return JSONResponse({"status": "revoked"})


@router.post("/oauth/revoke")
async def revoke_token(token: str = Form(...)):
    """Revoke an access or refresh token.

    A CLI JWT (access or refresh) revokes its whole ``cli_session``: both
    tokens of that login stop working. Opaque MCP tokens are revoked in the
    MCP token tables as before.
    """
    cli_response = _revoke_cli_jwt(token)
    if cli_response is not None:
        return cli_response

    from preloop.api.endpoints.oauth_consent import get_oauth_provider

    provider = get_oauth_provider()
    if not provider:
        raise HTTPException(status_code=501, detail="OAuth not configured")

    # Try to revoke as access token first
    access_token = await provider.load_access_token(token)
    if access_token:
        await provider.revoke_token(access_token)
        return JSONResponse({"status": "revoked"})

    # Try to revoke as refresh token
    try:
        from preloop.models.crud.oauth_mcp_token import crud_oauth_mcp_refresh_token
        from preloop.models.db.session import get_db_session

        db = next(get_db_session())
        try:
            db_refresh = crud_oauth_mcp_refresh_token.get_by_token(db, token=token)
            if db_refresh and not db_refresh.is_revoked:
                crud_oauth_mcp_refresh_token.revoke(db, obj=db_refresh)
                return JSONResponse({"status": "revoked"})
        finally:
            db.close()
    except Exception:
        # Token may already be revoked or absent; RFC 7009 still returns success.
        pass

    # Not found — still return success per RFC 7009
    return JSONResponse({"status": "revoked"})
