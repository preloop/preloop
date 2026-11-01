"""Preloop OAuth Authorization Server Provider for MCP.

Implements FastMCP's OAuthProvider interface so that MCP clients (Claude Desktop,
CLI, etc.) can authenticate via the standard OAuth 2.1 authorization code flow
with PKCE.

Also falls back to existing Bearer token (API key / JWT) validation for
backward compatibility.
"""

import logging
import time
from typing import Any, Optional
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from fastmcp.server.auth.auth import AccessToken, OAuthProvider

from preloop.api.auth.identity import require_session_membership
from preloop.services.authentication_policy import (
    authorize_session_record,
    authorize_authentication,
)
from preloop.api.auth.jwt import user_auth_generation
from fastapi import HTTPException

from preloop.models.crud.oauth_mcp_client import crud_oauth_mcp_client
from preloop.models.crud.oauth_mcp_token import (
    crud_oauth_mcp_access_token,
    crud_oauth_mcp_auth_code,
    crud_oauth_mcp_refresh_token,
    generate_authorization_code,
    generate_token,
)

logger = logging.getLogger(__name__)

# Token expiry defaults (seconds)
ACCESS_TOKEN_EXPIRY = 3600  # 1 hour
REFRESH_TOKEN_EXPIRY = 2592000  # 30 days
AUTH_CODE_EXPIRY = 300  # 5 minutes


def _get_db():
    """Get a database session (caller must close)."""
    from preloop.models.db.session import get_session_factory

    factory = get_session_factory()
    return factory()


class PreloopOAuthProvider(OAuthProvider):
    """OAuth 2.1 Authorization Server for the Preloop MCP server.

    Implements the full OAuth flow:
    - Dynamic Client Registration (POST /register)
    - Authorization (GET /authorize → login/consent page → redirect with code)
    - Token exchange (POST /token)
    - Token refresh (POST /token with grant_type=refresh_token)
    - Token revocation (POST /revoke)
    - Token verification (Bearer token in requests)

    Also supports backward-compatible Bearer token auth (API keys and JWTs)
    by falling back to Preloop's existing auth system when the token is not
    found in the OAuth token store.
    """

    def __init__(
        self,
        base_url: str,
        issuer_url: Optional[str] = None,
        access_token_expiry: int = ACCESS_TOKEN_EXPIRY,
        refresh_token_expiry: int = REFRESH_TOKEN_EXPIRY,
    ):
        from mcp.server.auth.settings import (
            ClientRegistrationOptions,
            RevocationOptions,
        )

        self._access_token_expiry = access_token_expiry
        self._refresh_token_expiry = refresh_token_expiry

        super().__init__(
            base_url=base_url,
            issuer_url=issuer_url or base_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[],
            ),
            revocation_options=RevocationOptions(
                enabled=True,
            ),
            required_scopes=[],
        )

    # -------------------------------------------------------------------------
    # Dynamic Client Registration (RFC 7591)
    # -------------------------------------------------------------------------

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        """Retrieve registered client by client_id."""
        db = _get_db()
        try:
            db_client = crud_oauth_mcp_client.get_by_client_id(db, client_id=client_id)
            if not db_client:
                return None
            return OAuthClientInformationFull(
                client_id=db_client.client_id,
                client_secret=None,  # Never expose secret
                client_id_issued_at=db_client.client_id_issued_at,
                client_secret_expires_at=db_client.client_secret_expires_at or 0,
                redirect_uris=db_client.redirect_uris,
                grant_types=db_client.grant_types,
                response_types=db_client.response_types,
                token_endpoint_auth_method=db_client.token_endpoint_auth_method,
                client_name=db_client.client_name,
                scope=db_client.scope,
            )
        finally:
            db.close()

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Register a new OAuth client via Dynamic Client Registration."""
        db = _get_db()
        try:
            now = int(time.time())

            # Generate credentials
            new_client_id = crud_oauth_mcp_client.generate_client_id()
            new_client_secret = crud_oauth_mcp_client.generate_client_secret()
            secret_hash = crud_oauth_mcp_client.hash_secret(new_client_secret)

            # Store in DB
            crud_oauth_mcp_client.create(
                db,
                obj_in={
                    "client_id": new_client_id,
                    "client_secret_hash": secret_hash,
                    "client_secret_expires_at": 0,  # Never expires
                    "redirect_uris": [
                        str(u) for u in (client_info.redirect_uris or [])
                    ],
                    "grant_types": client_info.grant_types
                    or ["authorization_code", "refresh_token"],
                    "response_types": client_info.response_types or ["code"],
                    "token_endpoint_auth_method": client_info.token_endpoint_auth_method
                    or "client_secret_post",
                    "client_name": client_info.client_name,
                    "scope": client_info.scope,
                    "client_uri": str(client_info.client_uri)
                    if client_info.client_uri
                    else None,
                    "logo_uri": str(client_info.logo_uri)
                    if client_info.logo_uri
                    else None,
                    "contacts": client_info.contacts,
                    "software_id": client_info.software_id,
                    "software_version": client_info.software_version,
                    "client_id_issued_at": now,
                },
            )

            # Mutate the passed-in object so the SDK returns credentials to the client
            client_info.client_id = new_client_id
            client_info.client_secret = new_client_secret
            client_info.client_id_issued_at = now
            client_info.client_secret_expires_at = 0

            logger.info(
                f"Registered OAuth MCP client: {new_client_id} ({client_info.client_name})"
            )
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # Authorization
    # -------------------------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Redirect user to the Preloop login/consent page.

        Returns a URL that the MCP SDK will redirect the user's browser to.
        The login page will authenticate the user and redirect back to the
        client's redirect_uri with an authorization code.
        """
        assert self.base_url is not None

        # Build URL to our login/consent page (mounted at root, not under /mcp)
        # base_url is e.g. "http://localhost:8000/mcp" — strip /mcp to get root
        base = str(self.base_url).rstrip("/")
        if base.endswith("/mcp"):
            root_url = base[: -len("/mcp")]
        else:
            root_url = base

        query_params = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "code_challenge": params.code_challenge,
            "scopes": " ".join(params.scopes) if params.scopes else "",
        }
        if params.state:
            query_params["state"] = params.state
        if params.redirect_uri_provided_explicitly:
            query_params["redirect_uri_provided_explicitly"] = "true"
        if params.resource:
            query_params["resource"] = params.resource

        authorize_url = f"{root_url}/mcp/authorize/consent?{urlencode(query_params)}"
        return authorize_url

    # -------------------------------------------------------------------------
    # Authorization Code Exchange
    # -------------------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[AuthorizationCode]:
        """Load an authorization code from the database."""
        db = _get_db()
        try:
            db_code = crud_oauth_mcp_auth_code.get_by_code(
                db, code=authorization_code, client_id=client.client_id
            )
            if not db_code:
                return None

            # Check expiry
            if db_code.expires_at < time.time():
                logger.warning(
                    f"Authorization code expired for client {client.client_id}"
                )
                return None

            # Check if already used
            if db_code.is_used:
                logger.warning(
                    f"Authorization code already used for client {client.client_id}"
                )
                return None

            return AuthorizationCode(
                code=authorization_code,
                scopes=db_code.scopes or [],
                expires_at=db_code.expires_at,
                client_id=db_code.client_id,
                code_challenge=db_code.code_challenge,
                redirect_uri=db_code.redirect_uri,
                redirect_uri_provided_explicitly=db_code.redirect_uri_provided_explicitly,
                resource=db_code.resource,
            )
        finally:
            db.close()

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """Exchange an authorization code for access + refresh tokens."""
        db = _get_db()
        try:
            # Mark code as used
            db_code = crud_oauth_mcp_auth_code.get_by_code(
                db, code=authorization_code.code, client_id=client.client_id
            )
            if not db_code:
                raise TokenError(
                    error="invalid_grant",
                    error_description="Authorization code not found",
                )

            if db_code.is_used:
                raise TokenError(
                    error="invalid_grant",
                    error_description="Authorization code already used",
                )

            try:
                user = require_session_membership(db, db_code.user_id, lock=True)
                auth_context = authorize_session_record(
                    db, user, db_code, purpose="exchange"
                )
            except HTTPException as exc:
                raise TokenError(
                    error="invalid_grant", error_description=str(exc.detail)
                ) from exc
            db_code = crud_oauth_mcp_auth_code.get_by_code(
                db,
                code=authorization_code.code,
                client_id=client.client_id,
                lock=True,
            )
            if db_code is None or db_code.is_used:
                raise TokenError(
                    error="invalid_grant",
                    error_description="Authorization code no longer valid",
                )
            crud_oauth_mcp_auth_code.mark_used(db, obj=db_code, commit=False)

            # Generate tokens
            access_token_str = generate_token(32)
            refresh_token_str = generate_token(32)
            now = int(time.time())

            # Store access token
            crud_oauth_mcp_access_token.create(
                db,
                token=access_token_str,
                client_id=client.client_id,
                user_id=db_code.user_id,
                account_id=db_code.account_id,
                scopes=db_code.scopes or [],
                auth_context=auth_context,
                auth_generation=user_auth_generation(user),
                expires_at=now + self._access_token_expiry,
                resource=db_code.resource,
                commit=False,
            )

            # Store refresh token
            crud_oauth_mcp_refresh_token.create(
                db,
                token=refresh_token_str,
                client_id=client.client_id,
                user_id=db_code.user_id,
                account_id=db_code.account_id,
                scopes=db_code.scopes or [],
                auth_context=auth_context,
                auth_generation=user_auth_generation(user),
                expires_at=now + self._refresh_token_expiry,
            )

            logger.info(
                f"Exchanged auth code for tokens: client={client.client_id}, "
                f"user={db_code.user_id}"
            )

            return OAuthToken(
                access_token=access_token_str,
                token_type="Bearer",
                expires_in=self._access_token_expiry,
                scope=" ".join(db_code.scopes) if db_code.scopes else None,
                refresh_token=refresh_token_str,
            )
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # Refresh Token
    # -------------------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[RefreshToken]:
        """Load a refresh token from the database."""
        db = _get_db()
        try:
            db_token = crud_oauth_mcp_refresh_token.get_by_token(
                db, token=refresh_token
            )
            if not db_token:
                return None

            if db_token.is_revoked:
                return None

            if db_token.client_id != client.client_id:
                return None

            if db_token.expires_at and db_token.expires_at < int(time.time()):
                return None

            return RefreshToken(
                token=refresh_token,
                client_id=db_token.client_id,
                scopes=db_token.scopes or [],
                expires_at=db_token.expires_at,
            )
        finally:
            db.close()

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Exchange a refresh token for new access + refresh tokens (rotation)."""
        db = _get_db()
        try:
            # Look up the old refresh token to get user info
            db_old_refresh = crud_oauth_mcp_refresh_token.get_by_token(
                db, token=refresh_token.token
            )
            if not db_old_refresh:
                raise TokenError(
                    error="invalid_grant", error_description="Refresh token not found"
                )

            try:
                user = require_session_membership(db, db_old_refresh.user_id, lock=True)
                auth_context = authorize_session_record(
                    db, user, db_old_refresh, purpose="refresh"
                )
            except HTTPException as exc:
                raise TokenError(
                    error="invalid_grant", error_description=str(exc.detail)
                ) from exc
            db_old_refresh = crud_oauth_mcp_refresh_token.get_by_token(
                db,
                token=refresh_token.token,
                lock=True,
            )
            if (
                db_old_refresh is None
                or db_old_refresh.is_revoked
                or db_old_refresh.client_id != client.client_id
                or (
                    db_old_refresh.expires_at
                    and db_old_refresh.expires_at < int(time.time())
                )
            ):
                raise TokenError(
                    error="invalid_grant",
                    error_description="Refresh token no longer valid",
                )
            user_id = db_old_refresh.user_id
            account_id = db_old_refresh.account_id

            # Revoke old refresh token (rotation)
            crud_oauth_mcp_refresh_token.revoke(db, obj=db_old_refresh, commit=False)

            # Generate new tokens
            new_access_token_str = generate_token(32)
            new_refresh_token_str = generate_token(32)
            now = int(time.time())

            effective_scopes = scopes if scopes else (refresh_token.scopes or [])

            # Store new access token
            crud_oauth_mcp_access_token.create(
                db,
                token=new_access_token_str,
                client_id=client.client_id,
                user_id=user_id,
                account_id=account_id,
                scopes=effective_scopes,
                auth_context=auth_context,
                auth_generation=user_auth_generation(user),
                expires_at=now + self._access_token_expiry,
                commit=False,
            )

            # Store new refresh token
            crud_oauth_mcp_refresh_token.create(
                db,
                token=new_refresh_token_str,
                client_id=client.client_id,
                user_id=user_id,
                account_id=account_id,
                scopes=effective_scopes,
                auth_context=auth_context,
                auth_generation=user_auth_generation(user),
                expires_at=now + self._refresh_token_expiry,
            )

            logger.info(f"Rotated tokens for client={client.client_id}, user={user_id}")

            return OAuthToken(
                access_token=new_access_token_str,
                token_type="Bearer",
                expires_in=self._access_token_expiry,
                scope=" ".join(effective_scopes) if effective_scopes else None,
                refresh_token=new_refresh_token_str,
            )
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # Access Token Verification (called on every MCP request)
    # -------------------------------------------------------------------------

    async def load_access_token(self, token: str) -> Optional[AccessToken]:
        """Verify an access token — checks OAuth tokens first, then falls back
        to legacy API key / JWT validation for backward compatibility."""
        db = _get_db()
        try:
            # 1. Try OAuth token store
            db_token = crud_oauth_mcp_access_token.get_by_token(db, token=token)
            if db_token and not db_token.is_revoked:
                # Check expiry
                if db_token.expires_at and db_token.expires_at < int(time.time()):
                    return None

                try:
                    user = require_session_membership(db, str(db_token.user_id))
                    authorize_session_record(db, user, db_token, purpose="access")
                except HTTPException:
                    return None

                access_token = AccessToken(
                    token=token,
                    client_id=db_token.client_id,
                    scopes=db_token.scopes or [],
                    expires_at=db_token.expires_at,
                    resource=db_token.resource,
                )
                # Attach user info for downstream middleware
                object.__setattr__(access_token, "user", user)
                return access_token

            # 2. Fall back to legacy Bearer token (API key / JWT)
            from preloop.api.auth.jwt import get_user_from_token_if_valid
            from preloop.models.crud.managed_agent_auth import ManagedAgentPrincipal

            user = await get_user_from_token_if_valid(
                token, db, allow_managed_agent=True
            )
            if user:
                from preloop.api.auth.key_scopes import is_single_purpose_api_key

                api_key_obj = None
                if token and "." not in token:
                    from preloop.models.crud import crud_api_key

                    api_key_obj = getattr(user, "_auth_api_key", None)
                    if api_key_obj is None:
                        api_key_obj = crud_api_key.get_by_key(db, key=token)
                if is_single_purpose_api_key(api_key_obj):
                    return None
                access_token = AccessToken(
                    token=token,
                    client_id=str(
                        user.principal_id
                        if isinstance(user, ManagedAgentPrincipal)
                        else user.id
                    ),
                    scopes=[
                        scope
                        for scope in (api_key_obj.scopes or [])
                        if isinstance(scope, str)
                    ]
                    if api_key_obj
                    else [],
                    expires_at=None,
                )
                object.__setattr__(access_token, "user", user)

                # Also attach API key object if applicable
                if api_key_obj:
                    object.__setattr__(access_token, "api_key", api_key_obj)

                return access_token

            return None
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # Token Revocation
    # -------------------------------------------------------------------------

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke an access or refresh token and its counterpart."""
        db = _get_db()
        try:
            if isinstance(token, AccessToken):
                db_access = crud_oauth_mcp_access_token.get_by_token(
                    db, token=token.token
                )
                if db_access:
                    crud_oauth_mcp_access_token.revoke(db, obj=db_access)
                    # Also revoke associated refresh tokens
                    crud_oauth_mcp_refresh_token.revoke_by_user_and_client(
                        db, user_id=db_access.user_id, client_id=db_access.client_id
                    )
            elif isinstance(token, RefreshToken):
                db_refresh = crud_oauth_mcp_refresh_token.get_by_token(
                    db, token=token.token
                )
                if db_refresh:
                    crud_oauth_mcp_refresh_token.revoke(db, obj=db_refresh)
                    # Also revoke associated access tokens
                    crud_oauth_mcp_access_token.revoke_by_user_and_client(
                        db, user_id=db_refresh.user_id, client_id=db_refresh.client_id
                    )
        finally:
            db.close()

    # -------------------------------------------------------------------------
    # Helper: Create authorization code (called by consent page handler)
    # -------------------------------------------------------------------------

    def create_authorization_code_for_user(
        self,
        *,
        client_id: str,
        user_id,
        account_id,
        redirect_uri: str,
        redirect_uri_provided_explicitly: bool,
        code_challenge: str,
        scopes: list[str],
        resource: Optional[str] = None,
        auth_context: dict[str, Any] | None = None,
    ) -> str:
        """Create and store an authorization code for a user who has consented.

        This is called by the consent page handler after the user logs in
        and approves the authorization request.

        Returns the raw authorization code (to be included in the redirect).
        """
        db = _get_db()
        try:
            user = require_session_membership(db, user_id, lock=True)
            if str(user.account_id) != str(account_id):
                raise HTTPException(status_code=401, detail="Invalid account")
            auth_context = authorize_authentication(
                db, user, claims=auth_context, purpose="exchange"
            )
            code = generate_authorization_code()
            crud_oauth_mcp_auth_code.create(
                db,
                code=code,
                client_id=client_id,
                user_id=user_id,
                account_id=account_id,
                redirect_uri=redirect_uri,
                redirect_uri_provided_explicitly=redirect_uri_provided_explicitly,
                code_challenge=code_challenge,
                scopes=scopes,
                auth_context=auth_context,
                auth_generation=user_auth_generation(user),
                expires_at=time.time() + AUTH_CODE_EXPIRY,
                resource=resource,
            )
            logger.info(
                f"Created authorization code for user={user_id}, client={client_id}"
            )
            return code
        finally:
            db.close()
