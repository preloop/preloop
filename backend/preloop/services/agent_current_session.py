"""Resolve a managed agent's current open runtime session.

A durable onboarded credential names the agent but no runtime session. The
permission hook and the MCP dispatcher both attribute such a call to the
agent's current open session, the same one ``notes send --agent`` resolves
to, so the two paths agree on which session a call belongs to.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional
from uuid import UUID

from preloop.models.crud import crud_managed_agent, crud_runtime_session
from preloop.models.db.session import get_session_factory

logger = logging.getLogger(__name__)


def current_agent_session_id(
    account_id: Any,
    managed_agent_id: Any,
    *,
    session_factory: Optional[Callable[[], Any]] = None,
) -> Optional[UUID]:
    """Return the managed agent's current open session, account scoped.

    Args:
        account_id: Account that owns the agent.
        managed_agent_id: Managed agent id.
        session_factory: Session factory to read with; defaults to the
            application's.

    Returns:
        The id of the agent's current runtime session when it exists in the
        account and has not ended, otherwise ``None``. Never raises: a lookup
        failure leaves the call unattributed, as it was before.
    """
    if not account_id or not managed_agent_id:
        return None
    try:
        factory = session_factory or get_session_factory()
        with factory() as db:
            agent = crud_managed_agent.get_for_account(
                db, account_id=str(account_id), agent_id=str(managed_agent_id)
            )
            if agent is None or agent.runtime_session_id is None:
                return None
            session = crud_runtime_session.get_account_session(
                db,
                account_id=str(account_id),
                runtime_session_id=str(agent.runtime_session_id),
            )
            if session is None or session.ended_at is not None:
                return None
            return session.id
    except Exception:
        logger.warning("Agent session lookup failed")
        return None
