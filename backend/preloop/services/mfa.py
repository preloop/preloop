"""TOTP proofs, bounded challenges and session upgrades.

Every proof is checked under the membership and factor row locks. Proof use,
challenge consumption, failure counters and security audit share a transaction.
No exception text or caller-supplied proof enters the audit trail.
"""

import hashlib
import hmac
import os
import re
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import jwt
import pyotp
from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_user, mfa as crud_mfa
from preloop.services import rate_limit
from preloop.services.auth_audit import record_auth_event
from preloop.utils.encryption import decrypt_value, encrypt_value

WEAK_METHODS = frozenset({"password", "email_link", "chat", "social"})


def clock() -> float:
    """Current time; integration tests control proof steps through this seam."""
    return time.time()


def enabled() -> bool:
    """Emergency rollback setting; deliberately downgrades enrolled sign-ins."""
    return os.getenv("MFA_TOTP_ENABLED", "true").lower() not in {
        "false",
        "0",
        "no",
        "off",
    }


def error(code: str, status: int = 401) -> HTTPException:
    """Return a bounded, stable authentication error."""
    messages = {
        "invalid_code": "The code is invalid or has already been used.",
        "challenge_expired": "Sign in again to get a new challenge.",
        "challenge_locked": "Too many incorrect codes. Sign in again later.",
        "mfa_already_enabled": "An authenticator app is already enabled.",
        "no_pending_enrollment": "Start authenticator enrollment first.",
        "mfa_not_enabled": "Enroll an authenticator app first.",
        "mfa_unavailable": "Authenticator enrollment is disabled on this server.",
    }
    return HTTPException(
        status,
        detail={"code": code, "message": messages.get(code, "Authentication failed.")},
    )


def totp(secret: str) -> pyotp.TOTP:
    """The interoperable SHA-1, six-digit, 30-second authenticator profile."""
    return pyotp.TOTP(secret, digits=6, interval=30, digest=hashlib.sha1)


def match_step(
    secret: str, code: str, *, now: float, last_used_step: int | None
) -> int | None:
    """Match all three candidate steps in constant time, rejecting replays."""
    if re.fullmatch(r"[0-9]{6}", code) is None:
        return None
    current = int(now) // 30
    result = None
    for step in range(current - 1, current + 2):
        matches = hmac.compare_digest(totp(secret).at(step * 30), code)
        if matches and (last_used_step is None or step > last_used_step):
            result = step
    return result


def generate_recovery_codes() -> tuple[list[str], list[str]]:
    """Create ten 50-bit recovery codes and hash them with the password hasher."""
    from preloop.api.auth.jwt import get_password_hash

    alphabet = "abcdefghijklmnopqrstuvwxyz234567"
    codes: list[str] = []
    while len(codes) < 10:
        raw = "".join(secrets.choice(alphabet) for _ in range(10))
        code = raw[:5] + "-" + raw[5:]
        if code not in codes:
            codes.append(code)
    return codes, [get_password_hash(code) for code in codes]


def audit(db: Session, user: models.User, action: str) -> None:
    """Write an MFA event with identifiers only, in the caller's transaction."""
    record_auth_event(
        db,
        user=user,
        action="auth.mfa." + action,
        resource_type="user",
        resource_id=user.id,
        commit=False,
    )


def require_challenge(db: Session, user: models.User, context: dict[str, Any]) -> None:
    """Interrupt a weak proven sign-in before any session or grant is issued."""
    methods = context.get("amr", [])
    if not enabled() or not methods or not set(methods) <= WEAK_METHODS:
        return
    obj = crud_mfa.factor(db, user.id)
    if obj is None or obj.confirmed_at is None:
        return
    from preloop.api.auth.jwt import ALGORITHM, SECRET_KEY, user_auth_generation
    from preloop.services.authentication_policy import non_sso_generation

    now = int(time.time())
    payload = {
        **context,
        "sub": str(user.id),
        "typ": "mfa_challenge",
        "type": "mfa_challenge",
        "iat": now,
        "exp": now + 300,
        "jti": str(uuid.uuid4()),
        "factor_id": str(obj.id),
        "gen": user_auth_generation(user),
        "nsg": non_sso_generation(user),
    }
    challenge = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    raise HTTPException(
        401,
        detail={
            "code": "mfa_required",
            "challenge_token": challenge,
            "methods": ["totp", "recovery_code"],
        },
        headers={"Cache-Control": "no-store"},
    )


def read_challenge(token: str) -> dict[str, Any]:
    """Accept only signed, bounded MFA challenges, never access/refresh tokens."""
    from preloop.api.auth.jwt import ALGORITHM, SECRET_KEY
    from preloop.services.authentication_policy import authentication_claims

    try:
        payload = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            options={
                "require": ["sub", "exp", "iat", "jti", "gen"],
            },
        )
        if (
            payload.get("typ") != "mfa_challenge"
            or payload.get("type") != "mfa_challenge"
        ):
            raise ValueError("wrong type")
        for name in ("sub", "jti", "factor_id"):
            uuid.UUID(payload[name])
        if (
            type(payload["iat"]) is not int
            or type(payload["exp"]) is not int
            or payload["exp"] - payload["iat"] != 300
        ):
            raise ValueError("wrong lifetime")
        context = authentication_claims(payload)
        if not context.get("amr") or not set(context["amr"]) <= WEAK_METHODS:
            raise ValueError("wrong method")
        return payload
    except (jwt.PyJWTError, ValueError, KeyError, TypeError, HTTPException):
        raise error("challenge_expired") from None


def locked_user(db: Session, user: models.User) -> models.User:
    """Use the same person, membership, factor lock order as passkey management."""
    from preloop.api.auth.identity import require_active_identity

    require_active_identity(db, user, lock=True)
    locked = crud_user.lock(db, user_id=user.id)
    if locked is None:
        raise error("challenge_expired")
    return locked


def prove(
    db: Session,
    user: models.User,
    obj: models.UserMfaFactor,
    *,
    code: str | None,
    recovery_code: str | None = None,
    challenge: dict[str, Any] | None = None,
    invalid_status: int = 401,
) -> None:
    """Check and consume a proof under row locks; persist every failed attempt."""
    from preloop.api.auth.jwt import verify_password

    now = clock()
    identity = str(user.id)
    if challenge is not None:
        args = {"now": now, "started_at": challenge["iat"]}
        jti = challenge["jti"]
        if rate_limit.transactional_count(
            db, rate_limit.AUTH_MFA_CONSUMED, jti, **args
        ):
            raise error("challenge_expired")
        if (
            rate_limit.transactional_count(
                db, rate_limit.AUTH_MFA_CHALLENGE, jti, **args
            )
            >= 5
        ):
            raise error("challenge_locked", 429)
    if (
        rate_limit.transactional_count(db, rate_limit.AUTH_MFA_USER, identity, now=now)
        >= 10
    ):
        raise error("challenge_locked", 429)
    accepted = False
    if code is not None:
        step = match_step(
            decrypt_value(obj.secret_encrypted),
            code,
            now=now,
            last_used_step=obj.last_used_step,
        )
        if step is not None:
            crud_mfa.use_step(db, obj, step)
            accepted = True
    elif recovery_code is not None:
        matched = None
        for candidate in crud_mfa.recovery_codes(db, user.id):
            if verify_password(recovery_code, candidate.code_hash):
                matched = candidate
        if matched is not None:
            crud_mfa.use_recovery_code(
                db, matched, now=datetime.fromtimestamp(now, UTC)
            )
            audit(db, user, "recovery_code_used")
            accepted = True
    if not accepted:
        count = rate_limit.transactional_count(
            db, rate_limit.AUTH_MFA_USER, identity, now=now, increment=True
        )
        locked = count == 10
        if challenge is not None:
            count = rate_limit.transactional_count(
                db,
                rate_limit.AUTH_MFA_CHALLENGE,
                challenge["jti"],
                now=now,
                started_at=challenge["iat"],
                increment=True,
            )
            locked = locked or count == 5
        audit(db, user, "verify_failed")
        if locked:
            audit(db, user, "challenge_locked")
        db.commit()
        raise error("invalid_code", invalid_status)
    if challenge is not None:
        rate_limit.transactional_count(
            db,
            rate_limit.AUTH_MFA_CONSUMED,
            challenge["jti"],
            now=now,
            started_at=challenge["iat"],
            increment=True,
        )


def token_pair(user: models.User, context: dict[str, Any]) -> dict[str, Any]:
    """Issue the same token fields as refresh after the proof transaction commits."""
    from preloop.api.auth.jwt import (
        ACCESS_TOKEN_EXPIRE_MINUTES,
        create_access_token,
        create_refresh_token,
        user_auth_generation,
    )

    generation = user_auth_generation(user)
    return {
        "access_token": create_access_token(
            {"sub": str(user.id), "scopes": []},
            auth_generation=generation,
            auth_context=context,
        ),
        "refresh_token": create_refresh_token(
            str(user.id), auth_generation=generation, auth_context=context
        ),
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }


def upgrade_context(
    db: Session, user: models.User, claims: dict[str, Any]
) -> dict[str, Any]:
    """Add OTP assurance and run the edition policy again before committing."""
    from preloop.services.authentication_policy import (
        authentication_claims,
        authorize_authentication,
    )

    context = authentication_claims(claims)
    context["amr"] = list(dict.fromkeys([*context.get("amr", []), "otp"]))
    context.update(
        auth_time=int(clock()),
        nsg=user.non_sso_auth_generation,
        auth_nsg=user.non_sso_auth_generation,
        auth_account_id=str(user.account_id),
    )
    return authorize_authentication(db, user, claims=context, purpose="login")


def start_enrollment(db: Session, user: models.User) -> dict[str, str]:
    """Create a pending secret after the caller locks and rechecks the session."""
    if not enabled():
        raise error("mfa_unavailable", 404)
    secret = pyotp.random_base32()
    try:
        crud_mfa.enroll(db, user.id, encrypt_value(secret))
    except ValueError:
        raise error("mfa_already_enabled", 409) from None
    db.commit()
    return {
        "secret": secret,
        "otpauth_uri": totp(secret).provisioning_uri(
            name=user.email, issuer_name="Preloop"
        ),
    }
