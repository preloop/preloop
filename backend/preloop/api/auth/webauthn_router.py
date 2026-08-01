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

from fastapi import APIRouter, Depends, HTTPException, Request, status
from jose import JWTError, jwt
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

from preloop.api.auth.jwt import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    SECRET_KEY,
    create_access_token,
    create_refresh_token,
    get_current_active_user,
)
from preloop.models.crud import crud_audit_log, crud_user, crud_webauthn_credential
from preloop.models.db.session import get_db_session
from preloop.models.models.user import User
from preloop.models.models.webauthn_credential import WebAuthnCredential
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
# option generation. Simple in-process sliding-window per-IP throttle; the
# tokens are stateless so there is no storage to exhaust, only CPU to protect.
# Multi-replica deployments get N x the budget, which is acceptable for this
# purpose (protecting compute, not enforcing a strict global quota).

_RATE_LIMIT_MAX_CALLS = int(os.getenv("WEBAUTHN_CHALLENGE_RATE_LIMIT", "30"))
_RATE_LIMIT_WINDOW_SECONDS = 60
_rate_buckets: dict[str, list[float]] = {}


def _check_challenge_rate_limit(request: Request) -> None:
    import time

    ip = get_client_ip(request)
    now = time.monotonic()
    window_start = now - _RATE_LIMIT_WINDOW_SECONDS

    bucket = [t for t in _rate_buckets.get(ip, []) if t > window_start]
    if len(bucket) >= _RATE_LIMIT_MAX_CALLS:
        _rate_buckets[ip] = bucket
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many passkey challenge requests; try again shortly",
        )
    bucket.append(now)
    _rate_buckets[ip] = bucket

    # Opportunistic cleanup so the map cannot grow unbounded across many IPs.
    if len(_rate_buckets) > 10_000:
        for stale_ip in [
            k for k, v in _rate_buckets.items() if not v or v[-1] <= window_start
        ]:
            _rate_buckets.pop(stale_ip, None)


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


def _issue_challenge_token(challenge: bytes, purpose: str, user_id: str = "") -> str:
    """Sign the ceremony challenge into a short-lived stateless token."""
    return jwt.encode(
        {
            "chal": base64.urlsafe_b64encode(challenge).decode().rstrip("="),
            "purpose": purpose,
            "user_id": user_id,
            "exp": datetime.now(UTC) + timedelta(seconds=CHALLENGE_TTL_SECONDS),
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def _read_challenge_token(token: str, purpose: str) -> dict:
    """Validate a challenge token and return its payload."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
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


class AuthenticationOptionsResponse(BaseModel):
    """Options for navigator.credentials.get plus the challenge token."""

    options: dict
    challenge_token: str


class AuthenticationVerifyRequest(BaseModel):
    """Authenticator response for the authentication ceremony."""

    credential: dict
    challenge_token: str


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

    existing = crud_webauthn_credential.list_for_user(db, user_id=current_user.id)
    exclude: List[PublicKeyCredentialDescriptor] = [
        PublicKeyCredentialDescriptor(id=base64url_to_bytes(cred.credential_id))
        for cred in existing
    ]

    options = generate_registration_options(
        rp_id=_rp_id(request),
        rp_name="Preloop",
        user_id=str(current_user.id).encode("utf-8"),
        user_name=current_user.username,
        user_display_name=current_user.full_name or current_user.username,
        exclude_credentials=exclude,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )

    return RegistrationOptionsResponse(
        options=json.loads(options_to_json(options)),
        challenge_token=_issue_challenge_token(
            options.challenge, "register", str(current_user.id)
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

    payload = _read_challenge_token(body.challenge_token, "register")
    if payload.get("user_id") != str(current_user.id):
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
        )
    except InvalidRegistrationResponse as e:
        logger.warning(f"Passkey registration verification failed: {e}")
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

    cred = WebAuthnCredential(
        user_id=current_user.id,
        credential_id=credential_id_b64,
        public_key=base64.urlsafe_b64encode(verification.credential_public_key)
        .decode()
        .rstrip("="),
        sign_count=verification.sign_count,
        transports=json.dumps(transports) if transports else None,
        name=(body.name or "Passkey")[:100],
        aaguid=str(verification.aaguid) if verification.aaguid else None,
        is_active=True,
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)

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
def authentication_options(request: Request) -> AuthenticationOptionsResponse:
    """Begin passkey sign-in. Discoverable credentials: no username needed."""
    _require_enabled()
    _check_challenge_rate_limit(request)

    options = generate_authentication_options(
        rp_id=_rp_id(request),
        user_verification=UserVerificationRequirement.PREFERRED,
    )

    return AuthenticationOptionsResponse(
        options=json.loads(options_to_json(options)),
        challenge_token=_issue_challenge_token(options.challenge, "authenticate"),
    )


@router.post("/authenticate/verify")
def authentication_verify(
    body: AuthenticationVerifyRequest,
    request: Request,
    db: Session = Depends(get_db_session),
) -> dict:
    """Complete passkey sign-in; returns access and refresh tokens."""
    _require_enabled()

    payload = _read_challenge_token(body.challenge_token, "authenticate")

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
        )
    except InvalidAuthenticationResponse as e:
        logger.warning(f"Passkey authentication verification failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Passkey authentication failed",
        )

    user = crud_user.get(db, id=cred.user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

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

    access_token = create_access_token(
        data={"sub": str(user.id), "scopes": []},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh_token = create_refresh_token(sub=str(user.id), scopes=[])

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
    creds = crud_webauthn_credential.list_for_user(db, user_id=current_user.id)
    return [
        PasskeySummary(
            id=c.id,
            name=c.name,
            created_at=c.created_at,
            last_used_at=c.last_used_at,
        )
        for c in creds
    ]


@router.delete("/credentials/{credential_id}", status_code=204)
def delete_credential(
    credential_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> None:
    """Remove one of the signed-in user's passkeys."""
    _require_enabled()
    cred = crud_webauthn_credential.get(db, id=credential_id)
    if cred is None or cred.user_id != current_user.id or not cred.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Passkey not found",
        )
    crud_webauthn_credential.deactivate(db, obj=cred)
