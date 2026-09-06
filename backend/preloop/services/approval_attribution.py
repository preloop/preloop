"""Who asked for an approval: agent, API key, runtime session, flow run.

Every approval surface has to answer "which agent, on whose key, in which
session, for which run". The ids for that live on
:class:`~preloop.models.models.approval_request.ApprovalRequest`, but an id on
its own renders as a truncated UUID, and a missing display name used to render
as the generic label "AI agent". This module turns the ids into named
summaries so no surface has to guess.

Three entry points, for the three moments attribution is needed:

* :func:`attribution_from_user_context` at creation time, so a gate that has
  an authenticated caller records every id that caller carries.
* :func:`resolve_managed_agent_name` at creation time, so the denormalized
  ``managed_agent_name`` is filled in whenever the caller only had the id.
* :func:`attach_attribution` at read time, which batch-loads the referenced
  rows for a page of requests and fills the response summaries. Batched
  deliberately: the approvals list renders 50 rows, and four lookups per row
  would be 200 queries.

Lookups are best-effort. A revoked key, a deleted agent or a purged session
must degrade to "that part is omitted", never to a failed page load.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from preloop.models.models.api_key import ApiKey
from preloop.models.models.flow import Flow
from preloop.models.models.flow_execution import FlowExecution
from preloop.models.models.managed_agent import ManagedAgent
from preloop.models.models.runtime_session import RuntimeSession
from preloop.models.schemas.approval_request import (
    ApprovalAgentSummary,
    ApprovalApiKeySummary,
    ApprovalFlowExecutionSummary,
    ApprovalSessionSummary,
)

logger = logging.getLogger(__name__)


@dataclass
class CallerAttribution:
    """What the authenticated caller's context can tell us about the asker.

    Built from an MCP ``UserContext``. Every field is optional because the
    same gate serves onboarded agents (which have all four), plain API keys
    (which have a key and nothing else) and flow runs (which have a run id).
    """

    managed_agent_id: Optional[uuid.UUID] = None
    runtime_session_id: Optional[uuid.UUID] = None
    api_key_id: Optional[uuid.UUID] = None
    execution_id: Optional[str] = None
    managed_agent_name: Optional[str] = None


def _as_uuid(value: Any) -> Optional[uuid.UUID]:
    """Parse a UUID from whatever the caller had; None when it is not one."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def attribution_from_user_context(user_context: Any) -> CallerAttribution:
    """Read the attribution ids off an MCP user context.

    Tolerant by design: ``UserContext`` gained these fields over time and the
    tests build stand-ins, so every read is a ``getattr`` with a default.
    """
    if user_context is None:
        return CallerAttribution()
    execution_id = getattr(user_context, "flow_execution_id", None)
    name = (getattr(user_context, "runtime_principal_name", None) or "").strip()
    return CallerAttribution(
        managed_agent_id=_as_uuid(getattr(user_context, "managed_agent_id", None)),
        runtime_session_id=_as_uuid(getattr(user_context, "runtime_session_id", None)),
        api_key_id=_as_uuid(getattr(user_context, "api_key_id", None)),
        execution_id=str(execution_id) if execution_id else None,
        managed_agent_name=name or None,
    )


def current_caller_attribution() -> CallerAttribution:
    """Attribution for the MCP request being served, if there is one.

    Returns an empty attribution for non-MCP callers (flows, tests) rather
    than raising: an approval must still be creatable when we cannot say who
    asked, it just shows fewer links.
    """
    try:
        from preloop.services.dynamic_fastmcp_http import get_current_user_context

        return attribution_from_user_context(get_current_user_context())
    except Exception:
        logger.debug("No MCP user context available for attribution", exc_info=True)
        return CallerAttribution()


def _agent_display_name(agent: ManagedAgent) -> str:
    """Best available human label for an agent row."""
    return (
        (getattr(agent, "display_name", None) or "").strip()
        or (getattr(agent, "session_reference", None) or "").strip()
        or str(agent.id)
    )


def _session_subject(session: RuntimeSession) -> Optional[str]:
    """What the session was about, in the words the session pages use.

    ``title`` is the generated summary line, ``session_reference`` the
    caller-supplied name, ``runtime_principal_name`` the identity behind it.
    None when the session has none of them: a short id is added by the
    surface, not invented here.
    """
    for candidate in (
        getattr(session, "title", None),
        getattr(session, "session_reference", None),
        getattr(session, "runtime_principal_name", None),
    ):
        cleaned = (candidate or "").strip()
        if cleaned:
            return cleaned
    return None


async def resolve_managed_agent_name(
    db: AsyncSession,
    *,
    managed_agent_id: Optional[uuid.UUID],
    provided_name: Optional[str] = None,
) -> Optional[str]:
    """Return the agent's display name, looking it up when only the id is known.

    Creation paths that carry the id but not the name (the MCP tool gate, the
    approval wrapper) would otherwise store NULL and every surface would print
    a generic label for a request whose agent is perfectly well identified.
    """
    cleaned = (provided_name or "").strip()
    if cleaned:
        return cleaned
    agent_id = _as_uuid(managed_agent_id)
    if agent_id is None:
        return None
    try:
        result = await db.execute(
            select(ManagedAgent).where(ManagedAgent.id == agent_id).limit(1)
        )
        agent = result.scalars().first()
    except Exception:
        logger.debug(
            "Could not resolve display name for managed agent %s",
            agent_id,
            exc_info=True,
        )
        return None
    return _agent_display_name(agent) if agent is not None else None


def _load_by_id(db: Session, model: Any, ids: Iterable[Any]) -> Dict[Any, Any]:
    """Fetch rows of one model keyed by primary key; {} on any failure.

    Every step is inside the guard on purpose. Attribution is decoration: a
    revoked key, a purged session or a stubbed session in a unit test must
    cost the caller a missing link, never an exception.
    """
    try:
        wanted = list({str(i): i for i in ids if i is not None}.values())
        if not wanted:
            return {}
        rows = db.execute(select(model).where(model.id.in_(wanted))).scalars().all()
        return {row.id: row for row in rows}
    except Exception:
        logger.debug("Attribution lookup failed for %s", model.__name__, exc_info=True)
        return {}


def attach_attribution(db: Session, requests: Sequence[Any]) -> List[Any]:
    """Set ``agent``, ``api_key``, ``session`` and ``flow_execution`` on each row.

    Accepts approval-request ORM rows or already-validated
    :class:`ApprovalRequestResponse` models: both expose the four ids and both
    accept the four summary attributes, which is all this needs.
    ``ApprovalRequestResponse`` reads the attributes back by name, so an ORM
    row enriched here serialises with its attribution attached.

    Batched on purpose: the approvals list renders up to 50 rows, and four
    lookups per row would be 200 queries for one page. Parts whose id is unset
    (or whose row is gone) are set to ``None`` so the surface omits them
    rather than printing a generic label.
    """
    rows = [r for r in requests if r is not None]
    if not rows:
        return list(requests)

    agents = _load_by_id(db, ManagedAgent, (r.managed_agent_id for r in rows))
    api_keys = _load_by_id(db, ApiKey, (r.api_key_id for r in rows))
    sessions = _load_by_id(db, RuntimeSession, (r.runtime_session_id for r in rows))
    executions = _load_by_id(
        db, FlowExecution, (_as_uuid(r.execution_id) for r in rows)
    )
    flows = _load_by_id(
        db, Flow, (execution.flow_id for execution in executions.values())
    )

    for request in rows:
        agent = agents.get(request.managed_agent_id)
        request.agent = (
            ApprovalAgentSummary(
                id=agent.id,
                name=_agent_display_name(agent),
                kind=getattr(agent, "agent_kind", None),
            )
            if agent is not None
            else None
        )

        api_key = api_keys.get(request.api_key_id)
        request.api_key = (
            ApprovalApiKeySummary(id=api_key.id, name=api_key.name)
            if api_key is not None
            else None
        )

        session = sessions.get(request.runtime_session_id)
        request.session = (
            ApprovalSessionSummary(id=session.id, subject=_session_subject(session))
            if session is not None
            else None
        )

        execution = executions.get(_as_uuid(request.execution_id))
        flow = flows.get(execution.flow_id) if execution is not None else None
        request.flow_execution = (
            ApprovalFlowExecutionSummary(
                id=str(execution.id),
                flow_id=execution.flow_id,
                flow_name=getattr(flow, "name", None),
            )
            if execution is not None
            else None
        )

    return list(requests)


def attributed(db: Session, request: Any) -> Any:
    """Single-row convenience wrapper around :func:`attach_attribution`."""
    attach_attribution(db, [request])
    return request


__all__: List[str] = [
    "CallerAttribution",
    "attach_attribution",
    "attributed",
    "attribution_from_user_context",
    "current_caller_attribution",
    "resolve_managed_agent_name",
]
