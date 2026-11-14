"""Bounded authentication assurance and optional account login policy.

Core carries how and where the person authenticated through session exchanges.
Commercial policy can reject a method for the selected membership; it never
changes the selected account or receives arbitrary token claims.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.plugins.base import get_plugin_manager

AuthenticationPurpose = Literal["login", "access", "refresh", "switch", "exchange"]
_METHODS = frozenset(
    {
        "password",
        "passkey",
        "social",
        "email_link",
        "chat",
        "sso",
        "oidc",
        "saml",
        "otp",
    }
)
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
    for name in ("sso_break_glass", "passkey_uv"):
        if name in source:
            if type(source[name]) is not bool:
                raise HTTPException(
                    status_code=401, detail="Invalid authentication context"
                )
            result[name] = source[name]
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


# Methods a passkey-only membership still accepts. SSO sessions carry
# ``["sso", "oidc"|"saml"]``; the identity provider owns their MFA.
PASSKEY_ONLY_ALLOWED_METHODS = frozenset({"passkey", "sso", "oidc", "saml"})
PASSKEY_ONLY_REFUSED_CODE = "password_login_disabled"
PASSKEY_ONLY_REFUSED_MESSAGE = "This account signs in with a passkey"
# Purposes that mint a new session or a delegated token; a refusal here is
# audited. Access and refresh refusals are not (they would repeat per request).
_SIGN_IN_PURPOSES = frozenset({"login", "switch", "exchange"})


def is_passkey_only(user: models.User) -> bool:
    """Whether this membership refuses every sign-in method but passkey and SSO."""
    return getattr(user, "passkey_only", False) is True


def sign_in_method_allowed(user: models.User, methods: list[str]) -> bool:
    """Decide whether a session proven by ``methods`` may act as ``user``.

    This is the single helper every sign-in path goes through (via
    :func:`authorize_authentication`). Identity plugins that mint tokens call
    :func:`new_authentication`, which applies it for them.

    Args:
        user: The membership being signed in to.
        methods: The session's ``amr`` values.

    Returns:
        False when ``user`` is passkey-only and ``methods`` names only weaker
        methods; True otherwise. A context with no ``amr`` at all is not a
        sign-in: it is a server-issued delegation (administrator
        impersonation, session resume). Such tokens from before the setting
        was enabled already fail the non-SSO generation check.
    """
    if not is_passkey_only(user) or not methods:
        return True
    return any(method in PASSKEY_ONLY_ALLOWED_METHODS for method in methods)


def passkey_only_refusal() -> HTTPException:
    """The 401 every refused sign-in path returns for a passkey-only user."""
    return HTTPException(
        status_code=401,
        detail={
            "code": PASSKEY_ONLY_REFUSED_CODE,
            "message": PASSKEY_ONLY_REFUSED_MESSAGE,
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


def _enforce_core_policy(
    db: Session,
    user: models.User,
    context: Mapping[str, Any],
    *,
    purpose: AuthenticationPurpose,
    source_ip: str | None,
) -> None:
    """Apply the core checks that hold with or without a policy plugin.

    * A non-SSO session whose ``nsg`` is behind the membership's non-SSO
      generation was ended (passkey-only enable, recovery reset) and is
      refused on access, refresh and exchange.
    * A passkey-only membership refuses sessions proven by a weaker method.
    """
    if (
        purpose not in {"login", "switch"}
        and not has_sso_context(context, account_id=user.account_id)
        and context.get("nsg", 0) < non_sso_generation(user)
    ):
        raise HTTPException(status_code=401, detail="Session revoked")
    methods = list(context.get("amr", []))
    if sign_in_method_allowed(user, methods):
        return
    if purpose in _SIGN_IN_PURPOSES:
        from preloop.services.audit.service import AuditActionType
        from preloop.services.auth_audit import record_auth_event

        record_auth_event(
            db,
            action=AuditActionType.PASSKEY_ONLY_LOGIN_REFUSED,
            user=user,
            resource_type="user",
            resource_id=user.id,
            details={"method": methods[0] if methods else "unknown"},
            source_ip=source_ip,
        )
    raise passkey_only_refusal()


def authorize_authentication(
    db: Session,
    user: models.User,
    *,
    claims: Mapping[str, Any] | None,
    purpose: AuthenticationPurpose,
    source_ip: str | None = None,
    channel_action: bool = False,
) -> dict[str, Any]:
    """Authorize membership/context, with an internal paired-channel seam.

    ``channel_action`` is only for the broker's preauthorized internal transport;
    it is not a client claim. Core revocation and edition exchange policy still
    apply, and no strong authentication assurance is granted.
    """
    context = authentication_claims(claims)
    _enforce_core_policy(db, user, context, purpose=purpose, source_ip=source_ip)
    if channel_action and (purpose != "exchange" or context.get("amr") != ["chat"]):
        raise HTTPException(401, "Invalid channel authentication context")
    if purpose in {"login", "exchange", "switch"} and not channel_action:
        from preloop.services.mfa import require_challenge

        require_challenge(db, user, context)
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


def request_authentication_context(request: Any, user: models.User) -> dict[str, Any]:
    """Project the verified person session used for a browser token exchange."""
    from preloop.api.auth.jwt import decode_token

    scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Person session required")
    token_data = decode_token(bearer)
    if (
        token_data.refresh
        or token_data.sub != str(user.id)
        or token_data.claims.get("purpose")
    ):
        raise HTTPException(status_code=401, detail="Person session required")
    return authentication_claims(token_data.claims)


def check_recent_strong_auth(
    db: Session, user: models.User, context: Mapping[str, Any], max_age_seconds: int
) -> None:
    """Require bounded fresh assurance and advertise the user's usable factors."""
    from preloop.models.crud import crud_webauthn_credential, mfa as crud_mfa
    from preloop.services.mfa import clock, enabled

    methods = set(context.get("amr", []))
    strong = bool(methods & {"otp", "sso", "oidc", "saml"}) or (
        "passkey" in methods and context.get("passkey_uv") is True
    )
    now = int(clock())
    auth_time = context.get("auth_time")
    if strong and type(auth_time) is int and 0 <= now - auth_time <= max_age_seconds:
        return
    available = []
    if enabled() and crud_mfa.enabled(db, user.id):
        available.append("totp")
    if crud_webauthn_credential.list_for_person(db, person_id=user.person_id):
        available.append("passkey")
    if has_sso_context(context):
        available.append("sso")
    raise HTTPException(
        401, detail={"code": "step_up_required", "methods": available or ["enroll"]}
    )


def require_recent_strong_auth(max_age_seconds: int) -> Any:
    """Build a reusable session-only FastAPI dependency for sensitive actions.

    Args:
        max_age_seconds: Maximum age of the most recent strong proof.

    Returns:
        A dependency returning the verified authentication context.
    """
    from preloop.api.auth.jwt import decode_token, get_current_active_user
    from preloop.models.db.session import get_db_session

    def dependency(
        request: Request,
        user: models.User = Depends(get_current_active_user),
        db: Session = Depends(get_db_session),
    ) -> dict[str, Any]:
        """Validate the session class before evaluating authentication age."""
        context = request_authentication_context(request, user)
        token = decode_token(request.headers.get("authorization", "").partition(" ")[2])
        if token.sid is not None:
            raise HTTPException(401, "Console session required")
        check_recent_strong_auth(db, user, context, max_age_seconds)
        return context

    return dependency


def require_account_step_up(action: str) -> Any:
    """Expose optional edition policy on a user-session action without enforcing it in OSS."""
    from preloop.api.auth.jwt import get_current_active_user
    from preloop.models.db.session import get_db_session

    def dependency(
        request: Request,
        user: models.User = Depends(get_current_active_user),
        db: Session = Depends(get_db_session),
    ) -> None:
        """Evaluate policy in a worker thread and leave machine keys unchanged."""
        from preloop.plugins.account_hooks import enforce_step_up_policy

        enforce_step_up_policy(db, user, request, action)

    return dependency
