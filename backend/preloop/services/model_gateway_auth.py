"""Bearer authentication helpers for the model gateway."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Optional, Sequence

from sqlalchemy.orm import Session

from preloop.api.auth.jwt import (
    get_user_from_token_if_valid,
    _managed_agent_for_api_key,
)
from preloop.models.crud import (
    crud_api_key,
    crud_managed_agent,
    crud_managed_agent_ai_model_binding,
    crud_runtime_session,
    crud_user,
)
from preloop.models.crud.oauth_mcp_token import crud_oauth_mcp_access_token
from preloop.models.models.ai_model import AIModel
from preloop.models.models.api_key import ApiKey
from preloop.models.models.oauth_mcp_token import OAuthMCPAccessToken
from preloop.models.models.user import User

logger = logging.getLogger(__name__)

# Explicit empty bearer for synthetic gateway contexts that already have a
# resolved ``User`` (internal replay/optimization). ``authenticate_bearer_token``
# treats falsy tokens as unauthenticated; callers must never pass this to that
# path — only to ``ModelGatewayAuthContext`` when the user is already known.
NO_BEARER_TOKEN = ""


@dataclass
class ModelGatewayAuthContext:
    """Authenticated model gateway request context."""

    token: str
    user: User
    api_key: Optional[ApiKey] = None
    oauth_access_token: Optional[OAuthMCPAccessToken] = None


async def authenticate_bearer_token(
    token: str, db: Session
) -> Optional[ModelGatewayAuthContext]:
    """Authenticate a bearer token while preserving ApiKey context."""
    if not token:
        return None

    user = await get_user_from_token_if_valid(token, db)
    if user:
        api_key = crud_api_key.get_by_key(db, key=token)
        if api_key is not None:
            if not api_key.is_active or api_key.is_expired:
                return None
            context_data = (
                api_key.context_data if isinstance(api_key.context_data, dict) else {}
            )
            runtime_session_id = context_data.get("runtime_session_id")
            runtime_session = None
            if runtime_session_id:
                runtime_session = crud_runtime_session.get_account_session(
                    db,
                    account_id=str(api_key.account_id),
                    runtime_session_id=str(runtime_session_id),
                )
                if runtime_session is None or runtime_session.ended_at is not None:
                    return None
            managed_agent = _managed_agent_for_api_key(
                db, api_key, runtime_session=runtime_session
            )
            if context_data.get("managed_agent_id") and managed_agent is None:
                return None
            if managed_agent is not None and managed_agent.lifecycle_state != "active":
                return None
        return ModelGatewayAuthContext(token=token, user=user, api_key=api_key)

    # Not a valid user token. If it maps to a known API key, log WHY it was
    # rejected — a revoked durable agent credential (e.g. the managed agent
    # was deleted or suspended) otherwise surfaces only as a generic 401 and
    # is very hard to diagnose in the field.
    rejected_key = crud_api_key.get_by_key(db, key=token)
    if rejected_key is not None:
        if not rejected_key.is_active:
            reason = "key is deactivated (agent deleted/suspended or offboarded)"
        elif rejected_key.is_expired:
            reason = "key is expired"
        else:
            reason = "key binding is invalid (managed agent missing or inactive)"
        logger.warning(
            "Model gateway rejected API key %s (%s): %s — re-run "
            "`preloop agents onboard` to mint a fresh credential",
            rejected_key.key_prefix,
            rejected_key.name,
            reason,
        )
        return None

    oauth_token = crud_oauth_mcp_access_token.get_by_token(db, token=token)
    if not oauth_token or oauth_token.is_revoked:
        return None
    if oauth_token.expires_at and oauth_token.expires_at < int(time.time()):
        return None

    oauth_user = crud_user.get(db, id=str(oauth_token.user_id))
    if not oauth_user or not oauth_user.is_active:
        return None

    return ModelGatewayAuthContext(
        token=token,
        user=oauth_user,
        oauth_access_token=oauth_token,
    )


def build_runtime_key_auth_context(
    db: Session, *, token: str, api_key_id: str
) -> Optional[ModelGatewayAuthContext]:
    """Build a gateway auth context for an already-minted runtime API key.

    Non-request callers (e.g. the flow orchestrator building an execution
    context) mint a short-lived runtime credential and then need to answer
    "which models may this principal use?" with the same rules the gateway
    applies to a live request.  Resolving the key row and its owning user
    here avoids re-authenticating the plaintext token.

    Args:
        db: Active database session.
        token: Plaintext runtime token minted for the principal.
        api_key_id: Id of the ``ApiKey`` row backing *token*.

    Returns:
        Context carrying the key and its owning user, or ``None`` when the
        key or user cannot be resolved or is not usable.  Callers must treat
        ``None`` as "no principal" and fail closed.
    """
    if not token or not api_key_id:
        return None

    api_key = crud_api_key.get(db, id=api_key_id)
    if api_key is None or not api_key.is_active or api_key.is_expired:
        return None

    user = crud_user.get(db, id=str(api_key.user_id))
    if user is None or not user.is_active:
        return None

    return ModelGatewayAuthContext(token=token, user=user, api_key=api_key)


def resolve_managed_agent_id_for_context(
    db: Session, auth_context: ModelGatewayAuthContext
) -> Optional[str]:
    """Resolve the managed-agent identity behind a gateway principal.

    Args:
        db: Active database session.
        auth_context: Authenticated model gateway request context.

    Returns:
        The managed agent id string when the credential carries one — either
        directly via ``context_data.managed_agent_id`` or indirectly via its
        runtime principal source identity — otherwise ``None``.
    """
    api_key = auth_context.api_key
    context_data = (
        api_key.context_data
        if api_key is not None and isinstance(api_key.context_data, dict)
        else {}
    )
    managed_agent_id = context_data.get("managed_agent_id")
    if managed_agent_id:
        return str(managed_agent_id)

    runtime_principal = context_data.get("runtime_principal") or {}
    session_source_type = runtime_principal.get("type")
    session_source_id = runtime_principal.get("id")
    if not session_source_type or not session_source_id:
        return None

    managed_agent = crud_managed_agent.get_by_source(
        db,
        account_id=str(auth_context.user.account_id),
        session_source_type=session_source_type,
        session_source_id=session_source_id,
    )
    return str(managed_agent.id) if managed_agent is not None else None


def compute_authorized_model_ids(
    db: Session,
    auth_context: ModelGatewayAuthContext,
    account_models: Sequence[AIModel],
) -> frozenset[str]:
    """Compute the ids of ``account_models`` this gateway principal may use.

    Authorization rules, in order:

    - BYOK / API-key-backed / ambient models are authorized for every
      principal in the account (unchanged behavior).
    - Subscription-OAuth models (``is_principal_bound_oauth``) are authorized
      ONLY for the managed-agent principal whose id has an active row in
      ``managed_agent_ai_model_binding`` for that model.
    - User tokens (console-originated calls, no API key and no OAuth MCP
      token) keep full visibility of the account inventory.
    - Fail closed: credentials that resolve to no managed agent — legacy
      gateway keys without ``managed_agent_id`` and OAuth MCP client tokens —
      are never authorized for principal-bound models, and bound models with
      no binding row are authorized for nobody but user tokens.

    Args:
        db: Active database session.
        auth_context: Authenticated model gateway request context.
        account_models: Full account model inventory to authorize against.

    Returns:
        Frozen set of authorized ``AIModel`` id strings.
    """
    all_ids = frozenset(str(ai_model.id) for ai_model in account_models)
    bound_ids = {
        str(ai_model.id)
        for ai_model in account_models
        if bool(getattr(ai_model, "is_principal_bound_oauth", False))
    }
    if not bound_ids:
        return all_ids

    is_user_token = (
        auth_context.api_key is None and auth_context.oauth_access_token is None
    )
    if is_user_token:
        return all_ids

    authorized = set(all_ids - bound_ids)
    managed_agent_id = resolve_managed_agent_id_for_context(db, auth_context)
    if managed_agent_id is None:
        return frozenset(authorized)

    bindings = crud_managed_agent_ai_model_binding.list_for_agent(
        db,
        account_id=str(auth_context.user.account_id),
        agent_id=managed_agent_id,
    )
    for binding in bindings:
        bound_model_id = str(binding.ai_model_id)
        if bound_model_id in bound_ids:
            authorized.add(bound_model_id)
    return frozenset(authorized)
