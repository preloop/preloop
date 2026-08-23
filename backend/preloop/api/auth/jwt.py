"""Authentication for the API, including JWT tokens and API keys."""

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt import PyJWTError
from passlib.context import CryptContext
from sqlalchemy.orm import Session

# Configuration
from preloop.config import settings
from preloop.models.crud import (
    crud_api_key,
    crud_managed_agent,
    crud_runtime_session,
    crud_user,
)
from preloop.models.db.session import get_db_session
from preloop.models.models.managed_agent import ManagedAgent
from preloop.models.models.runtime_session import RuntimeSession
from preloop.models.models.user import User
from preloop.schemas.auth import TokenData

SECRET_KEY: str = settings.security.secret_key
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
# Refresh tokens rotate on every /refresh call (sliding window). Each rotation
# carries the original session start forward in the "sat" claim so the sliding
# window can be capped: an active user is never logged out inside
# MAX_SESSION_DAYS, and a stolen refresh token cannot be rotated forever.
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))
MAX_SESSION_DAYS = int(os.getenv("MAX_SESSION_DAYS", "30"))

# Password context for hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# OAuth2 for token authentication
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")
oauth2_scheme_optional = OAuth2PasswordBearer(
    tokenUrl="/api/v1/auth/token", auto_error=False
)

# Logger
logger = logging.getLogger(__name__)


def _token_log_fingerprint(token: str) -> str:
    """Return a non-reversible fingerprint for auth debug logs.

    Not used for password storage — only a truncated digest for correlating
    debug log lines without retaining the bearer token.
    """

    if not token:
        return "empty"
    # codeql[py/weak-sensitive-data-hashing] Log fingerprint only, not password storage
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class RuntimeBearerAuthContext:
    """Authenticated runtime bearer identity bound to a managed agent."""

    user: User
    api_key: Any
    runtime_session: RuntimeSession
    managed_agent: ManagedAgent


def _runtime_session_id_from_api_key(api_key: Any) -> Optional[uuid.UUID]:
    """Return the runtime session UUID bound to an API key, if any."""
    context_data = (
        api_key.context_data if isinstance(api_key.context_data, dict) else {}
    )
    runtime_session_id = context_data.get("runtime_session_id")
    if not runtime_session_id:
        return None
    try:
        return uuid.UUID(str(runtime_session_id))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid runtime session token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def _managed_agent_for_api_key(
    session: Any, api_key: Any, runtime_session: Optional[RuntimeSession] = None
) -> Optional[ManagedAgent]:
    """Resolve the managed agent linked to an API key, if any."""
    context_data = (
        api_key.context_data if isinstance(api_key.context_data, dict) else {}
    )
    managed_agent_id = context_data.get("managed_agent_id")
    if managed_agent_id:
        return crud_managed_agent.get_for_account(
            session, account_id=api_key.account_id, agent_id=managed_agent_id
        )

    runtime_principal = (
        context_data.get("runtime_principal")
        if isinstance(context_data.get("runtime_principal"), dict)
        else {}
    )
    principal_type = runtime_principal.get("type")
    principal_id = runtime_principal.get("id")
    if runtime_session is not None:
        principal_type = runtime_session.runtime_principal_type or principal_type
        principal_id = runtime_session.runtime_principal_id or principal_id
    if not principal_type or not principal_id:
        return None
    return crud_managed_agent.get_by_source(
        session,
        account_id=api_key.account_id,
        session_source_type=principal_type,
        session_source_id=principal_id,
    )


def _authenticate_with_api_key(
    session: Any, api_key: Any, *, allow_stale_runtime_session: bool = False
) -> User:
    """Validate an API key and return its active owner.

    ``allow_stale_runtime_session`` is for DURABLE managed-agent credentials
    (Agent Control WebSocket, native-tool permission checks): they outlive
    runtime sessions by design, so a missing or ended session binding must
    not reject the credential — callers resolve/reopen the agent's identity
    session instead.
    """
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not api_key.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key is inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if api_key.is_expired:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )

    runtime_session_id = _runtime_session_id_from_api_key(api_key)
    runtime_session = None
    if runtime_session_id is not None:
        runtime_session = crud_runtime_session.get_account_session(
            session,
            account_id=api_key.account_id,
            runtime_session_id=runtime_session_id,
        )
        if runtime_session is None and not allow_stale_runtime_session:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid runtime session token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if (
            runtime_session is not None
            and runtime_session.ended_at is not None
            and not allow_stale_runtime_session
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Runtime session has ended",
                headers={"WWW-Authenticate": "Bearer"},
            )

    context_data = (
        api_key.context_data if isinstance(api_key.context_data, dict) else {}
    )
    managed_agent = _managed_agent_for_api_key(
        session, api_key, runtime_session=runtime_session
    )
    if context_data.get("managed_agent_id"):
        if managed_agent is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Managed agent credential is invalid",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if managed_agent.lifecycle_state != "active":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Managed agent is not active",
                headers={"WWW-Authenticate": "Bearer"},
            )
    elif managed_agent is not None and managed_agent.lifecycle_state != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Managed agent is not active",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = crud_user.get(session, id=api_key.user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User associated with API key not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Inactive user",
            headers={"WWW-Authenticate": "Bearer"},
        )

    api_key.last_used_at = datetime.now(UTC)
    session.add(api_key)
    session.commit()
    return user


def authenticate_runtime_bearer_token(
    session: Any, token: str, *, enforce_current_binding: bool = True
) -> RuntimeBearerAuthContext:
    """Validate a runtime API key and return its managed-agent binding."""
    if not token or "." in token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runtime bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    api_key = crud_api_key.get_by_key(session, key=token)
    user = _authenticate_with_api_key(
        session, api_key, allow_stale_runtime_session=True
    )

    context_data = (
        api_key.context_data if isinstance(api_key.context_data, dict) else {}
    )
    runtime_session_id = _runtime_session_id_from_api_key(api_key)
    managed_agent_id = context_data.get("managed_agent_id")
    if not managed_agent_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runtime bearer token is not bound to a managed agent",
            headers={"WWW-Authenticate": "Bearer"},
        )

    managed_agent = crud_managed_agent.get_for_account(
        session,
        account_id=api_key.account_id,
        agent_id=managed_agent_id,
    )
    if managed_agent is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runtime bearer token binding is invalid",
            headers={"WWW-Authenticate": "Bearer"},
        )

    runtime_session = None
    if runtime_session_id is not None:
        runtime_session = crud_runtime_session.get_account_session(
            session,
            account_id=api_key.account_id,
            runtime_session_id=runtime_session_id,
        )
        if runtime_session is not None and runtime_session.ended_at is not None:
            # The bound session ended (operator action / expiry); fall through
            # to the identity-session resolution below rather than rejecting
            # a durable credential.
            runtime_session = None
    if runtime_session is None:
        # Durable managed-agent credentials are minted WITHOUT a session
        # binding (mirrors the loose resolution in agent_permission.py).
        # A long-lived Agent Control connection must survive session churn:
        # resolve — and reopen if ended — the agent's own identity session
        # instead of rejecting the plugin, which previously produced an
        # endless 403 reconnect loop for every credential minted after
        # sessions became lazy.
        principal = context_data.get("runtime_principal")
        principal = principal if isinstance(principal, dict) else {}
        now = datetime.now(UTC)
        runtime_session = crud_runtime_session.upsert_by_source(
            session,
            account_id=api_key.account_id,
            session_source_type=managed_agent.session_source_type,
            session_source_id=managed_agent.session_source_id,
            runtime_principal_type=principal.get("type"),
            runtime_principal_id=principal.get("id"),
            runtime_principal_name=principal.get("name"),
            started_at=now,
            last_activity_at=now,
            reopen_if_ended=True,
        )
        session.commit()
    if (
        enforce_current_binding
        and managed_agent.runtime_session_id is not None
        and str(managed_agent.runtime_session_id) != str(runtime_session.id)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Managed agent is not bound to this runtime session",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return RuntimeBearerAuthContext(
        user=user,
        api_key=api_key,
        runtime_session=runtime_session,
        managed_agent=managed_agent,
    )


def get_password_hash(password: str) -> str:
    """Hash a password.

    Args:
        password: Plain text password.

    Returns:
        Hashed password.
    """
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against a hash.

    Args:
        plain_password: Plain text password.
        hashed_password: Hashed password.

    Returns:
        True if the password matches the hash, False otherwise.
    """
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(
    data: Dict[str, Any], expires_delta: Optional[timedelta] = None
) -> str:
    """Create a JWT access token.

    Args:
        data: Data to encode in the token.
        expires_delta: Token expiration time delta.

    Returns:
        JWT access token.
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({"exp": expire})

    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(
    sub: str,
    scopes: Optional[List[str]] = None,
    session_started_at: Optional[datetime] = None,
) -> str:
    """Create a JWT refresh token carrying the session start claim.

    Args:
        sub: Subject (user id) for the token.
        scopes: OAuth scopes to carry through rotation.
        session_started_at: When this login session originally started. On
            first login this is now; on rotation the previous token's value is
            carried forward so MAX_SESSION_DAYS caps the sliding window.

    Returns:
        Encoded JWT refresh token.
    """
    started = session_started_at or datetime.now(UTC)
    return create_access_token(
        data={
            "sub": sub,
            "scopes": scopes or [],
            "refresh": True,
            "sat": int(started.timestamp()),
        },
        expires_delta=timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
    )


def decode_token(token: str) -> TokenData:
    """Decode a JWT token.

    Args:
        token: JWT token.

    Returns:
        Decoded token data containing user_id in the 'sub' field.

    Raises:
        HTTPException: If the token is invalid.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub: str = payload.get("sub", "")
        if not sub:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Note: 'sub' now contains user_id (UUID string) instead of username
        scopes = payload.get("scopes", [])
        exp = payload.get("exp")
        refresh = payload.get("refresh", False)
        sat = payload.get("sat")

        return TokenData(
            sub=sub,
            scopes=scopes,
            exp=datetime.fromtimestamp(exp) if exp else None,
            refresh=refresh,
            session_started_at=(datetime.fromtimestamp(sat, tz=UTC) if sat else None),
        )
    except PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db_session),
) -> User:
    """Get the current user from a JWT token or API key.

    Args:
        token: JWT token or API key.
        db: Database session.

    Returns:
        The current User object.

    Raises:
        HTTPException: If the token is invalid or the user doesn't exist.
    """
    logger.info(f"Authenticating token fingerprint: {_token_log_fingerprint(token)}")
    # If token looks like an API key (no periods, which JWT has), try API key first
    # Most API keys are random alphanumeric strings without dots
    if token and "." not in token:
        logger.info(
            "Token appears to be an API key (no . character), trying API key authentication first"
        )
        try:
            # Look up the API key using CRUD
            logger.info(
                f"Looking up API key fingerprint: {_token_log_fingerprint(token)}"
            )
            api_key = crud_api_key.get_by_key(db, key=token)

            if api_key:
                logger.info(
                    f"API key found: {api_key.name}, user_id: {api_key.user_id}"
                )

                user = _authenticate_with_api_key(db, api_key)

                logger.info(
                    f"API key authentication successful for user: {user.username}"
                )
                return user  # Return the full User object
        except HTTPException:
            # Re-raise HTTP exceptions
            raise
        except Exception as e:
            logger.error(f"Error in API key first-try authentication: {str(e)}")
            # Fall through to JWT authentication
            logger.info("API key authentication failed, falling back to JWT")

    # Try to authenticate with JWT token
    try:
        # Try to decode as JWT token
        logger.info("Attempting JWT authentication")
        token_data = decode_token(token)
        logger.info(f"JWT decoded successfully: {token_data}")

        # Check if it's a refresh token. decode_token() returns a TokenData
        # model (never a dict), so the refresh flag must be read as an
        # attribute — an isinstance(..., dict) guard here is dead code and
        # would let a 7-day refresh token be used as an access token.
        if getattr(token_data, "refresh", False):
            logger.warning("Attempted to use refresh token for authentication")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Cannot use refresh token for authentication",
                headers={"WWW-Authenticate": "Bearer"},
            )

        user_id_str = getattr(token_data, "sub", "")
        if not user_id_str:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Parse user_id as UUID
        try:
            user_id = uuid.UUID(user_id_str)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid user ID in token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Get user from database
        try:
            # Get user by ID
            user = crud_user.get(db, id=user_id)

            if not user:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="User not found",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            if not user.is_active:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Inactive user",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            return user  # Return the full User object
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting current user from JWT: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication error",
                headers={"WWW-Authenticate": "Bearer"},
            )
    except PyJWTError as e:
        # If JWT decoding fails and we haven't tried API key authentication yet, try it
        if (
            "." in token
        ):  # Only try API key auth if we haven't already (tokens with dots were tried as JWT first)
            logger.info(
                f"JWT authentication failed: {str(e)}, attempting API key authentication as fallback"
            )
            try:
                # Look up the API key using CRUD
                logger.info(
                    f"Looking up API key fingerprint: {_token_log_fingerprint(token)}"
                )
                api_key = crud_api_key.get_by_key(db, key=token)

                if not api_key:
                    logger.warning(
                        f"API key not found: {_token_log_fingerprint(token)}"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Invalid API key",
                        headers={"WWW-Authenticate": "Bearer"},
                    )

                logger.info(
                    f"API key found: {api_key.name}, user_id: {api_key.user_id}"
                )

                user = _authenticate_with_api_key(db, api_key)

                logger.info(
                    f"API key authentication successful for user: {user.username}"
                )
                return user  # Return the full User object
            except HTTPException:
                # Re-raise HTTP exceptions
                raise
            except Exception as e:
                logger.error(f"Error authenticating with API key: {str(e)}")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication error",
                    headers={"WWW-Authenticate": "Bearer"},
                )
        else:
            # We already tried API key authentication first for tokens without dots
            logger.error("Both JWT and API key authentication methods failed")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication token",
                headers={"WWW-Authenticate": "Bearer"},
            )


def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Get the current active user.

    Args:
        current_user: The current user.

    Returns:
        The current active User object.

    Raises:
        HTTPException: If the user is disabled.
    """
    # Disabled check is now handled in get_current_user
    return current_user


def get_current_active_user_optional(
    token: str = Depends(oauth2_scheme_optional),
    db: Session = Depends(get_db_session),
) -> Optional[User]:
    """
    Get the current active user if a valid token is provided, otherwise return None.
    This dependency will not raise an error if the token is missing or invalid.
    """
    if token is None:
        return None
    try:
        # We must call get_current_user with the token parameter, not as a dependency,
        # to bypass the strict oauth2_scheme it depends on.
        user = get_current_user(token=token, db=db)
        if user and user.is_active:
            return user
        return None
    except HTTPException:
        # Any authentication error (invalid, expired, etc.) should result in None
        return None
    except PyJWTError as e:
        logger.debug(
            "Token validation failed in get_current_active_user_optional: %s", e
        )
        return None
    except Exception as e:
        logger.warning("Unexpected error in get_current_active_user_optional: %s", e)
        return None


async def get_user_from_token_if_valid(token: str, db_session: Any) -> Optional[User]:
    """
    Manually attempts to retrieve a user from a token string.
    Returns None if the token is invalid, expired, or the user doesn't exist.
    This function does not raise HTTPException.
    """
    return get_user_from_token_if_valid_sync(token, db_session)


def get_user_from_token_if_valid_sync(token: str, db_session: Any) -> Optional[User]:
    """Sync variant for short-lived sessions in WebSocket handlers."""
    if not token:
        return None

    try:
        if "." not in token:
            api_key = crud_api_key.get_by_key(db_session, key=token)
            if api_key:
                return _authenticate_with_api_key(db_session, api_key)

        token_data = decode_token(token)
        # TokenData model, not a dict — read the refresh flag as an attribute so
        # refresh tokens can't authenticate WebSocket/gateway sessions.
        if getattr(token_data, "refresh", False):
            return None

        user_id_str = getattr(token_data, "sub", "")
        if not user_id_str:
            return None

        try:
            import uuid

            user_id = uuid.UUID(user_id_str)
        except ValueError:
            return None

        user = crud_user.get(db_session, id=user_id)
        if user and user.is_active:
            return user

    except HTTPException as e:
        # Raised by _authenticate_with_api_key with the concrete rejection
        # reason (inactive key, expired key, deleted/suspended managed agent).
        logger.info("Token rejected in get_user_from_token_if_valid: %s", e.detail)
    except PyJWTError as e:
        logger.debug("Token validation failed in get_user_from_token_if_valid: %s", e)
    except Exception as e:
        logger.warning("Unexpected error in get_user_from_token_if_valid: %s", e)

    return None
