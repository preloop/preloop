"""WebAuthn passkey endpoints: registration and authentication ceremonies.

Mounted under /api/v1/auth/webauthn/. Feature-flagged via PASSKEYS_ENABLED
(default true).

Scope (deliberately tight):
- Single-device passkeys with discoverable (resident) credentials.
- Registration from user settings (authenticated).
- "Sign in with passkey" on the login page (unauthenticated ceremony).
- No admin management UI.

Challenge state is carried in a short-lived JWT signed with the server
secret rather than server-side session storage, so the ceremony survives
multi-replica deployments without shared state.
"""

import base64
import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import List, Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from jwt import PyJWTError
from pydantic import BaseModel
from sqlalchemy.orm import Session
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from preloop.api.auth.identity import login_membership, require_active_identity
from preloop.api.auth.email_verification import enforce_verified_email
from preloop.api.auth.jwt import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    SECRET_KEY,
    create_access_token,
    create_refresh_token,
    get_current_active_user,
    user_auth_generation,
    get_current_user,
)
from preloop.services.authentication_policy import require_recent_strong_auth

from preloop.models.crud import (
    crud_audit_log,
    crud_person,
    crud_user,
    crud_webauthn_credential,
)
from preloop.models.db.session import get_db_session
from preloop.models.models.user import User
from preloop.services.authentication_policy import is_passkey_only
from preloop.services.rate_limit import AUTH_PASSKEY_CHALLENGE, enforce
from preloop.utils.request import get_client_ip

logger = logging.getLogger(__name__)

router = APIRouter()

CHALLENGE_TTL_SECONDS = 300


def passkeys_enabled() -> bool:
    """Whether passkey support is enabled (PASSKEYS_ENABLED, default true)."""
    return os.getenv("PASSKEYS_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def user_verification_required() -> bool:
    """Whether passkey ceremonies require user verification.

    ``PASSKEYS_REQUIRE_USER_VERIFICATION`` (default true) is a rollback
    switch: false restores ``preferred`` for security keys without a PIN,
    and while it is false passkey-only sign-in cannot be enabled.
    """
    return os.getenv(
        "PASSKEYS_REQUIRE_USER_VERIFICATION", "true"
    ).strip().lower() not in ("0", "false", "no", "off")


def _user_verification() -> UserVerificationRequirement:
    if user_verification_required():
        return UserVerificationRequirement.REQUIRED
    return UserVerificationRequirement.PREFERRED


_UV_REQUIRED_CODE = "passkey_user_verification_required"
_UV_REQUIRED_MESSAGE = (
    "This security key did not verify you. Set a PIN or biometric on it, "
    "or register a different passkey."
)


def _uv_required_for(db: Session, person_id: uuid.UUID) -> bool:
    """Whether this person's ceremonies must carry the UV flag.

    The rollback switch never weakens a person who opted into passkey-only
    sign-in: they enabled it under the UV requirement, and with their
    password refused, a passkey without UV would be their only factor.
    """
    if user_verification_required():
        return True
    return any(
        is_passkey_only(membership)
        for membership in crud_person.list_memberships(db, person_id=person_id)
    )


def _raise_if_user_not_verified(error: Exception) -> None:
    """Map the library's missing-UV-flag rejection to a stable 400 code."""
    if "user verification is required" in str(error).lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": _UV_REQUIRED_CODE, "message": _UV_REQUIRED_MESSAGE},
        )


def _require_enabled() -> None:
    if not passkeys_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Passkey support is disabled",
        )


# ---------------------------------------------------------------------------
# Rate limiting for challenge generation
# ---------------------------------------------------------------------------
# The challenge endpoints sign a JWT per call, and /authenticate/options is
# unauthenticated, so an anonymous caller could burn CPU on JWT signing and
# option generation. Throttled per client address on the shared limiter
# (rule ``auth.passkey_challenge``), so every API replica draws from one
# budget. ``WEBAUTHN_CHALLENGE_RATE_LIMIT`` keeps setting the per-minute
# limit; ``RATE_LIMIT_AUTH_PASSKEY_CHALLENGE`` overrides it like any rule.

_RATE_LIMIT_MAX_CALLS = int(os.getenv("WEBAUTHN_CHALLENGE_RATE_LIMIT", "30"))
_RATE_LIMIT_WINDOW_SECONDS = 60
# Counters live in the shared limiter now. Kept empty so code that cleared
# the old in-process map keeps working; nothing reads it.
_rate_buckets: dict[str, list[float]] = {}
_CHALLENGE_RATE_LIMITED_MESSAGE = (
    "Too many passkey challenge requests; try again shortly"
)


def _check_challenge_rate_limit(request: Request) -> None:
    """Charge one challenge to the caller's address; 429 when over budget.

    Args:
        request: Incoming request; keyed by :func:`get_client_ip` as before.

    Raises:
        RateLimitExceeded: 429 ``rate_limited`` with ``Retry-After``.
    """
    enforce(
        AUTH_PASSKEY_CHALLENGE,
        get_client_ip(request),
        default=(_RATE_LIMIT_MAX_CALLS, _RATE_LIMIT_WINDOW_SECONDS),
        detail=_CHALLENGE_RATE_LIMITED_MESSAGE,
    )


def _rp_id(request: Request) -> str:
    """Relying party ID: WEBAUTHN_RP_ID env override or the request host."""
    configured = os.getenv("WEBAUTHN_RP_ID", "").strip()
    if configured:
        return configured
    host = request.url.hostname or "localhost"
    return host


def _expected_origin(request: Request) -> str:
    """Expected client origin: WEBAUTHN_ORIGIN env override or the Origin header."""
    configured = os.getenv("WEBAUTHN_ORIGIN", "").strip()
    if configured:
        return configured
    origin = request.headers.get("origin")
    if origin:
        return origin
    scheme = request.url.scheme or "https"
    host = request.url.hostname or "localhost"
    port = request.url.port
    if port and port not in (80, 443):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def _issue_challenge_token(
    challenge: bytes,
    purpose: str,
    user_id: str = "",
    *,
    auth_generation: int = 0,
    account_id: uuid.UUID | None = None,
    step_up: bool = False,
    non_sso_generation: int = 0,
) -> str:
    """Sign the ceremony challenge into a short-lived stateless token."""
    return jwt.encode(
        {
            "chal": base64.urlsafe_b64encode(challenge).decode().rstrip("="),
            "jti": str(uuid.uuid4()),
            "iat": int(datetime.now(UTC).timestamp()),
            "purpose": purpose,
            "step_up": step_up,
            "nsg": non_sso_generation,
            "user_id": user_id,
            "gen": auth_generation,
            "account_id": str(account_id) if account_id else None,
            "exp": datetime.now(UTC) + timedelta(seconds=CHALLENGE_TTL_SECONDS),
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def _read_challenge_token(token: str, purpose: str) -> dict:
    """Validate a challenge token and return its payload."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired passkey challenge",
        )
    if payload.get("purpose") != purpose:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Challenge token purpose mismatch",
        )
    padded = payload["chal"] + "=" * (-len(payload["chal"]) % 4)
    payload["challenge_bytes"] = base64.urlsafe_b64decode(padded)
    return payload


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RegistrationOptionsResponse(BaseModel):
    """Options for navigator.credentials.create plus the challenge token."""

    options: dict
    challenge_token: str


class RegistrationVerifyRequest(BaseModel):
    """Authenticator response for the registration ceremony."""

    credential: dict
    challenge_token: str
    name: Optional[str] = None


class AuthenticationOptionsRequest(BaseModel):
    """Optional account destination and a session-bound reauthentication flag."""

    account_id: uuid.UUID | None = None
    step_up: bool = False


class AuthenticationOptionsResponse(BaseModel):
    """Options for navigator.credentials.get plus the challenge token."""

    options: dict
    challenge_token: str


class AuthenticationVerifyRequest(BaseModel):
    """Authenticator response for the authentication ceremony."""

    credential: dict
    challenge_token: str
    account_id: uuid.UUID | None = None
    step_up: bool = False


class PasskeySummary(BaseModel):
    """Non-sensitive passkey metadata for the settings UI."""

    id: uuid.UUID
    name: str
    created_at: datetime
    last_used_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Registration ceremony (authenticated)
# ---------------------------------------------------------------------------


@router.post(
    "/register/options",
    response_model=RegistrationOptionsResponse,
)
def registration_options(
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> RegistrationOptionsResponse:
    """Begin passkey registration for the signed-in user."""
    _require_enabled()
    _check_challenge_rate_limit(request)

    person = require_active_identity(db, current_user)
    existing = crud_webauthn_credential.list_for_person(db, person_id=person.id)
    exclude: List[PublicKeyCredentialDescriptor] = [
        PublicKeyCredentialDescriptor(id=base64url_to_bytes(cred.credential_id))
        for cred in existing
    ]

    options = generate_registration_options(
        rp_id=_rp_id(request),
        rp_name="Preloop",
        user_id=str(person.id).encode("utf-8"),
        user_name=person.username,
        user_display_name=person.full_name or person.username,
        exclude_credentials=exclude,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=(
                UserVerificationRequirement.REQUIRED
                if _uv_required_for(db, person.id)
                else UserVerificationRequirement.PREFERRED
            ),
        ),
    )

    return RegistrationOptionsResponse(
        options=json.loads(options_to_json(options)),
        challenge_token=_issue_challenge_token(
            options.challenge,
            "register",
            str(person.id),
            auth_generation=person.auth_generation,
        ),
    )


@router.post("/register/verify", response_model=PasskeySummary)
def registration_verify(
    body: RegistrationVerifyRequest,
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> PasskeySummary:
    """Complete passkey registration and store the credential."""
    _require_enabled()

    person = require_active_identity(db, current_user, lock=True)
    payload = _read_challenge_token(body.challenge_token, "register")
    if (
        payload.get("user_id") != str(person.id)
        or payload.get("gen") != person.auth_generation
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Challenge was issued for a different user",
        )

    try:
        verification = verify_registration_response(
            credential=body.credential,
            expected_challenge=payload["challenge_bytes"],
            expected_rp_id=_rp_id(request),
            expected_origin=_expected_origin(request),
            require_user_verification=_uv_required_for(db, person.id),
        )
    except InvalidRegistrationResponse as e:
        logger.warning(f"Passkey registration verification failed: {e}")
        _raise_if_user_not_verified(e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Passkey registration could not be verified",
        )

    credential_id_b64 = (
        base64.urlsafe_b64encode(verification.credential_id).decode().rstrip("=")
    )
    if crud_webauthn_credential.get_by_credential_id(
        db, credential_id=credential_id_b64
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This passkey is already registered",
        )

    transports = body.credential.get("response", {}).get("transports")

    cred = crud_webauthn_credential.create(
        db,
        obj_in={
            "person_id": person.id,
            "user_id": current_user.id,
            "credential_id": credential_id_b64,
            "public_key": base64.urlsafe_b64encode(verification.credential_public_key)
            .decode()
            .rstrip("="),
            "sign_count": verification.sign_count,
            "transports": json.dumps(transports) if transports else None,
            "name": (body.name or "Passkey")[:100],
            "aaguid": str(verification.aaguid) if verification.aaguid else None,
            "is_active": True,
        },
    )

    return PasskeySummary(
        id=cred.id,
        name=cred.name,
        created_at=cred.created_at,
        last_used_at=None,
    )


# ---------------------------------------------------------------------------
# Authentication ceremony (unauthenticated)
# ---------------------------------------------------------------------------


@router.post(
    "/authenticate/options",
    response_model=AuthenticationOptionsResponse,
)
def authentication_options(
    request: Request,
    body: AuthenticationOptionsRequest | None = None,
    db: Session = Depends(get_db_session),
) -> AuthenticationOptionsResponse:
    """Begin passkey sign-in. Discoverable credentials: no username needed."""
    _require_enabled()
    _check_challenge_rate_limit(request)

    stepping_up = body is not None and body.step_up
    current = None
    if stepping_up:
        from preloop.api.auth.mfa_router import person_session

        token = request.headers.get("authorization", "").partition(" ")[2]
        current = get_current_user(token=token, db=db, request=request)
        person_session(request, current)
        if body.account_id != current.account_id:
            raise HTTPException(401, "Passkey must target the current account")
    options = generate_authentication_options(
        rp_id=_rp_id(request),
        user_verification=UserVerificationRequirement.REQUIRED
        if stepping_up
        else _user_verification(),
    )

    return AuthenticationOptionsResponse(
        options=json.loads(options_to_json(options)),
        challenge_token=_issue_challenge_token(
            options.challenge,
            "authenticate",
            user_id=str(current.id) if current is not None else "",
            auth_generation=user_auth_generation(current) if current is not None else 0,
            non_sso_generation=current.non_sso_auth_generation
            if current is not None
            else 0,
            step_up=stepping_up,
            account_id=body.account_id if body else None,
        ),
    )


@router.post("/authenticate/verify")
def authentication_verify(
    body: AuthenticationVerifyRequest,
    request: Request,
    db: Session = Depends(get_db_session),
) -> dict:
    """Audit one final passkey outcome, including challenge and policy failures."""
    from preloop.services.auth_audit import (
        login_failure_reason,
        login_subject,
        record_login_event,
    )

    succeeded = False
    reason_code = "internal_error"
    try:
        result = _authentication_verify(body, request, db)
        succeeded = True
        return result
    except Exception as exc:
        reason_code = login_failure_reason(exc, method="passkey")
        raise
    finally:
        try:
            user = getattr(request.state, "auth_audit_user", None)
            if user is None:
                raw_id = body.credential.get("rawId") or body.credential.get("id") or ""
                cred = crud_webauthn_credential.get_by_credential_id(
                    db, credential_id=raw_id.rstrip("=")
                )
                if cred is not None:
                    user = login_subject(db, crud_person.get(db, id=cred.person_id))
            record_login_event(
                db,
                user=user,
                method="passkey",
                succeeded=succeeded,
                reason_code=None if succeeded else reason_code,
                source_ip=get_client_ip(request),
            )
        except Exception:
            logger.warning("Authentication audit submission failed")


def _authentication_verify(
    body: AuthenticationVerifyRequest,
    request: Request,
    db: Session = Depends(get_db_session),
) -> dict:
    """Complete passkey sign-in; returns access and refresh tokens."""
    _require_enabled()

    payload = _read_challenge_token(body.challenge_token, "authenticate")
    if payload.get("step_up", False) is not body.step_up:
        raise HTTPException(401, "Passkey challenge purpose changed")
    if payload.get("account_id") != (str(body.account_id) if body.account_id else None):
        raise HTTPException(status_code=400, detail="Passkey account selection changed")

    raw_id = body.credential.get("rawId") or body.credential.get("id") or ""
    credential_id_b64 = raw_id.rstrip("=")
    cred = crud_webauthn_credential.get_by_credential_id(
        db, credential_id=credential_id_b64
    )
    if cred is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown passkey",
        )

    public_key_padded = cred.public_key + "=" * (-len(cred.public_key) % 4)
    try:
        verification = verify_authentication_response(
            credential=body.credential,
            expected_challenge=payload["challenge_bytes"],
            expected_rp_id=_rp_id(request),
            expected_origin=_expected_origin(request),
            credential_public_key=base64.urlsafe_b64decode(public_key_padded),
            credential_current_sign_count=cred.sign_count,
            require_user_verification=body.step_up
            or _uv_required_for(db, cred.person_id),
        )
    except InvalidAuthenticationResponse as e:
        logger.warning(f"Passkey authentication verification failed: {e}")
        _raise_if_user_not_verified(e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Passkey authentication failed",
        )

    person = crud_person.get(db, id=cred.person_id)
    user = login_membership(db, person) if person is not None else None
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    from preloop.services.authentication_policy import select_authenticated_membership

    user = select_authenticated_membership(db, user, account_id=body.account_id)
    if body.step_up:
        from preloop.api.auth.mfa_router import person_session

        token = request.headers.get("authorization", "").partition(" ")[2]
        current = get_current_user(token=token, db=db, request=request)
        person_session(request, current)
        if current.id != user.id:
            raise HTTPException(401, "Passkey must belong to the current session")
        from preloop.services import mfa, rate_limit

        user = mfa.locked_user(db, current)
        from preloop.api.auth.jwt import decode_token, reject_revoked_token

        reject_revoked_token(db, user, decode_token(token))
        if (
            payload.get("user_id") != str(user.id)
            or payload.get("gen") != user_auth_generation(user)
            or payload.get("nsg") != user.non_sso_auth_generation
        ):
            raise HTTPException(401, "Passkey challenge expired")
        cred = crud_webauthn_credential.lock_active(db, credential_id=credential_id_b64)
        if cred is None:
            raise HTTPException(401, "Passkey no longer available")
        # Consume the signed assertion challenge under the same membership lock
        # as factor management, including authenticators with a zero counter.
        jti = payload.get("jti")
        issued = payload.get("iat")
        if not isinstance(jti, str) or type(issued) is not int:
            raise HTTPException(401, "Start a new passkey assertion")
        if rate_limit.transactional_count(
            db,
            rate_limit.AUTH_MFA_CONSUMED,
            "passkey:" + jti,
            now=mfa.clock(),
            started_at=issued,
        ):
            raise HTTPException(401, "Passkey challenge already used")
        rate_limit.transactional_count(
            db,
            rate_limit.AUTH_MFA_CONSUMED,
            "passkey:" + jti,
            now=mfa.clock(),
            started_at=issued,
            increment=True,
        )
    request.state.auth_audit_user = user

    # A passkey is a second way into the same local account, so it obeys the
    # same verification requirement the password endpoints do. Without this,
    # registering a passkey would be a way around REQUIRE_EMAIL_VERIFICATION.
    require_active_identity(db, user)
    enforce_verified_email(user, db)
    from preloop.services.authentication_policy import new_authentication

    auth_context = new_authentication(
        db, user, method="passkey", source_ip=get_client_ip(request)
    )
    auth_context["passkey_uv"] = verification.user_verified is True

    crud_webauthn_credential.touch(db, obj=cred, sign_count=verification.new_sign_count)

    # Audit and notification parity with password login (authenticate_user in
    # auth/router.py): update last_login, write a user.login audit event, and
    # notify admins on login after prolonged inactivity.
    import threading

    from preloop.services.account_setup_service import (
        notify_admins_user_login_after_inactivity,
        should_notify_on_login,
    )

    source_ip = get_client_ip(request)
    old_last_login = user.last_login
    user.last_login = datetime.now(UTC)
    db.commit()
    db.refresh(user)

    try:
        crud_audit_log.log_action(
            db,
            account_id=user.account_id,
            user_id=user.id,
            action="user.login",
            resource_type="user",
            resource_id=str(user.id),
            status="success",
            ip_address=source_ip,
            details={"method": "passkey", "credential_id": str(cred.id)},
        )
    except Exception:
        logger.debug("Failed to audit passkey login", exc_info=True)

    if (
        should_notify_on_login(old_last_login, days_threshold=7)
        and source_ip != "testclient"
    ):
        username_str = user.username
        email_str = user.email

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

    generation = user_auth_generation(user)
    access_token = create_access_token(
        data={"sub": str(user.id), "scopes": []},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        auth_generation=generation,
        auth_context=auth_context,
    )
    refresh_token = create_refresh_token(
        sub=str(user.id),
        scopes=[],
        auth_generation=generation,
        auth_context=auth_context,
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }


# ---------------------------------------------------------------------------
# Credential management (authenticated, settings UI)
# ---------------------------------------------------------------------------


@router.get("/credentials", response_model=List[PasskeySummary])
def list_credentials(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> List[PasskeySummary]:
    """List the signed-in user's passkeys."""
    _require_enabled()
    person = require_active_identity(db, current_user)
    creds = crud_webauthn_credential.list_for_person(db, person_id=person.id)
    return [
        PasskeySummary(
            id=c.id,
            name=c.name,
            created_at=c.created_at,
            last_used_at=c.last_used_at,
        )
        for c in creds
    ]


@router.delete(
    "/credentials/{credential_id}",
    status_code=204,
    dependencies=[Depends(require_recent_strong_auth(600))],
)
def delete_credential(
    credential_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> None:
    """Remove one of the signed-in user's passkeys.

    The person row and this membership row are locked first, in the same
    order as enabling passkey-only, so a concurrent enable and a delete of
    the last passkey cannot both succeed.

    Raises:
        HTTPException: 404 when the passkey is not the caller's; 409
            ``last_passkey_required`` when a membership of this person
            requires a passkey and this is the last one.
    """
    _require_enabled()
    person = require_active_identity(db, current_user, lock=True)
    crud_user.lock(db, user_id=current_user.id)
    cred = crud_webauthn_credential.get(db, id=credential_id)
    if cred is None or cred.person_id != person.id or not cred.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Passkey not found",
        )
    remaining = crud_webauthn_credential.list_for_person(db, person_id=person.id)
    requires_passkey = any(
        is_passkey_only(membership)
        for membership in crud_person.list_memberships(db, person_id=person.id)
    )
    if requires_passkey and len(remaining) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "last_passkey_required",
                "message": (
                    "This account requires a passkey to sign in. Register "
                    "another passkey or turn off passkey-only sign-in first."
                ),
            },
        )
    from preloop.plugins.account_hooks import get_factor_removal_policy

    policy = get_factor_removal_policy()
    if policy is not None:
        policy(db, current_user, "passkey")
    crud_webauthn_credential.deactivate(db, obj=cred)


# ---------------------------------------------------------------------------
# Passkey-only sign-in (authenticated, settings UI)
# ---------------------------------------------------------------------------


class PasskeyOnlyStatus(BaseModel):
    """Whether passkey-only sign-in is on, and how many passkeys back it."""

    enabled: bool
    passkey_count: int


class PasskeyOnlyUpdate(BaseModel):
    """Requested passkey-only state."""

    enabled: bool


class PasskeyOnlyUpdateResponse(BaseModel):
    """New state; on enable, a fresh token pair for the current session.

    Enabling ends every other non-SSO session of the user, including the
    tokens this request was made with, so the console replaces them with the
    pair returned here (same fields as ``POST /auth/refresh``).
    """

    enabled: bool
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_type: Optional[str] = None
    expires_in: Optional[int] = None


def _detail(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


@router.get("/passkey-only", response_model=PasskeyOnlyStatus)
def get_passkey_only(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> PasskeyOnlyStatus:
    """Report whether the signed-in user requires a passkey to sign in."""
    _require_enabled()
    person = require_active_identity(db, current_user)
    count = len(crud_webauthn_credential.list_for_person(db, person_id=person.id))
    return PasskeyOnlyStatus(enabled=is_passkey_only(current_user), passkey_count=count)


@router.put("/passkey-only", response_model=PasskeyOnlyUpdateResponse)
def put_passkey_only(
    body: PasskeyOnlyUpdate,
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> PasskeyOnlyUpdateResponse:
    """Turn passkey-only sign-in on or off for the signed-in user.

    Either direction requires strong authentication at most 10 minutes old. Enabling ends every other non-SSO session of the
    user and returns a fresh token pair for this one.

    Raises:
        HTTPException: 401 ``step_up_required``; 409
            ``user_verification_disabled`` when enabling while
            ``PASSKEYS_REQUIRE_USER_VERIFICATION`` is false; 409
            ``no_passkey_registered`` when enabling without a passkey.
    """
    from preloop.services.auth_audit import record_auth_event
    from preloop.services.audit.service import AuditActionType

    _require_enabled()
    if body.enabled and not user_verification_required():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_detail(
                "user_verification_disabled",
                "Passkey-only sign-in is unavailable while this server does "
                "not require passkey user verification.",
            ),
        )

    claims = require_recent_strong_auth(600)(request, current_user, db)
    person = require_active_identity(db, current_user, lock=True)
    user = crud_user.lock(db, user_id=current_user.id)
    if user is None:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    count = len(crud_webauthn_credential.list_for_person(db, person_id=person.id))
    if body.enabled and count == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_detail(
                "no_passkey_registered",
                "Register a passkey before requiring one to sign in.",
            ),
        )
    changed = is_passkey_only(user) != body.enabled
    generation = crud_user.set_passkey_only(
        db,
        user=user,
        enabled=body.enabled,
        end_sessions=body.enabled and changed,
        commit=False,
    )
    if changed:
        record_auth_event(
            db,
            action=AuditActionType.PASSKEY_ONLY_ENABLED
            if body.enabled
            else AuditActionType.PASSKEY_ONLY_DISABLED,
            user=user,
            resource_type="user",
            resource_id=user.id,
            details={"enabled": body.enabled, "passkey_count": count},
            source_ip=get_client_ip(request),
            commit=False,
        )
    db.commit()
    db.refresh(user)

    if not (body.enabled and changed):
        return PasskeyOnlyUpdateResponse(enabled=body.enabled)

    # This session's tokens carry the old non-SSO generation; reissue them
    # with the same assurance (passkey, original auth_time) and the new one.
    auth_context = dict(claims)
    auth_context["nsg"] = generation
    auth_generation = user_auth_generation(user)
    access_token = create_access_token(
        data={"sub": str(user.id), "scopes": []},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        auth_generation=auth_generation,
        auth_context=auth_context,
    )
    refresh_token = create_refresh_token(
        sub=str(user.id),
        scopes=[],
        session_started_at=datetime.fromtimestamp(claims["auth_time"], UTC),
        auth_generation=auth_generation,
        auth_context=auth_context,
    )
    return PasskeyOnlyUpdateResponse(
        enabled=True,
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
