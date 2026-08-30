"""Shared helper to list authorized gateway models for agent configs.

Agent config generators (OpenCode, Codex, etc.) need the full set of models
the agent principal is authorized for so the generated config includes ALL
models, not just the primary one.  This module provides a single function
that reuses the gateway's authorization predicate
(``compute_authorized_model_ids``) and runtime resolver
(``resolve_ai_model_runtime``) to return the list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence

from sqlalchemy.orm import Session

from preloop.models.models.ai_model import AIModel
from preloop.services.model_gateway_auth import (
    ModelGatewayAuthContext,
    compute_authorized_model_ids,
)
from preloop.services.model_runtime_resolver import resolve_ai_model_runtime

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthorizedGatewayModel:
    """Lightweight descriptor for one gateway-enabled model."""

    alias: str
    display_name: str


def list_authorized_gateway_models(
    db: Session,
    account_id: str,
    auth_context: Optional[ModelGatewayAuthContext] = None,
) -> List[AuthorizedGatewayModel]:
    """Return every gateway-enabled model the principal is authorized for.

    When *auth_context* is ``None`` (e.g. during flow-execution context
    building where no gateway request is in-flight) the function skips the
    per-principal authorization filter and returns all gateway-enabled models
    in the account.  This matches the behavior for user-token callers whose
    ``compute_authorized_model_ids`` returns the full inventory.

    Args:
        db: Active database session.
        account_id: Account whose model inventory to scan.
        auth_context: Gateway auth context for per-principal filtering.
            Pass ``None`` when running outside an authenticated gateway
            request to return the full account inventory.

    Returns:
        Deduplicated list of authorized, gateway-enabled models sorted by
        alias for deterministic config output.
    """
    from preloop.models.crud.ai_model import ai_model as crud_ai_model

    account_models: Sequence[AIModel] = crud_ai_model.get_all_for_account(
        db, account_id=account_id
    )
    if not account_models:
        return []

    if auth_context is not None:
        authorized_ids = compute_authorized_model_ids(db, auth_context, account_models)
    else:
        authorized_ids = frozenset(str(m.id) for m in account_models)

    result: list[AuthorizedGatewayModel] = []
    seen_aliases: set[str] = set()

    for ai_model in account_models:
        if str(ai_model.id) not in authorized_ids:
            continue
        runtime = resolve_ai_model_runtime(ai_model)
        if not runtime.model_gateway_enabled:
            continue
        alias = runtime.model_gateway_model_alias
        if not alias or alias in seen_aliases:
            continue
        seen_aliases.add(alias)
        display_name = ai_model.display_name or ai_model.model_identifier or alias
        result.append(AuthorizedGatewayModel(alias=alias, display_name=display_name))

    result.sort(key=lambda m: m.alias)
    return result
