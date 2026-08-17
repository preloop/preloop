"""Short-lived runtime credentials for flow executions."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_api_key, crud_user

logger = logging.getLogger(__name__)


def create_flow_runtime_token(
    db: Session,
    *,
    flow: Any,
    execution_id: Optional[UUID],
    runtime_session_id: Optional[UUID] = None,
) -> tuple[Optional[str], Optional[UUID]]:
    """Mint a two-hour MCP token scoped to one flow execution."""
    account_id = getattr(flow, "account_id", None)
    try:
        account = crud_account.get(db, id=account_id)
        if not account:
            logger.warning("Account %s not found", account_id)
            return None, None

        principal_user = None
        if account.primary_user_id:
            principal_user = crud_user.get(db, id=account.primary_user_id)
            if principal_user and not principal_user.is_active:
                principal_user = None

        if not principal_user:
            users = crud_user.get_by_account(db, account_id=account_id)
            principal_user = next((user for user in users if user.is_active), None)

        if not principal_user:
            logger.warning(
                "No active users found for account %s, cannot create API token",
                account_id,
            )
            return None, None

        expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
        flow_id = getattr(flow, "id", None)
        flow_name = getattr(flow, "name", None) or str(flow_id)
        execution_id_value = str(execution_id) if execution_id is not None else None
        context_data = {
            "flow_execution_id": execution_id_value,
            "runtime_session_id": (
                str(runtime_session_id) if runtime_session_id is not None else None
            ),
            "flow_id": str(flow_id),
            "allowed_mcp_tools": getattr(flow, "allowed_mcp_tools", None) or [],
            "allowed_mcp_servers": getattr(flow, "allowed_mcp_servers", None) or [],
            "runtime_principal": {
                "type": "flow_execution",
                "id": execution_id_value,
                "name": flow_name,
                "user_id": str(principal_user.id),
                "username": principal_user.username,
            },
        }
        api_key, token_key = crud_api_key.create_runtime_key(
            db,
            name=f"Flow Execution {execution_id_value or 'temp'}",
            account_id=account_id,
            user_id=principal_user.id,
            expires_at=expires_at,
            scopes=["mcp:read", "mcp:write"],
            context_data=context_data,
        )
        logger.info(
            "Created temporary API key record id=%s for flow execution %s "
            "(principal_user=%s), expires at %s",
            api_key.id,
            execution_id,
            principal_user.username,
            expires_at,
        )
        return token_key, api_key.id
    except Exception as exc:
        logger.error(
            "Failed to create temporary API key record: %s",
            type(exc).__name__,
            exc_info=True,
        )
        db.rollback()
        return None, None
