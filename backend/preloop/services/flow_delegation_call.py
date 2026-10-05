"""Call time delegation: what happens when a flow calls ``run_flow`` (#630).

``preloop.services.flow_delegation`` is the write side of the delegation
allowlist (issue #627): it validates a ``callable_flows`` list as an operator
saves it. This module is the other half, the one that runs inside an agent's
turn: it decides whether one execution may start another, creates the child
when it may, and answers in the A2A shaped records frozen by issue #625.

Everything here fails closed. A delegation tool that fails open is a way to
spend an account's budget from inside a model turn, so each rule is checked
server side, in a fixed order, and a refusal comes back as a structured
record rather than as an exception the agent has to read out of a stack
trace:

1. ``run_flow`` is on the calling flow's ``allowed_mcp_tools``
   (``tool_not_allowed``). Read from the flow row rather than from the api
   key context the execution carries, so revoking the tool mid run takes
   effect at once.
2. the target reference names a flow in the calling account
   (``flow_not_found``) and that flow is enabled (``flow_not_callable``).
3. the target is named on the calling flow's ``callable_flows``
   (``flow_not_callable``).
4. the depth cap and the cycle guard pass (``depth_exceeded``,
   ``cycle_detected``).
5. the direct child count is under both ceilings (``fanout_exceeded``).
6. the delegation tree can afford the child's cost ceiling
   (``budget_exceeded``, #631, in ``flow_delegation_budget``). Money is
   checked last because it is the only rule whose answer changes while the
   flow definition does not.

The central CEL policy is another rule and it is not evaluated here: every
tool call already passes through ``DynamicFastMCP.call_tool``, which
evaluates policy (and the account kill switch) before dispatch, so a policy
deny never reaches this module. Checking it again here would double the audit
rows without changing an outcome; a deny is final wherever it is read.

The account kill switch is checked twice for the same reason it is checked at
all: ``call_tool`` denies every tool while the ``tools`` scope is halted, and
``FlowTriggerService`` refuses to start a flow while the ``flows`` scope is.
This module reuses the second one by letting ``FlowHaltActiveError`` out.

Operator facing documentation of the same rules and the settings that bound
them: ``docs/guide/flows/flow-delegation.md``.

Not here, on purpose: reading a child's result (#632), waiting for a child
in any form (#633). Nothing in this module blocks the calling turn.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from sqlalchemy.orm import Session

from preloop.a2a.delegation import (
    REFUSED_STATUS,
    task_state_for_status,
)
from preloop.config import settings
from preloop.flow_presets import PRESET_SLUGS
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.models.flow import Flow
from preloop.models.models.flow_execution import FlowExecution
from preloop.models.schemas.flow import CallableFlowEntry, callable_flows_for
from preloop.services.flow_delegation_budget import (
    COST_CEILING_KEY,
    DELEGATION_DETAILS_KEY,
    DelegationBudgetError,
    check_child_affordable,
    fanout_batch_id,
    resolve_child_ceiling,
)

logger = logging.getLogger(__name__)

#: Name of the delegation tool, as an agent calls it and as a flow selects it.
RUN_FLOW_TOOL_NAME = "run_flow"

#: ``DELEGATION_DETAILS_KEY`` and ``COST_CEILING_KEY`` are defined in
#: ``flow_delegation_budget`` (which reads them off child rows without
#: importing this module) and re-exported here, where the record they key is
#: written.

#: ``trigger_event.source`` of a delegated run, so a prompt (and a human
#: reading the row) can tell a child from a webhook or a schedule.
DELEGATION_SOURCE = "flow_delegation"

#: ``log_type`` of the row that keeps a refused call on the calling
#: execution's timeline. A refusal creates no child execution, so this is the
#: only durable place a parent can read back what it asked for and did not
#: get (#633 reports one row per attempt, refusals included).
DELEGATION_REFUSAL_LOG_TYPE = "delegation_refusal"

#: Hard stop for the ancestor walk, independent of the configured depth cap:
#: lineage is written by this module and cannot loop, but a walk over data is
#: bounded anyway rather than trusted.
_MAX_ANCESTOR_WALK = 64

#: The kind marker every delegation task record carries (#625).
_TASK_KIND = "delegation_task"


class DelegationRefusedError(Exception):
    """One of the delegation rules declined the call.

    Carries the frozen refusal reason from ``preloop.a2a.delegation`` so the
    caller can turn it into a rejected task record; the message is for the
    human reading the audit row and the agent reading the record.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class DelegationUnavailableError(Exception):
    """The call cannot be judged at all, so there is nothing to refuse.

    Raised when ``run_flow`` is reached from something that is not a flow
    execution. That is not a policy decision about a delegation, it is a
    caller that has no parent to delegate from.
    """


@dataclass(frozen=True)
class DelegationDecision:
    """An allowed call: who is calling, what will run, and where it sits."""

    parent_execution: FlowExecution
    parent_flow: Flow
    target_flow: Flow
    entry: CallableFlowEntry
    depth: int
    root_execution_id: uuid.UUID
    #: Cost ceiling in USD the child is admitted under, already clamped by
    #: the allowlist entry and already checked against the tree's remaining
    #: allowance (#631). None when nothing bounds this child.
    cost_ceiling_usd: Optional[float] = None


def max_delegation_depth() -> int:
    """Deepest ``delegation_depth`` a delegated child may be created at."""
    return max(0, int(settings.flow_delegation_max_depth))


def max_children_per_parent() -> int:
    """How many direct children one execution may start through run_flow."""
    return max(0, int(settings.flow_delegation_max_children))


def tool_names_for(flow: Flow) -> List[str]:
    """Read the tool allow-list off a flow row.

    ``allowed_mcp_tools`` holds either plain names or objects carrying
    ``tool_name`` (the stored shape) or ``name`` (the legacy one), the same
    two shapes the MCP server accepts when it mints an execution's context.
    """
    names: List[str] = []
    for tool in getattr(flow, "allowed_mcp_tools", None) or []:
        if isinstance(tool, str):
            names.append(tool)
        elif isinstance(tool, dict):
            name = tool.get("tool_name") or tool.get("name")
            if name:
                names.append(str(name))
    return names


def resolve_delegation_target(
    db: Session, *, reference: str, account_id: Union[str, uuid.UUID]
) -> Optional[Flow]:
    """Resolve a ``run_flow`` reference to a flow inside ``account_id``.

    Deliberately narrower than the write side resolver in
    ``preloop.services.flow_delegation``: exact name, then a preset catalog
    slug, both indexed lookups. The write side also tries a case insensitive
    name match, which is a sequential scan; an operator saving a form can
    afford one, an agent turn on the tool path should not, and an allowlist
    that was validated on write already names flows that exist.

    Args:
        db: Database session.
        reference: The slug or name the agent asked for.
        account_id: Account that owns the calling flow.

    Returns:
        The flow the reference names, or None. Never a flow of another
        account: the account is the resolution scope, so a reference to a
        flow next door and a reference to nothing are the same answer.
    """
    reference = (reference or "").strip()
    if not reference or account_id is None:
        return None
    exact = crud_flow.get_by_name_and_account(db, name=reference, account_id=account_id)
    if exact is not None:
        return exact

    preset_name = PRESET_SLUGS.get(reference)
    if preset_name is None:
        return None
    by_preset_name = crud_flow.get_by_name_and_account(
        db, name=preset_name, account_id=account_id
    )
    if by_preset_name is not None:
        return by_preset_name
    global_preset = crud_flow.get_global_preset_by_name(db, name=preset_name)
    if global_preset is None:
        return None
    return crud_flow.get_by_source_preset(
        db, account_id=account_id, source_preset_id=global_preset.id
    )


def _allowlist_entry_for(
    db: Session, *, caller: Flow, target: Flow
) -> Optional[CallableFlowEntry]:
    """Find the caller's allowlist entry that names ``target``.

    Entries are compared by resolved flow id, not by string: an entry may be
    written as a preset slug and the agent may ask by name (or the other way
    round) and both are the same permission.
    """
    for entry in callable_flows_for(caller):
        resolved = resolve_delegation_target(
            db, reference=entry.flow, account_id=caller.account_id
        )
        if resolved is not None and str(resolved.id) == str(target.id):
            return entry
        if str(entry.flow).casefold() == str(target.name or "").casefold():
            return entry
    return None


def callable_names_hint(flow: Flow, *, limit: int = 10) -> str:
    """Render the caller's allowlist for a refusal message.

    A reference that does not resolve is almost always a name the agent read
    out of the wrong column (a result schema id, a preset title, a project
    path). The refusal is the only place the agent is looking at the time, so
    it carries the names that would have worked. Empty allowlist renders as a
    statement of that fact, not as an empty list.
    """
    names = [str(entry.flow) for entry in callable_flows_for(flow) if entry.flow]
    if not names:
        return "this flow has no callable flows configured"
    shown = names[:limit]
    rendered = ", ".join(f"'{name}'" for name in shown)
    if len(names) > limit:
        rendered += f" and {len(names) - limit} more"
    return f"callable flows here: {rendered}"


def ancestor_flow_ids(db: Session, execution: FlowExecution) -> List[str]:
    """Flow ids of ``execution`` and of every execution above it.

    Walks ``parent_execution_id`` upwards. The list is ordered from the
    calling execution to the root, so a cycle message can say where the
    target already appears.
    """
    chain: List[str] = []
    seen: set[str] = set()
    current: Optional[FlowExecution] = execution
    steps = 0
    while current is not None and steps < _MAX_ANCESTOR_WALK:
        if str(current.id) in seen:
            # Impossible on data this module writes; refuse to spin anyway.
            logger.warning(
                "Delegation ancestor walk revisited execution %s; stopping",
                current.id,
            )
            break
        seen.add(str(current.id))
        chain.append(str(current.flow_id))
        parent_id = getattr(current, "parent_execution_id", None)
        if parent_id is None:
            break
        current = crud_flow_execution.get(db, id=str(parent_id))
        steps += 1
    return chain


def _direct_children(db: Session, parent: FlowExecution, account_id: Any) -> List[Any]:
    """Executions this parent has already started."""
    return crud_flow_execution.get_children(
        db,
        parent_execution_id=parent.id,
        account_id=account_id,
    )


def evaluate_delegation(
    db: Session,
    *,
    parent_execution: FlowExecution,
    parent_flow: Flow,
    reference: str,
    max_cost_usd: Optional[float] = None,
) -> DelegationDecision:
    """Run every server side rule for one delegation, in order.

    Args:
        db: Database session.
        parent_execution: The execution making the call.
        parent_flow: The flow that execution is running.
        reference: Slug or name the agent asked for.
        max_cost_usd: Cost ceiling the agent asked for, if any. Clamped by
            the allowlist entry and then checked against what the tree has
            left.

    Returns:
        The decision, carrying the target flow, the allowlist entry that
        permitted it and the ceiling the child will be admitted under.

    Raises:
        DelegationRefusedError: The first rule that declined, with its reason.
    """
    from preloop.models import models

    if isinstance(parent_execution, models.FlowExecution) and any(
        value is not None
        for value in (
            parent_execution.ci_principal_id,
            parent_execution.ci_review_binding,
            parent_execution.initiating_ci_key_id,
        )
    ):
        raise DelegationRefusedError(
            "restricted_ci",
            "Restricted CI v1 permits one execution only",
        )

    depth = int(getattr(parent_execution, "delegation_depth", 0) or 0) + 1
    root_execution_id = (
        getattr(parent_execution, "root_execution_id", None) or parent_execution.id
    )

    if RUN_FLOW_TOOL_NAME not in tool_names_for(parent_flow):
        raise DelegationRefusedError(
            "tool_not_allowed",
            f"flow '{parent_flow.name}' does not have the "
            f"{RUN_FLOW_TOOL_NAME} tool enabled",
        )

    target = resolve_delegation_target(
        db, reference=reference, account_id=parent_flow.account_id
    )
    if target is None:
        raise DelegationRefusedError(
            "flow_not_found",
            f"'{reference}' does not name a flow in this account; "
            f"{callable_names_hint(parent_flow)}",
        )
    if not getattr(target, "is_enabled", True):
        raise DelegationRefusedError(
            "flow_not_callable",
            f"flow '{target.name}' is disabled",
        )

    entry = _allowlist_entry_for(db, caller=parent_flow, target=target)
    if entry is None:
        raise DelegationRefusedError(
            "flow_not_callable",
            f"flow '{target.name}' is not on the callable flows allowlist of "
            f"'{parent_flow.name}'; {callable_names_hint(parent_flow)}",
        )

    limit = max_delegation_depth()
    if depth > limit:
        raise DelegationRefusedError(
            "depth_exceeded",
            f"a child here would be at delegation depth {depth}; the maximum "
            f"delegation depth is {limit}",
        )

    chain = ancestor_flow_ids(db, parent_execution)
    if str(target.id) in chain:
        position = chain.index(str(target.id))
        where = (
            "is the flow making this call"
            if position == 0
            else f"is already running {position} level(s) above this execution"
        )
        raise DelegationRefusedError(
            "cycle_detected",
            f"flow '{target.name}' {where}; a delegation cycle is refused",
        )

    children = _direct_children(db, parent_execution, parent_flow.account_id)
    ceiling = max_children_per_parent()
    if len(children) >= ceiling:
        raise DelegationRefusedError(
            "fanout_exceeded",
            f"this execution already started {len(children)} child(ren); the "
            f"maximum number of direct children is {ceiling}",
        )
    if entry.max_children is not None:
        through_entry = sum(
            1 for child in children if str(child.flow_id) == str(target.id)
        )
        if through_entry >= entry.max_children:
            raise DelegationRefusedError(
                "fanout_exceeded",
                f"this execution already started {through_entry} child(ren) of "
                f"'{target.name}'; the allowlist entry caps it at "
                f"{entry.max_children}",
            )

    ceiling = resolve_child_ceiling(
        requested=max_cost_usd, entry_ceiling=entry.max_usd_per_child
    )
    try:
        check_child_affordable(
            db,
            parent_execution=parent_execution,
            account_id=parent_flow.account_id,
            ceiling_usd=ceiling,
            target_name=target.name,
        )
    except DelegationBudgetError as exc:
        raise DelegationRefusedError("budget_exceeded", str(exc)) from exc

    return DelegationDecision(
        parent_execution=parent_execution,
        parent_flow=parent_flow,
        target_flow=target,
        entry=entry,
        depth=depth,
        root_execution_id=root_execution_id,
        cost_ceiling_usd=ceiling,
    )


def remaining_parent_seconds(execution: FlowExecution) -> Optional[int]:
    """Seconds left in the calling execution's own wall clock budget.

    None when the execution has no start time yet (nothing has been spent),
    which leaves the requested timeout unclamped rather than guessing.
    """
    from preloop.services.flow_orchestrator import (
        FLOW_TIMEOUT_SECONDS_MAX,
        FLOW_TIMEOUT_SECONDS_MIN,
    )

    flow = getattr(execution, "flow", None)
    configured = getattr(flow, "timeout_seconds", None)
    if configured is None:
        configured = settings.flow_execution_max_wait_seconds
    try:
        budget = int(configured)
    except (TypeError, ValueError):
        budget = int(settings.flow_execution_max_wait_seconds)
    budget = max(FLOW_TIMEOUT_SECONDS_MIN, min(FLOW_TIMEOUT_SECONDS_MAX, budget))

    started = getattr(execution, "start_time", None)
    if started is None:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    return max(0, int(budget - elapsed))


def clamp_timeout(
    requested: Optional[int], *, parent_execution: FlowExecution
) -> Optional[int]:
    """Bound a requested child window by the caller's own remaining window.

    A child cannot usefully outlive the turn that is going to read it, so a
    request longer than the parent has left comes back shortened rather than
    refused. Nothing waits on this value yet: it is recorded on the child so
    the park in #633 has a window to honour.
    """
    if requested is None:
        return None
    try:
        seconds = int(requested)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    remaining = remaining_parent_seconds(parent_execution)
    if remaining is None:
        return seconds
    return max(1, min(seconds, remaining))


def console_url_for(execution_id: Any) -> Optional[str]:
    """Deep link to one execution in the console, when a base url is set.

    Public because get_execution (#632) and the child wait (#633) link the
    same executions, and two spellings of one url is one spelling too many.
    """
    import os

    base = os.getenv("PRELOOP_URL", "").strip()
    if not base:
        return None
    return f"{base.rstrip('/')}/console/flows/executions/{execution_id}"


def task_record_for_execution(
    execution: FlowExecution,
    *,
    flow: Flow,
    parent_execution_id: Any,
    root_execution_id: Any,
    depth: int,
) -> Dict[str, Any]:
    """Build the A2A task record standing for one child execution (#625).

    Only the fields a just created child can honestly populate are written:
    no artifacts, because a child that has not run has produced nothing, and
    no cost, because nothing has been spent. ``contextId`` is the root of the
    delegation tree, which is what groups a tree in A2A terms.
    """
    status = str(execution.status)
    metadata: Dict[str, Any] = {
        "preloop.ai/kind": _TASK_KIND,
        "preloop.ai/executionId": str(execution.id),
        "preloop.ai/parentExecutionId": str(parent_execution_id),
        "preloop.ai/rootExecutionId": (
            str(root_execution_id) if root_execution_id is not None else None
        ),
        "preloop.ai/flowId": str(flow.id),
        "preloop.ai/flowName": str(flow.name),
        "preloop.ai/depth": int(depth),
        "preloop.ai/status": status,
    }
    console_url = console_url_for(execution.id)
    if console_url:
        metadata["preloop.ai/consoleUrl"] = console_url

    record: Dict[str, Any] = {
        "id": str(execution.id),
        "contextId": str(root_execution_id),
        "status": {
            "state": task_state_for_status(status),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "metadata": metadata,
    }
    return record


def refusal_record(
    *,
    reason: str,
    message: str,
    flow_id: Any,
    flow_name: Optional[str],
    depth: int,
    parent_execution_id: Any,
    root_execution_id: Any,
    attempt_id: str,
) -> Dict[str, Any]:
    """Build the A2A task record for a delegation that never started (#625).

    A refusal is not a failure: nothing ran and nothing was charged, so the
    state is ``TASK_STATE_REJECTED`` and there is no execution id, because no
    row exists. ``preloop.ai/flowId`` names the target when one resolved and
    the calling flow when the reference resolved to nothing, so the record is
    always about a flow that exists; the reason says which of the two it is.
    """
    metadata: Dict[str, Any] = {
        "preloop.ai/kind": _TASK_KIND,
        "preloop.ai/parentExecutionId": str(parent_execution_id),
        "preloop.ai/rootExecutionId": (
            str(root_execution_id) if root_execution_id is not None else None
        ),
        "preloop.ai/flowId": str(flow_id),
        "preloop.ai/depth": int(depth),
        "preloop.ai/status": REFUSED_STATUS,
        "preloop.ai/refusalReason": reason,
    }
    if flow_name:
        metadata["preloop.ai/flowName"] = str(flow_name)
    return {
        "id": attempt_id,
        "contextId": str(root_execution_id),
        "status": {
            "state": task_state_for_status(REFUSED_STATUS),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": {
                "messageId": attempt_id,
                "role": "ROLE_AGENT",
                "parts": [{"text": message}],
            },
        },
        "metadata": metadata,
    }


def audit_delegation_refusal(
    *,
    account_id: str,
    user_id: Optional[str],
    reason: str,
    message: str,
    reference: str,
    parent_execution_id: Any,
    correlation_id: Optional[str],
    runtime_session_id: Optional[str] = None,
    api_key_id: Optional[str] = None,
    api_key_name: Optional[str] = None,
) -> None:
    """Write one audit row for one refused delegation.

    Fire and forget, on the same helper every other tool call is audited
    through, so a refusal sits next to the executions it did not create and
    carries the same correlation id as the call that was refused.
    """
    try:
        from preloop.services.dynamic_mcp_server import _log_tool_execution_async

        _log_tool_execution_async(
            account_id=account_id,
            user_id=user_id,
            tool_name=RUN_FLOW_TOOL_NAME,
            tool_args={
                "flow": reference,
                "refusal_reason": reason,
                "refusal_message": message,
            },
            status=f"refused:{reason}",
            execution_id=str(parent_execution_id) if parent_execution_id else None,
            correlation_id=correlation_id,
            runtime_session_id=runtime_session_id,
            api_key_id=api_key_id,
            api_key_name=api_key_name,
        )
    except Exception:  # pragma: no cover - auditing must not break a refusal
        logger.debug("Failed to audit delegation refusal", exc_info=True)


def record_refusal_on_parent(
    db: Session,
    *,
    parent_execution_id: Any,
    record: Dict[str, Any],
    label: Optional[str] = None,
) -> None:
    """Keep a refused call on the parent's own timeline (#633).

    A refusal creates no child row, so without this the only trace of it is
    the tool audit log. A parent that later parks on its children has to
    report what it asked for and did not get, so the refusal record is stored
    as one log row on the calling execution and read back by
    ``flow_child_wait`` when the parent is resumed. Never raises: a refusal
    that could not be written down is still a refusal.
    """
    try:
        crud_flow_execution.append_log(
            db,
            execution_id=str(parent_execution_id),
            log_data={
                "type": DELEGATION_REFUSAL_LOG_TYPE,
                "message": (
                    "run_flow refused: "
                    f"{record.get('metadata', {}).get('preloop.ai/refusalReason')}"
                ),
                "metadata": {
                    "milestone": DELEGATION_REFUSAL_LOG_TYPE,
                    "label": str(label)[:200] if label else None,
                    "task": record,
                },
            },
        )
    except Exception:  # pragma: no cover - a log row must not break a refusal
        logger.debug("Could not record the refusal on the parent", exc_info=True)


def _child_trigger_details(
    *,
    decision: DelegationDecision,
    payload: Optional[Dict[str, Any]],
    label: Optional[str],
    timeout_seconds: Optional[int],
    correlation_id: Optional[str],
) -> Dict[str, Any]:
    """Trigger snapshot for the child: agent payload plus its lineage.

    The cost ceiling is part of that snapshot rather than a column: it is
    the number the child was admitted under, so it has to survive a restart
    and be readable by anything summing the tree, and it is written once and
    never updated.
    """
    return {
        "source": DELEGATION_SOURCE,
        "payload": dict(payload) if isinstance(payload, dict) else {},
        DELEGATION_DETAILS_KEY: {
            "parent_execution_id": str(decision.parent_execution.id),
            "root_execution_id": str(decision.root_execution_id),
            "parent_flow_id": str(decision.parent_flow.id),
            "parent_flow_name": str(decision.parent_flow.name),
            "depth": decision.depth,
            "label": str(label) if label else None,
            "timeout_seconds": timeout_seconds,
            "correlation_id": correlation_id,
            COST_CEILING_KEY: decision.cost_ceiling_usd,
        },
    }


async def delegate_flow(
    db: Session,
    *,
    account_id: str,
    parent_execution_id: Any,
    reference: str,
    payload: Optional[Dict[str, Any]] = None,
    label: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    max_cost_usd: Optional[float] = None,
    correlation_id: Optional[str] = None,
    user_id: Optional[str] = None,
    runtime_session_id: Optional[str] = None,
    api_key_id: Optional[str] = None,
    api_key_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Start one child execution of an allowlisted flow, or refuse.

    Args:
        db: Database session.
        account_id: Account of the calling execution.
        parent_execution_id: The calling execution, from its runtime identity
            and never from a tool argument.
        reference: Slug or name of the flow to run.
        payload: Trigger payload for the child.
        label: Optional label recorded on the child.
        timeout_seconds: Optional window, clamped to the caller's own.
        max_cost_usd: Optional cost ceiling for the child, clamped by the
            allowlist entry and refused when the tree cannot afford it.
        correlation_id: Correlation id of the tool call, for the audit row.
        user_id: Calling identity, for the audit row.
        runtime_session_id: Runtime session of the call, for the audit row.
        api_key_id: Runtime token id, for the audit row.
        api_key_name: Runtime token name, for the audit row.

    Returns:
        One A2A shaped task record: the child on success, a rejected record
        carrying a refusal reason when a rule declined the call.

    Raises:
        DelegationUnavailableError: The caller is not a flow execution.
        FlowHaltActiveError: The account kill switch halts new executions.
    """
    parent_execution = crud_flow_execution.get(
        db, id=str(parent_execution_id), account_id=str(account_id)
    )
    if parent_execution is None:
        raise DelegationUnavailableError(
            "run_flow is only available inside a flow execution"
        )
    parent_flow = crud_flow.get(db, id=str(parent_execution.flow_id))
    if parent_flow is None or str(parent_flow.account_id) != str(account_id):
        raise DelegationUnavailableError(
            "run_flow is only available inside a flow execution"
        )

    depth = int(getattr(parent_execution, "delegation_depth", 0) or 0) + 1
    root_execution_id = (
        getattr(parent_execution, "root_execution_id", None) or parent_execution.id
    )

    try:
        decision = evaluate_delegation(
            db,
            parent_execution=parent_execution,
            parent_flow=parent_flow,
            reference=reference,
            max_cost_usd=max_cost_usd,
        )
    except DelegationRefusedError as refusal:
        audit_delegation_refusal(
            account_id=str(account_id),
            user_id=user_id,
            reason=refusal.reason,
            message=refusal.message,
            reference=reference,
            parent_execution_id=parent_execution.id,
            correlation_id=correlation_id,
            runtime_session_id=runtime_session_id,
            api_key_id=api_key_id,
            api_key_name=api_key_name,
        )
        logger.info(
            "Delegation refused (%s) for execution %s: %s",
            refusal.reason,
            parent_execution.id,
            refusal.message,
        )
        target = None
        if refusal.reason != "flow_not_found":
            target = resolve_delegation_target(
                db, reference=reference, account_id=parent_flow.account_id
            )
        named = target if target is not None else parent_flow
        record = refusal_record(
            reason=refusal.reason,
            message=refusal.message,
            flow_id=named.id,
            flow_name=named.name,
            depth=depth,
            parent_execution_id=parent_execution.id,
            root_execution_id=root_execution_id,
            attempt_id=correlation_id or str(uuid.uuid4()),
        )
        record_refusal_on_parent(
            db,
            parent_execution_id=parent_execution.id,
            record=record,
            label=label,
        )
        return record

    child, child_flow = await _start_child(
        db,
        decision=decision,
        payload=payload,
        label=label,
        timeout_seconds=clamp_timeout(
            timeout_seconds, parent_execution=parent_execution
        ),
        correlation_id=correlation_id,
    )
    return task_record_for_execution(
        child,
        flow=child_flow,
        parent_execution_id=decision.parent_execution.id,
        root_execution_id=decision.root_execution_id,
        depth=decision.depth,
    )


async def _start_child(
    db: Session,
    *,
    decision: DelegationDecision,
    payload: Optional[Dict[str, Any]],
    label: Optional[str],
    timeout_seconds: Optional[int],
    correlation_id: Optional[str],
) -> Tuple[FlowExecution, Flow]:
    """Create the child row through the trigger service and read it back.

    The trigger service is the only thing that starts a flow, so a delegated
    start inherits its account kill switch check, its routing sanitation and
    its dispatch, instead of a second creation path that would drift.

    Every child of one parent is created with the same ``batch_id``, derived
    from the parent execution, so one fan out is a batch in exactly the sense
    a matrix trigger already is and the existing batch rollup endpoint
    reports its cost with no new query (#631).
    """
    from preloop.services.flow_trigger_service import FlowTriggerService

    service = FlowTriggerService(db)
    result = await service.trigger_flow(
        flow_id=decision.target_flow.id,
        trigger_event_data=_child_trigger_details(
            decision=decision,
            payload=payload,
            label=label,
            timeout_seconds=timeout_seconds,
            correlation_id=correlation_id,
        ),
        triggered_by=f"flow {decision.parent_flow.name}",
        parent_execution_id=decision.parent_execution.id,
        root_execution_id=decision.root_execution_id,
        delegation_depth=decision.depth,
        batch_id=fanout_batch_id(decision.parent_execution.id),
    )
    child = crud_flow_execution.get(db, id=str(result["id"]))
    if child is None:  # pragma: no cover - the row was just committed
        raise DelegationUnavailableError(
            f"child execution {result['id']} could not be read back"
        )
    return child, decision.target_flow
