"""Account kill switch: one control to halt all agent activity.

The kill switch is the account-scoped emergency stop (#157). While a scope
is active, the platform refuses the traffic class it covers:

* ``gateway`` — every model-gateway request (OpenAI, Anthropic, and Gemini
  ingress alike) is rejected with a distinct, clearly-attributed 403 before
  any upstream dispatch, and the rejection is recorded on the account's
  usage ledger so rejected traffic stays attributable.
* ``tools`` — every MCP tool call is denied, and pending approvals are
  frozen (their timeout no longer auto-expires them) rather than
  auto-denied.
* ``flows`` — no new flow executions start: triggers are dropped (recorded
  as events), manually triggered runs are refused, and already-created
  PENDING executions are not claimed by workers until the scope is lifted.

Running containers are not terminated or suspended. Their subsequent
model-gateway and MCP requests are blocked, but already-authorized work,
local commands and requests bypassing Preloop may continue. This is a
traffic gate, not a complete emergency stop for running agents.

Reaction time: halt state is cached in-process for a few seconds so the
gateway/tool hot paths pay no extra DB round-trip per request in the common
(not halted) case; toggles invalidate the cache immediately in the process
that served them, and every other process converges within the TTL.

Re-enable is staged: each scope is lifted independently, so an operator can
restore the gateway first, verify behavior, then tools, then flows.
"""

from __future__ import annotations

import logging
import time
from threading import Lock
from typing import TYPE_CHECKING, FrozenSet, Optional, Set
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import crud_account_halt
from preloop.models.models.account_halt import (
    HALT_SCOPE_FLOWS,
    HALT_SCOPE_GATEWAY,
    HALT_SCOPE_TOOLS,
)
from preloop.services.model_gateway_errors import (
    GatewayProvider,
    ModelGatewayAPIError,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: Process-level cache TTL. Bounds how long a toggle takes to reach other
#: API/worker processes ("within seconds") while keeping the hot path free
#: of per-request DB queries for the overwhelmingly common not-halted case.
KILL_SWITCH_CACHE_TTL_SECONDS = 5.0

#: Distinct error code surfaced to gateway clients (OpenAI ``error.code``
#: field) so tooling can attribute the rejection to the halt and not to a
#: generic failure.
KILL_SWITCH_ERROR_CODE = "preloop_account_halted"

#: Error class recorded on usage rows and echoed as ``X-Preloop-Error-Class``
#: so rejected traffic is attributable in the cost/audit surfaces.
KILL_SWITCH_ERROR_CLASS = "kill_switch"

_CACHE: dict[str, tuple[FrozenSet[str], float]] = {}
_LOCK = Lock()

GATEWAY_DENIAL_MESSAGE = (
    "Account kill switch active: all model requests for this account are "
    "rejected until the halt is lifted by an operator"
)
TOOL_DENIAL_MESSAGE = (
    "Access denied: the account kill switch is active. All tool calls are "
    "rejected until the halt is lifted by an operator."
)
FLOW_DENIAL_MESSAGE = (
    "Account kill switch active: new flow executions are blocked until the "
    "halt is lifted by an operator"
)


class FlowHaltActiveError(Exception):
    """Raised when a flow execution cannot start because of an active halt."""


def invalidate_kill_switch_cache(account_id: Optional[str | UUID] = None) -> None:
    """Drop cached halt state after a toggle.

    Args:
        account_id: One account, or ``None`` to clear every account (tests).
    """
    with _LOCK:
        if account_id is None:
            _CACHE.clear()
        else:
            _CACHE.pop(str(account_id), None)


def halted_scopes(db: Session, account_id: str | UUID) -> Set[str]:
    """Return the set of currently-halted scopes for an account.

    Cached per account for ``KILL_SWITCH_CACHE_TTL_SECONDS``; toggles call
    :func:`invalidate_kill_switch_cache` so the process that served the
    toggle enforces it on the very next request.

    The account_halt migration is required. Lookup errors propagate so
    unavailable halt state cannot silently permit traffic.
    """
    key = str(account_id)
    now = time.monotonic()
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is not None:
            scopes, expires_at = entry
            if now < expires_at:
                return set(scopes)
            _CACHE.pop(key, None)

    scopes = frozenset(crud_account_halt.active_scopes(db, account_id=account_id))
    with _LOCK:
        _CACHE[key] = (scopes, now + KILL_SWITCH_CACHE_TTL_SECONDS)
    return set(scopes)


async def halted_scopes_async(db: "AsyncSession", account_id: str | UUID) -> Set[str]:
    """Async-session variant of :func:`halted_scopes`.

    Approval-service callers hold an ``AsyncSession`` across the human
    decision; ``run_sync`` borrows its connection for the cached sync
    lookup without opening a second one.
    """
    scopes = await db.run_sync(lambda session: halted_scopes(session, account_id))
    return set(scopes)


async def tools_halted_async(db: "AsyncSession", account_id: str | UUID) -> bool:
    """Async-session variant of :func:`tools_halted`."""
    return HALT_SCOPE_TOOLS in await halted_scopes_async(db, account_id)


def gateway_halted(db: Session, account_id: str | UUID) -> bool:
    """Return True when model-gateway traffic is halted for the account."""
    return HALT_SCOPE_GATEWAY in halted_scopes(db, account_id)


def tools_halted(db: Session, account_id: str | UUID) -> bool:
    """Return True when MCP tool calls are halted for the account."""
    return HALT_SCOPE_TOOLS in halted_scopes(db, account_id)


def flows_halted(db: Session, account_id: str | UUID) -> bool:
    """Return True when new flow executions are halted for the account."""
    return HALT_SCOPE_FLOWS in halted_scopes(db, account_id)


def gateway_halt_error(
    *, provider: GatewayProvider = "openai", reason: Optional[str] = None
) -> ModelGatewayAPIError:
    """Build the distinct gateway rejection for an active halt.

    Args:
        provider: Client wire format the error is rendered in.
        reason: Optional operator-supplied halt reason, surfaced so the
            rejection explains itself.

    Returns:
        A 403 :class:`ModelGatewayAPIError` carrying the kill-switch error
        code and error class.
    """
    message = GATEWAY_DENIAL_MESSAGE
    if reason:
        message = f"{message} (reason: {reason})"
    return ModelGatewayAPIError(
        provider=provider,
        status_code=403,
        message=message,
        code=KILL_SWITCH_ERROR_CODE,
        error_class=KILL_SWITCH_ERROR_CLASS,
    )


def halt_reason(db: Session, account_id: str | UUID, scope: str) -> Optional[str]:
    """Return the operator-supplied reason a scope was halted, if recorded."""
    row = crud_account_halt.get_for_scope(db, account_id=account_id, scope=scope)
    if row is None or not row.is_active:
        return None
    return row.reason


def account_halt_status(db: Session, account_id: str | UUID) -> dict:
    """Return the render-ready kill-switch status for an account.

    The snapshot is always read fresh (never the hot-path cache): status
    reads happen on toggles and banner polls, not per model request.
    """
    return crud_account_halt.snapshot_for_account(db, account_id=account_id)
