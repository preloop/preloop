"""Bearer authentication helpers for the model gateway."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Optional

from sqlalchemy.orm import Session

from preloop.api.auth.jwt import (
    get_user_from_token_if_valid,
    _managed_agent_for_api_key,
)
from preloop.models.crud import crud_api_key, crud_runtime_session, crud_user
from preloop.models.crud.oauth_mcp_token import crud_oauth_mcp_access_token
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
