"""Bounded authentication assurance and optional account login policy.

Core carries how and where the person authenticated through session exchanges.
Commercial policy can reject a method for the selected membership; it never
changes the selected account or receives arbitrary token claims.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.plugins.base import get_plugin_manager

AuthenticationPurpose = Literal["login", "access", "refresh", "switch", "exchange"]
_METHODS = frozenset({"password", "passkey", "social", "sso", "oidc", "saml"})
_UUID_CLAIMS = ("sso_connection_id", "auth_account_id")
_INTEGER_CLAIMS = ("nsg", "auth_nsg", "auth_time")


def authentication_claims(claims: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy only bounded assurance claims from an already authenticated token.

    This is not token verification. Callers must verify the signature or load
    a server-created authorization-code row before using this projection.
    """
    source = claims or {}
    result: dict[str, Any] = {}
    methods = source.get("amr", [])
    if (
        not isinstance(methods, list)
        or len(methods) > 3
        or any(
            not isinstance(method, str) or method not in _METHODS for method in methods
        )
    ):
        raise HTTPException(status_code=401, detail="Invalid authentication context")
    if methods:
        result["amr"] = list(dict.fromkeys(methods))
    for name in _UUID_CLAIMS:
        if name in source:
            try:
                result[name] = str(UUID(str(source[name])))
            except (ValueError, TypeError, AttributeError) as exc:
                raise HTTPException(
                    status_code=401, detail="Invalid authentication context"
                ) from exc
    for name in _INTEGER_CLAIMS:
        if name in source:
            value = source[name]
            if type(value) is not int or value < 0:
                raise HTTPException(
                    status_code=401, detail="Invalid authentication context"
                )
            result[name] = value
    if "sso_break_glass" in source:
        if type(source["sso_break_glass"]) is not bool:
            raise HTTPException(
                status_code=401, detail="Invalid authentication context"
            )
        result["sso_break_glass"] = source["sso_break_glass"]
    return result


def has_sso_context(claims: Mapping[str, Any], *, account_id: Any = None) -> bool:
    """Identify SSO assurance bound to this account, including legacy OIDC JWTs."""
    context = authentication_claims(claims)
    methods = context.get("amr", [])
    return bool(
        "sso" in methods
        and ("oidc" in methods or "saml" in methods)
        and context.get("sso_connection_id")
        and (
            account_id is None
            or context.get("auth_account_id") in (None, str(account_id))
        )
    )


def non_sso_generation(user: models.User) -> int:
    """Return the membership-local generation without canonical-person fanout."""
    value = getattr(user, "non_sso_auth_generation", 0)
    return value if type(value) is int and value >= 0 else 0


def authorize_authentication(
    db: Session,
    user: models.User,
    *,
    claims: Mapping[str, Any] | None,
    purpose: AuthenticationPurpose,
    source_ip: str | None = None,
) -> dict[str, Any]:
    """Ask the installed policy to authorize this exact membership/context."""
    context = authentication_claims(claims)
    gate = get_plugin_manager().get_service("authentication_policy")
    if gate is not None:
        gate.authorize(
            db, user=user, claims=context, purpose=purpose, source_ip=source_ip
        )
    return authentication_claims(context)


def new_authentication(
    db: Session,
    user: models.User,
    *,
    method: str,
    connection_id: UUID | None = None,
    source_ip: str | None = None,
) -> dict[str, Any]:
    """Gate a proven fresh sign-in before its tokens are minted."""
    context: dict[str, Any] = {
        "amr": ["sso", method] if method in {"oidc", "saml"} else [method],
        "auth_account_id": str(user.account_id),
        "nsg": non_sso_generation(user),
        "auth_nsg": non_sso_generation(user),
        "auth_time": int(datetime.now(UTC).timestamp()),
    }
    if connection_id is not None:
        context["sso_connection_id"] = str(connection_id)
    return authorize_authentication(
        db, user, claims=context, purpose="login", source_ip=source_ip
    )


def switched_authentication(
    db: Session,
    target: models.User,
    *,
    claims: Mapping[str, Any],
    source_account_id: UUID,
) -> dict[str, Any]:
    """Preserve original assurance while checking the destination membership."""
    context = authentication_claims(claims)
    context.setdefault("auth_account_id", str(source_account_id))
    context = authorize_authentication(db, target, claims=context, purpose="switch")
    context["nsg"] = non_sso_generation(target)
    return context


def authorize_session_record(
    db: Session, user: models.User, record: Any, *, purpose: AuthenticationPurpose
) -> dict[str, Any]:
    """Validate a persisted OAuth grant without converting service keys to sessions."""
    from preloop.api.auth.jwt import user_auth_generation

    generation = getattr(record, "auth_generation", 0)
    if type(generation) is not int:
        generation = 0
    if (
        str(record.user_id) != str(user.id)
        or str(record.account_id) != str(user.account_id)
        or generation < user_auth_generation(user)
    ):
        raise HTTPException(status_code=401, detail="Session revoked")
    context = getattr(record, "auth_context", None)
    return authorize_authentication(
        db, user, claims=context if isinstance(context, dict) else {}, purpose=purpose
    )


def select_authenticated_membership(
    db: Session, user: models.User, *, account_id: UUID | None
) -> models.User:
    """Select an already-held membership only after proving this person's credential.

    Unknown, other-person and inactive destinations share one refusal. This is
    fresh authentication; it does not upgrade an existing switched session.
    """
    if account_id is None:
        return user
    from preloop.api.auth.email_verification import enforce_verified_email
    from preloop.api.auth.identity import require_active_identity
    from preloop.models.crud import crud_person

    for candidate in crud_person.list_memberships(db, person_id=user.person_id):
        if candidate.person_id == user.person_id and candidate.account_id == account_id:
            try:
                require_active_identity(db, candidate)
                enforce_verified_email(candidate, db)
            except HTTPException as exc:
                raise HTTPException(
                    status_code=401, detail="Invalid account selection"
                ) from exc
            return candidate
    raise HTTPException(status_code=401, detail="Invalid account selection")
