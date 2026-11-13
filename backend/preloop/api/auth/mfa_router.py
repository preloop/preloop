"""Authenticator enrollment, two-step login, recovery and fresh authentication."""

import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import (
    decode_token,
    get_current_active_user,
    reject_revoked_token,
)
from preloop.models import models
from preloop.models.crud import crud_user, mfa as crud_mfa
from preloop.models.db.session import get_db_session
from preloop.services import mfa
from preloop.services.authentication_policy import request_authentication_context


class MfaRoute(APIRoute):
    """Keep submitted proof material out of validation error responses."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap validation without retaining its input-bearing error details."""
        handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(
                    422,
                    detail={
                        "code": "invalid_request",
                        "message": "Supply the requested authentication proof.",
                    },
                    headers={"Cache-Control": "no-store"},
                ) from None

        return safe_handler


router = APIRouter(route_class=MfaRoute)


class MfaStatus(BaseModel):
    """Non-sensitive factor state."""

    totp_enabled: bool
    recovery_codes_remaining: int


class Enrollment(BaseModel):
    """The only response that exposes the pending authenticator secret."""

    secret: str
    otpauth_uri: str


class CodeRequest(BaseModel):
    """A bounded authenticator proof."""

    code: str = Field(min_length=1, max_length=32)


class ProofRequest(BaseModel):
    """Exactly one authenticator or recovery proof."""

    code: str | None = Field(default=None, min_length=1, max_length=32)
    recovery_code: str | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def one_proof(self) -> "ProofRequest":
        """Reject ambiguous requests before doing cryptographic work."""
        if (self.code is None) == (self.recovery_code is None):
            raise ValueError("Supply exactly one code or recovery_code")
        return self


class VerifyRequest(ProofRequest):
    """Exchange a short-lived sign-in challenge and one proof."""

    challenge_token: str = Field(min_length=1, max_length=4096)


class TokenPair(BaseModel):
    """Session token fields shared with the refresh endpoint."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RecoveryCodes(BaseModel):
    """New recovery codes, shown once and never returned by status."""

    recovery_codes: list[str]


class ConfirmedEnrollment(TokenPair, RecoveryCodes):
    """Proof of enrollment replaces the now-revoked current token pair."""


def person_session(
    request: Request, user: models.User = Depends(get_current_active_user)
) -> models.User:
    """Reject API keys, runner/delegation tokens and CLI-bound token upgrades."""
    request_authentication_context(request, user)
    bearer = request.headers.get("authorization", "").partition(" ")[2]
    token = decode_token(bearer)
    if token.sid is not None or token.scopes:
        raise HTTPException(401, "Console session required")
    return user


def _lock_session(
    db: Session, user: models.User, request: Request
) -> tuple[models.User, dict[str, Any]]:
    """Recheck session revocation after obtaining mutation locks."""
    user = mfa.locked_user(db, user)
    token = decode_token(request.headers.get("authorization", "").partition(" ")[2])
    reject_revoked_token(db, user, token)
    return user, request_authentication_context(request, user)


def _confirmed(db: Session, user: models.User) -> models.UserMfaFactor:
    """Lock a usable factor or fail closed."""
    obj = crud_mfa.factor(db, user.id, lock=True)
    if obj is None or obj.confirmed_at is None:
        raise mfa.error("mfa_not_enabled", 409)
    return obj


@router.get("/status", response_model=MfaStatus)
def status(
    user: models.User = Depends(person_session), db: Session = Depends(get_db_session)
) -> MfaStatus:
    """Read state without disclosing secrets or recovery hashes."""
    return MfaStatus(
        totp_enabled=crud_mfa.enabled(db, user.id),
        recovery_codes_remaining=crud_mfa.remaining(db, user.id),
    )


@router.post("/enroll", response_model=Enrollment)
def enroll(
    request: Request,
    response: Response,
    user: models.User = Depends(person_session),
    db: Session = Depends(get_db_session),
) -> dict[str, str]:
    """Start or replace a pending enrollment."""
    user, _ = _lock_session(db, user, request)
    response.headers["Cache-Control"] = "no-store"
    return mfa.start_enrollment(db, user)


@router.post("/enroll/confirm", response_model=ConfirmedEnrollment)
def confirm(
    body: CodeRequest,
    request: Request,
    response: Response,
    user: models.User = Depends(person_session),
    db: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Confirm possession, issue recovery codes and end other non-SSO sessions."""
    if not mfa.enabled():
        raise mfa.error("mfa_unavailable", 404)
    user, context = _lock_session(db, user, request)
    obj = crud_mfa.factor(db, user.id, lock=True)
    if obj is None or obj.confirmed_at is not None:
        raise mfa.error("no_pending_enrollment", 409)
    mfa.prove(db, user, obj, code=body.code, invalid_status=400)
    crud_mfa.confirm(db, obj, now=datetime.fromtimestamp(mfa.clock(), UTC))
    codes, hashes = mfa.generate_recovery_codes()
    crud_mfa.replace_recovery_codes(db, user.id, hashes)
    crud_mfa.end_non_sso_sessions(db, user)
    context = mfa.upgrade_context(db, user, context)
    mfa.audit(db, user, "enrolled")
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"recovery_codes": codes, **mfa.token_pair(user, context)}


@router.post("/verify", response_model=TokenPair)
def verify(
    body: VerifyRequest, response: Response, db: Session = Depends(get_db_session)
) -> dict[str, Any]:
    """Exchange a single-use challenge, atomically consuming its proof."""
    from preloop.api.auth.email_verification import enforce_verified_email
    from preloop.api.auth.jwt import user_auth_generation
    from preloop.services.authentication_policy import (
        sign_in_method_allowed,
        passkey_only_refusal,
    )

    claims = mfa.read_challenge(body.challenge_token)
    user = crud_user.get(db, id=uuid.UUID(claims["sub"]))
    if user is None:
        raise mfa.error("challenge_expired")
    user = mfa.locked_user(db, user)
    obj = crud_mfa.factor(db, user.id, lock=True)
    if obj is None or obj.confirmed_at is None:
        raise mfa.error("challenge_expired")
    if (
        str(obj.id) != claims["factor_id"]
        or claims["gen"] != user_auth_generation(user)
        or claims.get("nsg") != user.non_sso_auth_generation
    ):
        raise mfa.error("challenge_expired")
    # Edition login policy sees the completed proof, never this intermediate
    # weak context. Generation and identity checks have already run above.
    if not sign_in_method_allowed(user, claims["amr"]):
        raise passkey_only_refusal()
    enforce_verified_email(user, db)
    mfa.prove(
        db,
        user,
        obj,
        code=body.code,
        recovery_code=body.recovery_code,
        challenge=claims,
    )
    context = mfa.upgrade_context(db, user, claims)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return mfa.token_pair(user, context)


@router.post("/disable", status_code=204)
def disable(
    body: ProofRequest,
    request: Request,
    user: models.User = Depends(person_session),
    db: Session = Depends(get_db_session),
) -> None:
    """Remove TOTP only after proof and the optional edition removal guard."""
    from preloop.plugins.account_hooks import get_factor_removal_policy

    user, _ = _lock_session(db, user, request)
    obj = _confirmed(db, user)
    policy = get_factor_removal_policy()
    if policy is not None:
        policy(db, user, "totp")
    mfa.prove(db, user, obj, code=body.code, recovery_code=body.recovery_code)
    crud_mfa.remove(db, user.id)
    mfa.audit(db, user, "disabled")
    db.commit()


@router.post("/recovery-codes", response_model=RecoveryCodes)
def regenerate(
    body: CodeRequest,
    request: Request,
    response: Response,
    user: models.User = Depends(person_session),
    db: Session = Depends(get_db_session),
) -> dict[str, list[str]]:
    """Replace every recovery code after a fresh authenticator proof."""
    user, _ = _lock_session(db, user, request)
    mfa.prove(db, user, _confirmed(db, user), code=body.code)
    codes, hashes = mfa.generate_recovery_codes()
    crud_mfa.replace_recovery_codes(db, user.id, hashes)
    mfa.audit(db, user, "recovery_codes_regenerated")
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"recovery_codes": codes}


@router.post("/step-up", response_model=TokenPair)
def step_up(
    body: CodeRequest,
    request: Request,
    response: Response,
    user: models.User = Depends(person_session),
    db: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Refresh strong authentication without requiring a new password sign-in."""
    user, context = _lock_session(db, user, request)
    mfa.prove(db, user, _confirmed(db, user), code=body.code)
    context = mfa.upgrade_context(db, user, context)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return mfa.token_pair(user, context)
