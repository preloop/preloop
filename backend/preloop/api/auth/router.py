"""Authentication router for the API."""

import logging
import secrets
import string
from datetime import datetime, timedelta, UTC
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    decode_token,
    get_current_active_user,
    get_password_hash,
    verify_password,
)
from preloop.config import settings
from preloop.schemas.auth import (
    ApiKeyCreate,
    ApiKeyResponse,
    ApiKeySummary,
    ApiUsageStatistics,
    EmailVerificationRequest,
    LoginRequest,
    PasswordChangeRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    RefreshRequest,
    RuntimeSessionTokenCreate,
    RuntimeSessionTokenResponse,
    Token,
    AuthUserCreate,
    AuthUserResponse,
    AuthUserUpdate,
)
from preloop.schemas.subject_governance import (
    SubjectGovernanceConfig,
    SubjectGovernanceResponse,
)
from preloop.utils import get_client_ip
from preloop.utils.email import send_password_reset_email
from preloop.utils.tokens import (
    TokenError,
    create_password_reset_token,
    verify_token,
)
from preloop.models.crud import (
    crud_account,
    crud_audit_log,
    crud_user,
    crud_api_key,
    crud_api_usage,
    crud_managed_agent,
    crud_managed_agent_enrollment,
    crud_mcp_server,
    crud_role,
    crud_runtime_session,
    crud_user_role,
)
from preloop.models.db.session import get_db_session
from preloop.models.models.user import User as UserModel
from preloop.models.models.api_key import ApiKey
from pydantic import BaseModel
from preloop.services.account_realtime import (
    ACCOUNT_TOPIC_AUDIT,
    ACCOUNT_TOPIC_MANAGED_AGENTS,
    ACCOUNT_TOPIC_RUNTIME_SESSIONS,
    build_account_event,
    emit_account_event,
)
from preloop.services.account_setup_service import (
    complete_new_account_setup_background,
    notify_admins_user_login_after_inactivity,
    should_notify_on_login,
)
from preloop.services.subject_governance import (
    SUBJECT_TYPE_API_KEYS,
    get_subject_governance,
    set_subject_governance,
)


logger = logging.getLogger(__name__)
router = APIRouter()
RUNTIME_SESSION_SOURCE_TYPES = {
    "claude_code",
    "claude_desktop",
    "openclaw",
    "codex",
    "gemini_cli",
    "opencode",
    "hermes",
    "desktop_agent",
    "custom",
}
RUNTIME_SESSION_ALLOWED_SCOPES = ("mcp:read", "mcp:write")
API_KEY_ACTIVE_WINDOW = timedelta(minutes=10)
API_KEY_RECENT_WINDOW = timedelta(hours=24)


def _normalize_runtime_session_scopes(requested_scopes: List[str]) -> List[str]:
    """Return a deduplicated runtime-safe scope list."""
    if not requested_scopes:
        return list(RUNTIME_SESSION_ALLOWED_SCOPES)

    normalized_scopes: List[str] = []
    invalid_scopes: List[str] = []
    for scope in requested_scopes:
        if not isinstance(scope, str):
            invalid_scopes.append(str(scope))
            continue
        normalized_scope = scope.strip()
        if (
            not normalized_scope
            or normalized_scope not in RUNTIME_SESSION_ALLOWED_SCOPES
        ):
            invalid_scopes.append(scope)
            continue
        if normalized_scope not in normalized_scopes:
            normalized_scopes.append(normalized_scope)

    if invalid_scopes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Runtime session tokens only support these scopes: "
                + ", ".join(RUNTIME_SESSION_ALLOWED_SCOPES)
            ),
        )

    return normalized_scopes or list(RUNTIME_SESSION_ALLOWED_SCOPES)


def _resolve_user_permissions(user: UserModel, db: Session) -> Optional[List[str]]:
    """Return RBAC permissions, or None when RBAC is disabled/unavailable."""
    import os

    if os.getenv("DISABLE_RBAC", "false").lower() == "true":
        return None

    try:
        from preloop.plugins.proprietary.rbac.permissions import get_user_permissions
    except ModuleNotFoundError:
        return None

    return get_user_permissions(user, db)


def _api_key_activity_status(
    last_activity_at: Optional[datetime], *, is_active: bool
) -> str:
    if not is_active:
        return "revoked"
    if last_activity_at is None:
        return "idle"
    observed_at = (
        last_activity_at
        if last_activity_at.tzinfo is not None
        else last_activity_at.replace(tzinfo=UTC)
    )
    age = datetime.now(UTC) - observed_at.astimezone(UTC)
    if age <= API_KEY_ACTIVE_WINDOW:
        return "active_now"
    if age <= API_KEY_RECENT_WINDOW:
        return "recently_active"
    return "idle"


def _build_api_key_summary(session: Session, key: ApiKey) -> ApiKeySummary:
    context_data = key.context_data if isinstance(key.context_data, dict) else {}
    runtime_principal = (
        context_data.get("runtime_principal")
        if isinstance(context_data.get("runtime_principal"), dict)
        else {}
    )
    recent_start = datetime.now(UTC) - API_KEY_RECENT_WINDOW
    last_model_call = crud_api_usage.get_last_model_call_timestamp(
        session, api_key_id=key.id
    )
    recent_model_calls = crud_api_usage.get_recent_model_calls_count(
        session, api_key_id=key.id, recent_start=recent_start
    )

    from preloop.models.crud import crud_runtime_session_activity

    last_tool_call = crud_runtime_session_activity.get_last_tool_call_timestamp(
        session, api_key_id=key.id
    )
    recent_tool_calls = crud_runtime_session_activity.get_recent_tool_calls_count(
        session, api_key_id=key.id, recent_start=recent_start
    )
    candidate_times = []
    for value in (key.last_used_at, last_model_call, last_tool_call):
        if value:
            candidate_times.append(
                value if value.tzinfo is not None else value.replace(tzinfo=UTC)
            )
    last_activity_at = max(candidate_times) if candidate_times else None
    managed_agent_id = context_data.get("managed_agent_id")
    return ApiKeySummary(
        id=key.id,
        name=key.name,
        created_at=key.created_at,
        expires_at=key.expires_at,
        scopes=key.scopes,
        last_used_at=key.last_used_at,
        managed_agent_id=UUID(str(managed_agent_id)) if managed_agent_id else None,
        runtime_principal_type=runtime_principal.get("type"),
        runtime_principal_id=runtime_principal.get("id"),
        runtime_principal_name=runtime_principal.get("name"),
        last_activity_at=last_activity_at,
        activity_status=_api_key_activity_status(
            last_activity_at, is_active=bool(key.is_active)
        ),
        recent_model_calls=int(recent_model_calls or 0),
        recent_tool_calls=int(recent_tool_calls or 0),
    )


def _normalize_runtime_session_server_names(server_names: List[str]) -> List[str]:
    """Return deduplicated MCP server names."""
    normalized_names: List[str] = []
    for server_name in server_names:
        if not isinstance(server_name, str):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="allowed_mcp_servers must only contain strings",
            )
        normalized_name = server_name.strip()
        if not normalized_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="allowed_mcp_servers must not contain empty names",
            )
        if normalized_name not in normalized_names:
            normalized_names.append(normalized_name)
    return normalized_names


def _normalize_runtime_session_tool_names(requested_tools: List[Any]) -> List[str]:
    """Extract requested tool names from strings or tool spec objects."""
    normalized_names: List[str] = []
    for tool in requested_tools:
        tool_name: Optional[str]
        if isinstance(tool, str):
            tool_name = tool
        elif isinstance(tool, dict):
            candidate = tool.get("tool_name") or tool.get("name")
            tool_name = candidate if isinstance(candidate, str) else None
        else:
            tool_name = None

        if not tool_name or not tool_name.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "allowed_mcp_tools entries must be strings or objects with "
                    "'tool_name' or 'name'"
                ),
            )

        normalized_name = tool_name.strip()
        if normalized_name not in normalized_names:
            normalized_names.append(normalized_name)

    return normalized_names


def _resolve_runtime_session_tool_restrictions(
    db: Session,
    *,
    account_id: UUID,
    requested_server_names: List[str],
    requested_tools: List[Any],
) -> tuple[List[str], List[Dict[str, str]]]:
    """Resolve caller input into account-authorized runtime MCP restrictions."""
    normalized_server_names = _normalize_runtime_session_server_names(
        requested_server_names
    )
    normalized_tool_names = _normalize_runtime_session_tool_names(requested_tools)

    authorized_tool_names: List[str] = []
    server_ids: List[UUID] = []
    if normalized_server_names:
        active_servers = crud_mcp_server.get_active_by_account(
            db, account_id=str(account_id)
        )
        active_server_by_name = {server.name: server for server in active_servers}
        missing_servers = [
            server_name
            for server_name in normalized_server_names
            if server_name not in active_server_by_name
        ]
        if missing_servers:
            logger.warning(
                f"Ignoring missing or inactive allowed_mcp_servers requested by runtime session: {missing_servers}"
            )

        server_ids = [
            active_server_by_name[server_name].id
            for server_name in normalized_server_names
            if server_name in active_server_by_name
        ]

        from preloop.models.crud import crud_mcp_tool

        if server_ids:
            fetched_tool_names = crud_mcp_tool.get_tool_names_by_server_ids(
                db, server_ids
            )
            for name in fetched_tool_names:
                if name not in authorized_tool_names:
                    authorized_tool_names.append(name)

    if normalized_tool_names:
        from preloop.models.crud import crud_tool_configuration

        allowed_tool_names = crud_tool_configuration.get_enabled_tool_names(
            db,
            account_id=str(account_id),
            tool_names=normalized_tool_names,
            allowed_server_ids=server_ids,
        )
        missing_tool_names = [
            tool_name
            for tool_name in normalized_tool_names
            if tool_name not in allowed_tool_names
        ]
        if missing_tool_names:
            logger.warning(
                f"Ignoring missing or disabled allowed_mcp_tools requested by runtime session: {missing_tool_names}"
            )

        for tool_name in normalized_tool_names:
            if (
                tool_name in allowed_tool_names
                and tool_name not in authorized_tool_names
            ):
                authorized_tool_names.append(tool_name)

    return normalized_server_names, [
        {"tool_name": tool_name} for tool_name in authorized_tool_names
    ]


class OnboardingRequest(BaseModel):
    email: str
    username: str
    password: str


@router.post(
    "/register", response_model=AuthUserResponse, status_code=status.HTTP_201_CREATED
)
async def register(
    user_data: AuthUserCreate,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db_session),
) -> Dict[str, str]:
    """Register a new user.

    Args:
        user_data: User creation data.
        background_tasks: Background tasks for sending emails.
        request: The incoming request object.

    Returns:
        The created user.

    Raises:
        HTTPException: If the username or email is already taken, or if registration is disabled.
    """
    # Check if registration is enabled
    if not settings.registration_enabled:
        logger.warning(
            f"[REGISTER] Registration attempt blocked - registration is disabled. "
            f"Username: {user_data.username}, Email: {user_data.email}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is disabled. Please contact an administrator for an invitation.",
        )

    logger.info(f"[REGISTER] Starting registration for username: {user_data.username}")

    # Check if username or email already exists
    # Since get_db_session() doesn't support async with, we'll use a manual approach
    logger.info("[REGISTER] Getting database session")
    session = db
    logger.info("[REGISTER] Database session acquired")

    # Check if username exists using CRUD layer
    logger.info("[REGISTER] Checking if username exists")
    existing_user = crud_user.get_by_username(session, username=user_data.username)
    logger.info(
        f"[REGISTER] Username check complete, exists: {existing_user is not None}"
    )
    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already registered",
        )

    # Check if email exists using CRUD layer
    logger.info("[REGISTER] Checking if email exists")
    existing_email = crud_user.get_by_email(session, email=user_data.email)
    logger.info(
        f"[REGISTER] Email check complete, exists: {existing_email is not None}"
    )
    if existing_email is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    # Create organization (Account) first
    logger.info("[REGISTER] Creating account")
    account_data = {
        "organization_name": f"{user_data.username}'s Organization",
        "is_active": True,
    }
    new_account = crud_account.create(session, obj_in=account_data)
    logger.info(f"[REGISTER] Account created with ID: {new_account.id}")

    # Create user linked to the account
    logger.info("[REGISTER] Hashing password")
    hashed_password = get_password_hash(user_data.password)
    logger.info("[REGISTER] Password hashed")
    logger.info("[REGISTER] Creating user")
    user_dict = {
        "account_id": new_account.id,
        "username": user_data.username,
        "email": user_data.email,
        "hashed_password": hashed_password,
        "full_name": user_data.full_name,
        "is_active": True,
        "email_verified": False,
        "user_source": "local",
    }
    new_user = crud_user.create(session, obj_in=user_dict)
    logger.info(f"[REGISTER] User created with ID: {new_user.id}")

    # Set this user as the primary user for the account
    logger.info("[REGISTER] Setting primary_user_id on account")
    new_account.primary_user_id = new_user.id
    session.add(new_account)
    logger.info(
        f"[REGISTER] Set user {new_user.id} as primary user for account {new_account.id}"
    )

    # Assign Owner role to the first user
    logger.info("[REGISTER] Looking up owner role")
    owner_role = crud_role.get_by_name(session, name="owner")
    logger.info(f"[REGISTER] Owner role found: {owner_role is not None}")
    if owner_role:
        user_role_data = {
            "user_id": new_user.id,
            "role_id": owner_role.id,
        }
        crud_user_role.create(session, obj_in=user_role_data)
        logger.info(f"Assigned Owner role to user {new_user.username}")
    else:
        logger.warning(
            "Owner role not found in database - user will have no permissions"
        )

    try:
        logger.info("[REGISTER] Committing transaction")
        session.commit()
        logger.info("[REGISTER] Transaction committed, refreshing objects")
        session.refresh(new_user)
        session.refresh(new_account)
        logger.info("[REGISTER] Objects refreshed")

        # Schedule all post-account-creation tasks (verification email,
        # default approval workflow, admin notifications)
        logger.info("[REGISTER] Scheduling account setup tasks")
        background_tasks.add_task(
            complete_new_account_setup_background,
            account_id=new_account.id,
            user_id=new_user.id,
            user_email=new_user.email,
            username=new_user.username,
            full_name=new_user.full_name,
            organization_name=new_account.organization_name,
            signup_source="standard",
            source_ip=get_client_ip(request),
            send_verification=True,
        )
        logger.info("[REGISTER] Account setup tasks scheduled")

        logger.info("[REGISTER] Registration complete, returning response")
        return {
            "username": new_user.username,
            "email": new_user.email,
            "full_name": new_user.full_name,
            "email_verified": new_user.email_verified,
        }
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Error creating user - username or email may be taken",
        )
    except Exception as e:
        session.rollback()
        logger.error(f"Error registering user: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error registering user",
        )


@router.post("/verify-email", status_code=status.HTTP_200_OK)
async def verify_email(
    verification_data: EmailVerificationRequest,
    db: Session = Depends(get_db_session),
) -> Dict[str, str]:
    """Verify a user's email address.

    Args:
        verification_data: Email verification data with token.

    Returns:
        Success message.

    Raises:
        HTTPException: If the token is invalid or the user does not exist.
    """
    try:
        # Verify the token
        email = verify_token(verification_data.token, "email_verification")

        # Find and update the user
        session = db

        # Find the user using CRUD layer
        user = crud_user.get_by_email(session, email=email)

        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )

        # Update email verification status
        user.email_verified = True
        session.commit()

        return {"message": "Email verified successfully"}
    except TokenError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Error verifying email: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error verifying email",
        )


@router.post("/forgot-password", status_code=status.HTTP_200_OK)
async def forgot_password(
    reset_data: PasswordResetRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db_session),
) -> Dict[str, str]:
    """Send a password reset email.

    Args:
        reset_data: Password reset request with email.
        background_tasks: Background tasks for sending emails.

    Returns:
        Success message.
    """
    # Always return success even if email doesn't exist (security best practice)
    # But only send email if user exists
    session = db

    # Find user using CRUD layer
    user = crud_user.get_by_email(session, email=reset_data.email)

    if user:
        # Generate password reset token
        token = create_password_reset_token(reset_data.email)

        # Send password reset email as a background task
        background_tasks.add_task(
            send_password_reset_email, user_email=reset_data.email, token=token
        )
    return {
        "message": "If your email is registered, you will receive a password reset link"
    }


@router.post("/reset-password", status_code=status.HTTP_200_OK)
async def reset_password(
    reset_data: PasswordResetConfirmRequest,
    db: Session = Depends(get_db_session),
) -> Dict[str, str]:
    """Reset a user's password.

    Args:
        reset_data: Password reset confirmation with token and new password.

    Returns:
        Success message.

    Raises:
        HTTPException: If the token is invalid or the user does not exist.
    """
    try:
        # Verify the token
        email = verify_token(reset_data.token, "password_reset")

        # Find and update the user
        session = db

        # Find user using CRUD layer
        user = crud_user.get_by_email(session, email=email)

        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )

        # Update password
        user.hashed_password = get_password_hash(reset_data.new_password)
        session.commit()

        return {"message": "Password reset successfully"}
    except TokenError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        logger.error(f"Error resetting password: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error resetting password",
        )


@router.post("/token", response_model=Token)
async def login_form(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db_session),
) -> Dict[str, str]:
    """Login to get an access token using form data (required for OAuth2 flow).

    Args:
        request: The incoming request object.
        form_data: OAuth2 password request form.

    Returns:
        Access token.

    Raises:
        HTTPException: If the username or password is incorrect.
    """
    user = await authenticate_user(
        form_data.username, form_data.password, source_ip=get_client_ip(request), db=db
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Create access token with user information
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": str(user.id), "scopes": form_data.scopes or []},
        expires_delta=access_token_expires,
    )

    # Create refresh token with longer expiration
    refresh_token_expires = timedelta(days=7)  # 7 days
    refresh_token = create_access_token(
        data={"sub": str(user.id), "scopes": form_data.scopes or [], "refresh": True},
        expires_delta=refresh_token_expires,
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,  # in seconds
    }


@router.post("/token/json", response_model=Token)
async def login_json(
    http_request: Request,
    request: LoginRequest,
    db: Session = Depends(get_db_session),
) -> Dict[str, str]:
    """Login to get an access token using JSON data.

    Args:
        http_request: The incoming HTTP request object.
        request: Login request with username and password.

    Returns:
        Access token.

    Raises:
        HTTPException: If the username or password is incorrect.
    """
    user = await authenticate_user(
        request.username, request.password, source_ip=get_client_ip(http_request), db=db
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Create access token with user information
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": str(user.id), "scopes": []},
        expires_delta=access_token_expires,
    )

    # Create refresh token with longer expiration
    refresh_token_expires = timedelta(days=7)  # 7 days
    refresh_token = create_access_token(
        data={"sub": str(user.id), "scopes": [], "refresh": True},
        expires_delta=refresh_token_expires,
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,  # in seconds
    }


@router.post("/refresh", response_model=Token)
async def refresh_token(
    request: RefreshRequest,
    db: Session = Depends(get_db_session),
) -> Dict[str, str]:
    """Refresh an access token using a refresh token.

    Args:
        request: Refresh token request.

    Returns:
        New access token.

    Raises:
        HTTPException: If the refresh token is invalid or expired.
    """
    # Get synchronous database session
    session = db

    try:
        # Decode and validate the refresh token
        token_data = decode_token(request.refresh_token)

        # Parse user_id from token
        try:
            user_id = UUID(token_data.sub)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid user ID in token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Verify user exists and is active using CRUD layer
        user = crud_user.get(session, id=user_id)

        if user is None or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or inactive",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Check if it's a refresh token
        if not token_data.refresh:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Create a new access token
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": token_data.sub, "scopes": token_data.scopes},
            expires_delta=access_token_expires,
        )

        # Create a new refresh token
        refresh_token_expires = timedelta(days=7)  # 7 days
        refresh_token = create_access_token(
            data={"sub": token_data.sub, "scopes": token_data.scopes, "refresh": True},
            expires_delta=refresh_token_expires,
        )

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,  # in seconds
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid refresh token: {str(e)}",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.get("/users/me", response_model=AuthUserResponse)
def read_users_me(
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> AuthUserResponse:
    """Get the current user.

    Args:
        current_user: The current user.

    Returns:
        The current user.
    """
    return AuthUserResponse(
        username=current_user.username,
        email=current_user.email,
        full_name=current_user.full_name,
        email_verified=current_user.email_verified,
        is_superuser=bool(current_user.is_superuser),
        permissions=_resolve_user_permissions(current_user, db),
    )


@router.put("/users/me", response_model=AuthUserResponse)
def update_user_me(
    *,
    db: Session = Depends(get_db_session),
    user_update: AuthUserUpdate,
    current_user: UserModel = Depends(get_current_active_user),
) -> Any:
    """Update own user."""
    user = crud_user.update(db, db_obj=current_user, obj_in=user_update)
    return user


@router.put("/users/me/password", status_code=status.HTTP_204_NO_CONTENT)
def change_current_user_password(
    passwords: PasswordChangeRequest,
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Change current user's password."""
    if not verify_password(passwords.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect current password",
        )
    hashed_password = get_password_hash(passwords.new_password)
    crud_user.update(
        db, db_obj=current_user, obj_in={"hashed_password": hashed_password}
    )


@router.post(
    "/api-keys", response_model=ApiKeyResponse, status_code=status.HTTP_201_CREATED
)
def create_api_key(
    key_data: ApiKeyCreate,
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ApiKeyResponse:
    """Create a new API key.

    Args:
        key_data: The key creation data.
        current_user: The current authenticated user.

    Returns:
        The created API key details.
    """
    # Generate a secure random key
    alphabet = string.ascii_letters + string.digits
    key_value = "".join(secrets.choice(alphabet) for _ in range(40))

    session = db

    def _is_duplicate_name_for_account_error(err: IntegrityError) -> bool:
        orig = getattr(err, "orig", None)
        diag = getattr(orig, "diag", None)
        constraint_name = getattr(diag, "constraint_name", None)
        if constraint_name == "uix_api_key_account_id_name":
            return True

        msg = str(orig) if orig is not None else str(err)
        return (
            "uix_api_key_account_id_name" in msg
            or "UNIQUE constraint failed: api_key.account_id, api_key.name" in msg
            or ("api_key.account_id" in msg and "api_key.name" in msg)
        )

    try:
        # Create a new API key
        new_key = ApiKey(
            name=key_data.name,
            key=key_value,
            key_hash=crud_api_key.build_key_hash(key_value),
            key_prefix=crud_api_key.build_key_prefix(key_value),
            scopes=key_data.scopes,
            account_id=current_user.account_id,
            user_id=current_user.id,
            expires_at=key_data.expires_at,
        )

        session.add(new_key)
        session.commit()
        session.refresh(new_key)

        return ApiKeyResponse(
            id=new_key.id,
            name=new_key.name,
            key=new_key.key,
            created_at=new_key.created_at,
            expires_at=new_key.expires_at,
            scopes=new_key.scopes,
            user_id=new_key.user_id,
            last_used_at=new_key.last_used_at,
        )
    except IntegrityError as e:
        session.rollback()
        if _is_duplicate_name_for_account_error(e):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="API key with this name already exists",
            )

        logger.error(f"Integrity error creating API key: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error creating API key",
        )
    except Exception as e:
        session.rollback()
        logger.error(f"Error creating API key: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error creating API key",
        )


@router.post(
    "/runtime-sessions/token",
    response_model=RuntimeSessionTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_runtime_session_token(
    session_data: RuntimeSessionTokenCreate,
    current_user: UserModel = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> RuntimeSessionTokenResponse:
    """Create a short-lived runtime token bound to a shared runtime session."""
    if session_data.session_source_type not in RUNTIME_SESSION_SOURCE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Unsupported session_source_type. Expected one of: "
                + ", ".join(sorted(RUNTIME_SESSION_SOURCE_TYPES))
            ),
        )

    scopes = _normalize_runtime_session_scopes(session_data.scopes)
    allowed_mcp_servers, allowed_mcp_tools = _resolve_runtime_session_tool_restrictions(
        db,
        account_id=current_user.account_id,
        requested_server_names=session_data.allowed_mcp_servers,
        requested_tools=session_data.allowed_mcp_tools,
    )

    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=session_data.expires_in_minutes)
    runtime_principal_id = (
        session_data.runtime_principal_id or session_data.session_source_id
    )
    runtime_principal_name = (
        session_data.runtime_principal_name
        or session_data.session_reference
        or runtime_principal_id
    )
    existing_managed_agent = crud_managed_agent.get_by_source(
        db,
        account_id=str(current_user.account_id),
        session_source_type=session_data.session_source_type,
        session_source_id=runtime_principal_id,
    )
    if (
        existing_managed_agent is not None
        and existing_managed_agent.lifecycle_state in {"suspended", "decommissioned"}
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Runtime token issuance is blocked for "
                f"{existing_managed_agent.lifecycle_state} agents. "
                "Resume or reenroll the agent first."
            ),
        )
    existing_runtime_session = crud_runtime_session.get_by_source(
        db,
        account_id=current_user.account_id,
        session_source_type=session_data.session_source_type,
        session_source_id=session_data.session_source_id,
    )

    runtime_session = crud_runtime_session.upsert_by_source(
        db,
        account_id=current_user.account_id,
        session_source_type=session_data.session_source_type,
        session_source_id=session_data.session_source_id,
        session_reference=session_data.session_reference,
        runtime_principal_type=session_data.session_source_type,
        runtime_principal_id=runtime_principal_id,
        runtime_principal_name=runtime_principal_name,
        last_activity_at=now,
        started_at=now,
        reopen_if_ended=True,
    )
    managed_agent = crud_managed_agent.upsert_from_runtime_session(
        db,
        account_id=current_user.account_id,
        runtime_session_id=runtime_session.id,
        session_source_type=session_data.session_source_type,
        session_source_id=runtime_principal_id,
        display_name=runtime_principal_name,
        session_reference=session_data.session_reference,
        managed_mcp_servers=allowed_mcp_servers,
        last_seen_at=now,
        owner_user_id=current_user.id,
    )
    db.commit()
    db.refresh(runtime_session)
    db.refresh(managed_agent)
    crud_managed_agent_enrollment.upsert_runtime_bootstrap(
        db,
        account_id=current_user.account_id,
        agent_id=managed_agent.id,
        created_by_user_id=current_user.id,
        session_source_type=session_data.session_source_type,
        session_source_id=session_data.session_source_id,
        session_reference=session_data.session_reference,
        display_name=runtime_principal_name,
        managed_mcp_servers=allowed_mcp_servers,
        runtime_session_id=runtime_session.id,
        commit=False,
    )

    api_key, token_value = crud_api_key.create_runtime_key(
        db,
        name=f"Managed Agent {runtime_principal_name} [{runtime_principal_id}]",
        account_id=current_user.account_id,
        user_id=current_user.id,
        scopes=scopes,
        expires_at=expires_at,
        context_data={
            "runtime_session_id": str(runtime_session.id),
            "managed_agent_id": str(managed_agent.id),
            "allowed_mcp_tools": allowed_mcp_tools,
            "allowed_mcp_servers": allowed_mcp_servers,
            "runtime_principal": {
                "type": session_data.session_source_type,
                "id": runtime_principal_id,
                "name": runtime_principal_name,
                "user_id": str(current_user.id),
                "username": current_user.username,
            },
        },
    )

    try:
        from preloop.plugins.base import get_plugin_manager

        plugin_manager = get_plugin_manager()
        audit_service = plugin_manager.get_service("audit_service")
        if audit_service:
            event_name = "created" if existing_runtime_session is None else "updated"
            audit_service.log_runtime_session_event(
                db=db,
                account_id=current_user.account_id,
                runtime_session_id=runtime_session.id,
                event=event_name,
                session_source_type=runtime_session.session_source_type,
                session_source_id=runtime_session.session_source_id,
                session_reference=runtime_session.session_reference,
                runtime_principal_type=runtime_session.runtime_principal_type,
                runtime_principal_id=runtime_session.runtime_principal_id,
                runtime_principal_name=runtime_session.runtime_principal_name,
                api_key_id=api_key.id,
                api_key_name=(
                    f"Runtime Session {session_data.session_source_type}:"
                    f"{session_data.session_source_id}"
                ),
                user_id=current_user.id,
            )
            emit_account_event(
                build_account_event(
                    account_id=str(current_user.account_id),
                    topic=ACCOUNT_TOPIC_AUDIT,
                    event_type="audit_event",
                    payload={
                        "action": f"runtime_session_{event_name}",
                        "runtime_session_id": str(runtime_session.id),
                        "session_source_type": runtime_session.session_source_type,
                        "session_source_id": runtime_session.session_source_id,
                        "session_reference": runtime_session.session_reference,
                        "runtime_principal_type": runtime_session.runtime_principal_type,
                        "runtime_principal_id": runtime_session.runtime_principal_id,
                        "runtime_principal_name": runtime_session.runtime_principal_name,
                        "api_key_id": str(api_key.id),
                        "api_key_name": api_key.name,
                    },
                    runtime_session_id=str(runtime_session.id),
                )
            )
    except Exception:
        logger.debug("Failed to audit runtime session token creation", exc_info=True)

    session_event_type = (
        "runtime_session_created"
        if existing_runtime_session is None
        else "runtime_session_updated"
    )
    emit_account_event(
        build_account_event(
            account_id=str(current_user.account_id),
            topic=ACCOUNT_TOPIC_RUNTIME_SESSIONS,
            event_type=session_event_type,
            payload={
                "runtime_session_id": str(runtime_session.id),
                "session_source_type": runtime_session.session_source_type,
                "session_source_id": runtime_session.session_source_id,
                "session_reference": runtime_session.session_reference,
                "runtime_principal_type": runtime_session.runtime_principal_type,
                "runtime_principal_id": runtime_session.runtime_principal_id,
                "runtime_principal_name": runtime_session.runtime_principal_name,
                "started_at": runtime_session.started_at.isoformat()
                if runtime_session.started_at
                else None,
                "last_activity_at": runtime_session.last_activity_at.isoformat()
                if runtime_session.last_activity_at
                else None,
            },
            runtime_session_id=str(runtime_session.id),
        )
    )
    emit_account_event(
        build_account_event(
            account_id=str(current_user.account_id),
            topic=ACCOUNT_TOPIC_MANAGED_AGENTS,
            event_type=(
                "managed_agent_created"
                if existing_runtime_session is None
                else "managed_agent_updated"
            ),
            payload={
                "agent_id": str(managed_agent.id),
                "runtime_session_id": str(managed_agent.runtime_session_id)
                if managed_agent.runtime_session_id
                else None,
                "display_name": managed_agent.display_name,
                "session_source_type": managed_agent.session_source_type,
                "session_source_id": managed_agent.session_source_id,
                "session_reference": managed_agent.session_reference,
                "managed_mcp_servers": managed_agent.managed_mcp_servers,
                "last_seen_at": managed_agent.last_seen_at.isoformat(),
            },
            runtime_session_id=str(runtime_session.id),
        )
    )

    return RuntimeSessionTokenResponse(
        runtime_session_id=runtime_session.id,
        token=token_value,
        expires_at=expires_at,
        session_source_type=runtime_session.session_source_type,
        session_source_id=runtime_session.session_source_id,
        session_reference=runtime_session.session_reference,
    )


@router.get("/api-keys", response_model=List[ApiKeySummary])
def list_api_keys(
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> List[ApiKeySummary]:
    """List all API keys for the current user.

    Args:
        current_user: The current authenticated user.

    Returns:
        List of API keys.
    """
    session = db

    # Get API keys using CRUD layer
    keys = crud_api_key.get_by_user(session, username=current_user.username)

    return [_build_api_key_summary(session, key) for key in keys]


@router.get("/api-keys/{key_id}", response_model=ApiKeySummary)
def get_api_key(
    key_id: UUID,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ApiKeySummary:
    """Get a specific API key for the current user.

    Args:
        key_id: The API key ID.
        current_user: The current authenticated user.
        db: The database session.

    Returns:
        The API key summary.
    """
    # Get API key using CRUD layer
    key = crud_api_key.get_by_id_and_user(
        db, key_id=key_id, username=current_user.username
    )
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    return _build_api_key_summary(db, key)


@router.get("/api-keys/{key_id}/activity")
def get_api_key_activity(
    key_id: UUID,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> List[Dict[str, Any]]:
    """Get recent activity for an API key.

    Args:
        key_id: The API key ID.
        current_user: The current authenticated user.
        db: The database session.

    Returns:
        A list of recent API usage logs.
    """
    # Verify ownership
    key = crud_api_key.get_by_id_and_user(
        db, key_id=key_id, username=current_user.username
    )
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    from preloop.models.crud.api_usage import crud_api_usage

    return crud_api_usage.get_gateway_usage_by_api_key(
        db, api_key_id=str(key_id), limit=50
    )


@router.get("/api-keys/{key_id}/gateway-usage/summary")
def get_api_key_gateway_usage_summary(
    key_id: UUID,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> Any:
    """Get gateway usage summary for an API key."""
    key = crud_api_key.get_by_id_and_user(
        db, key_id=key_id, username=current_user.username
    )
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")

    from preloop.services.model_gateway_usage import ModelGatewayUsageService

    return ModelGatewayUsageService(db).get_api_key_summary(
        api_key=key,
        start_date=start_date,
        end_date=end_date,
    )


@router.get(
    "/api-keys/{key_id}/governance",
    response_model=SubjectGovernanceResponse,
)
def get_api_key_governance(
    key_id: UUID,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> SubjectGovernanceResponse:
    key = crud_api_key.get_by_id_and_user(
        db, key_id=key_id, username=current_user.username
    )
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    account = crud_account.get(db, id=current_user.account_id)
    return SubjectGovernanceResponse(
        subject_type=SUBJECT_TYPE_API_KEYS,
        subject_id=str(key_id),
        config=SubjectGovernanceConfig.model_validate(
            get_subject_governance(
                (account.meta_data or {}) if account else {},
                subject_type=SUBJECT_TYPE_API_KEYS,
                subject_id=str(key_id),
            )
        ),
    )


@router.put(
    "/api-keys/{key_id}/governance",
    response_model=SubjectGovernanceResponse,
)
def update_api_key_governance(
    key_id: UUID,
    payload: SubjectGovernanceConfig,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> SubjectGovernanceResponse:
    key = crud_api_key.get_by_id_and_user(
        db, key_id=key_id, username=current_user.username
    )
    if key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    account = crud_account.get(db, id=current_user.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    account.meta_data = set_subject_governance(
        account.meta_data or {},
        subject_type=SUBJECT_TYPE_API_KEYS,
        subject_id=str(key_id),
        config=payload.model_dump(),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return SubjectGovernanceResponse(
        subject_type=SUBJECT_TYPE_API_KEYS,
        subject_id=str(key_id),
        config=SubjectGovernanceConfig.model_validate(
            get_subject_governance(
                account.meta_data or {},
                subject_type=SUBJECT_TYPE_API_KEYS,
                subject_id=str(key_id),
            )
        ),
    )


@router.get("/api-keys/debug", response_model=List[ApiKeyResponse])
def debug_api_keys(
    username: str,
    api_key: Optional[str] = None,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> List[ApiKeyResponse]:
    """Debug endpoint to get API keys with their values (admin only).

    Args:
        username: The username to get keys for
        api_key: Optional specific API key to look up
        current_user: The current authenticated user.

    Returns:
        List of API keys with their values.
    """
    # This is for debugging only
    session = db

    try:
        # Check if specific key was requested
        if api_key:
            logger.info(f"Looking up specific API key: {api_key[:10]}...")
            # Get specific key using CRUD layer
            key = crud_api_key.get_by_key(session, key=api_key)
            return (
                [
                    ApiKeyResponse(
                        id=key.id
                        if key
                        else UUID("00000000-0000-0000-0000-000000000000"),
                        name=key.name if key else "Not Found",
                        key=key.key if key else api_key,
                        created_at=key.created_at if key else datetime.now(UTC),
                        expires_at=key.expires_at if key else None,
                        scopes=key.scopes if key else [],
                        user_id=key.user_id if key else uuid.uuid4(),
                        last_used_at=key.last_used_at if key else None,
                    )
                ]
                if key
                else []
            )

        # Get all keys for the specified user using CRUD layer
        keys = crud_api_key.get_by_user(session, username=username)

        return [
            ApiKeyResponse(
                id=key.id,
                name=key.name,
                key=key.key,
                created_at=key.created_at,
                expires_at=key.expires_at,
                scopes=key.scopes,
                user_id=key.user_id,
                last_used_at=key.last_used_at,
            )
            for key in keys
        ]
    except Exception as e:
        logger.error(f"Error debugging API keys: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error debugging API keys: {str(e)}",
        )


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_api_key(
    key_id: UUID,
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> None:
    """Delete an API key.

    Args:
        key_id: The ID of the key to delete.
        current_user: The current authenticated user.

    Raises:
        HTTPException: If the key doesn't exist or doesn't belong to the user.
    """
    session = db

    try:
        # Get the key using CRUD layer
        key = crud_api_key.get_by_id_and_user(
            session, key_id=key_id, username=current_user.username
        )

        if not key:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="API key not found",
            )

        # Delete the key
        session.delete(key)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Error deleting API key: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error deleting API key",
        )


@router.get("/api-usage", response_model=ApiUsageStatistics)
def get_api_usage(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    current_user: AuthUserResponse = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ApiUsageStatistics:
    """Get API usage statistics for the current user.

    Args:
        start_date: Optional start date for filtering.
        end_date: Optional end date for filtering.
        current_user: The current authenticated user.

    Returns:
        API usage statistics.
    """
    session = db

    # Get usage entries using CRUD layer
    usage_entries = crud_api_usage.get_for_user_filtered(
        session,
        username=current_user.username,
        start_date=start_date,
        end_date=end_date,
    )

    # Calculate statistics
    total_requests = len(usage_entries)

    # Group by date
    requests_by_date = {}
    for entry in usage_entries:
        date_str = entry.timestamp.strftime("%Y-%m-%d")
        requests_by_date[date_str] = requests_by_date.get(date_str, 0) + 1

    # Count issue actions
    issues_created = sum(
        1 for entry in usage_entries if entry.action_type == "create_issue"
    )
    issues_updated = sum(
        1 for entry in usage_entries if entry.action_type == "update_issue"
    )
    issues_closed = sum(
        1 for entry in usage_entries if entry.action_type == "close_issue"
    )

    # Group by endpoint
    requests_by_endpoint = {}
    for entry in usage_entries:
        endpoint = entry.endpoint
        requests_by_endpoint[endpoint] = requests_by_endpoint.get(endpoint, 0) + 1

    return ApiUsageStatistics(
        total_requests=total_requests,
        requests_by_date=requests_by_date,
        issues_created=issues_created,
        issues_updated=issues_updated,
        issues_closed=issues_closed,
        requests_by_endpoint=requests_by_endpoint,
    )


async def authenticate_user(
    username: str, password: str, db: Session, source_ip: Optional[str] = None
) -> Optional[UserModel]:
    """Authenticate a user.

    Args:
        username: The username.
        password: The password.
        source_ip: The IP address of the login request (optional).

    Returns:
        The user if authentication is successful, None otherwise.
    """
    from datetime import datetime, timezone
    import threading

    session = db

    # Find user using CRUD layer
    user = crud_user.get_by_username(session, username=username)

    if not user:
        return None

    if not verify_password(password, user.hashed_password):
        return None

    if not user.is_active:
        return None

    # Capture old last_login before updating (for inactivity notification)
    old_last_login = user.last_login

    # Update last_login timestamp
    user.last_login = datetime.now(timezone.utc)
    session.commit()
    session.refresh(user)

    # Cohort telemetry: record a login event so the 7-day-return launch metric
    # is queryable (last_login is updated in place and keeps no history).
    # NOTE: audit_log is compliance-shaped (append-only, retention/PII rules);
    # a dedicated user_activity_event table is the eventual clean home (deferred).
    try:
        crud_audit_log.log_action(
            session,
            account_id=user.account_id,
            user_id=user.id,
            action="user.login",
            resource_type="user",
            resource_id=str(user.id),
            status="success",
            ip_address=source_ip,
        )
    except Exception:
        logger.debug("Failed to audit user login", exc_info=True)

    # Check if we should notify admins about login after inactivity
    if (
        should_notify_on_login(old_last_login, days_threshold=7)
        and source_ip != "testclient"
    ):
        # Capture string values before creating thread to avoid accessing
        # detached ORM object after session.close() in finally block
        username_str = user.username
        email_str = user.email

        # Run notification in background thread to avoid blocking login
        def send_login_notification():
            try:
                notify_admins_user_login_after_inactivity(
                    username=username_str,
                    email=email_str,
                    last_login=old_last_login,
                    source_ip=source_ip,
                )
            except Exception as e:
                logger.error(f"Failed to send login notification: {e}")

        thread = threading.Thread(target=send_login_notification)
        thread.daemon = True
        thread.start()

    return user


@router.post("/complete-onboarding", response_model=Token)
async def complete_onboarding(
    request: OnboardingRequest,
    db: Session = Depends(get_db_session),
) -> Dict[str, str]:
    """
    Completes the onboarding for a new user created via Stripe checkout.
    Sets the password and updates the username.
    """
    session = db
    # Find user using CRUD layer
    user = crud_user.get_by_email(session, email=request.email)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    if user.hashed_password != "NEEDS_RESET":
        raise HTTPException(status_code=400, detail="Onboarding already completed.")

    # Check if the new username is taken by someone else using CRUD layer
    if user.username != request.username:
        existing_user = crud_user.get_by_username(session, username=request.username)
        if existing_user:
            raise HTTPException(status_code=400, detail="Username is already taken.")
        user.username = request.username

    user.hashed_password = get_password_hash(request.password)
    session.commit()

    # Create access and refresh tokens for auto-login
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": str(user.id), "scopes": []},
        expires_delta=access_token_expires,
    )
    refresh_token_expires = timedelta(days=7)
    refresh_token = create_access_token(
        data={"sub": str(user.id), "scopes": [], "refresh": True},
        expires_delta=refresh_token_expires,
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }
