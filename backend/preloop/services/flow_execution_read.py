"""Reading one execution from inside an agent turn: ``get_execution`` (#632).

``run_flow`` (#630) returns the moment a child execution row exists, so a
parent that wants to know how its child ended has to ask again later. This
module is that read: it answers with the same A2A shaped task record the
delegation call answers with (#625), so a parent polls the shape it was
already handed rather than a second vocabulary.

The read is scoped, and the scope is the whole point. An execution id is a
UUID a model can invent, so nothing here trusts one:

1. the calling execution is resolved from the runtime identity, never from a
   tool argument;
2. the target is loaded account scoped, so an execution of another account is
   not readable and not even distinguishable from one that does not exist;
3. the target must be the caller itself or a descendant of it, established by
   walking ``parent_execution_id`` upwards to the caller.

Everything that fails any of those three is refused with one reason,
``execution_not_found``, and one message. A sibling, an unrelated execution of
the same account, an execution of another account and a UUID nobody ever
issued are answered identically on purpose: a differentiated refusal is an
existence oracle, and one that a model could walk through an account with.

The result payload is bounded rather than trusted too:
``FLOW_DELEGATION_RESULT_MAX_BYTES`` caps what one call may return, and a
larger result comes back truncated, flagged, and with a pointer to the full
document instead of pasted whole into the caller's context.

Not here, on purpose: reading an arbitrary execution of the account, listing
executions, streaming or subscribing, and evidence archives. Waiting for a
child is #633.

Operator facing documentation: ``docs/guide/flows/flow-delegation.md``.
"""

from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal
from typing import Any, Dict, Final, List, Optional, Tuple

from sqlalchemy.orm import Session

from preloop.a2a.delegation import task_state_for_status
from preloop.config import settings
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.models.flow import Flow
from preloop.models.models.flow_execution import FlowExecution
from preloop.services.flow_delegation_call import (
    DelegationUnavailableError,
    console_url_for,
    refusal_record,
)

logger = logging.getLogger(__name__)

#: Name of the read tool, as an agent calls it and as a flow selects it.
GET_EXECUTION_TOOL_NAME: Final[str] = "get_execution"

#: The kind marker every delegation task record carries (#625).
_TASK_KIND: Final[str] = "delegation_task"

#: The one reason an out of scope read is refused with. Every case that is
#: not "the caller or one of its descendants" lands here, so no refusal
#: tells a caller whether the execution it guessed at exists.
NOT_VISIBLE_REASON: Final[str] = "execution_not_found"

#: Hard stop on the ancestor walk, independent of the configured depth cap.
#: The walk is over data this instance wrote and cannot loop, but a walk is
#: bounded rather than trusted.
_MAX_ANCESTOR_WALK: Final[int] = 64

#: A2A task states that mean the execution has stopped. Derived from the
#: frozen status mapping rather than from a second list of Preloop statuses,
#: so a new status is terminal here exactly when #625 says it is.
TERMINAL_TASK_STATES: Final[frozenset] = frozenset(
    {
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED",
    }
)

#: Artifact metadata keys. A2A leaves an artifact's ``metadata`` open, which
#: is where the truncation report lives: the task ``metadata`` object is
#: closed by the #625 schema and says nothing about a payload, while these
#: describe one artifact and belong to it.
ARTIFACT_TRUNCATED_KEY: Final[str] = "preloop.ai/truncated"
ARTIFACT_BYTES_KEY: Final[str] = "preloop.ai/resultBytes"
ARTIFACT_MAX_BYTES_KEY: Final[str] = "preloop.ai/resultMaxBytes"
ARTIFACT_POINTER_KEY: Final[str] = "preloop.ai/resultPath"

#: Status message metadata key carrying why a terminal execution did not
#: succeed, from the closed vocabulary in ``flow_failure_category``.
FAILURE_CATEGORY_KEY: Final[str] = "preloop.ai/failureCategory"

#: Where the whole result stays readable when the artifact was truncated.
_RESULT_PATH_TEMPLATE: Final[str] = "GET /flows/executions/{execution_id}/result"


def result_size_cap() -> int:
    """Largest result payload, in bytes, one read may return whole."""
    return max(0, int(settings.flow_delegation_result_max_bytes))


def _refusal_message(reference: str) -> str:
    """The one message every refused read comes back with.

    Deliberately free of any detail about the target: it names what the
    caller asked for and what the tool may read, and nothing about what
    exists.
    """
    return (
        f"no execution '{reference}' is readable from this execution; "
        "get_execution reads this execution and the executions it started, "
        "and nothing else"
    )


def _parse_execution_id(reference: Any) -> Optional[str]:
    """Normalise an agent supplied execution id, or None if it is not one.

    A reference that is not a UUID is refused like any other unreadable one,
    and never reaches a query: an id from a model turn is an argument, not a
    lookup key.
    """
    text = str(reference or "").strip()
    if not text:
        return None
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError, TypeError):
        return None


def is_visible_to(
    db: Session,
    *,
    caller: FlowExecution,
    target: FlowExecution,
    account_id: Any,
) -> bool:
    """Whether ``caller`` may read ``target``: itself, or one of its own.

    Walks ``parent_execution_id`` upwards from the target looking for the
    caller. Ancestry is walked from the target rather than descendants listed
    from the caller because it is bounded by the delegation depth cap and
    costs one indexed lookup per level, whereas a descendant listing grows
    with the fan out.
    """
    if str(target.id) == str(caller.id):
        return True

    seen = {str(target.id)}
    current: Optional[FlowExecution] = target
    steps = 0
    while current is not None and steps < _MAX_ANCESTOR_WALK:
        parent_id = getattr(current, "parent_execution_id", None)
        if parent_id is None:
            return False
        if str(parent_id) == str(caller.id):
            return True
        if str(parent_id) in seen:
            # Impossible on lineage this instance writes; refuse to spin.
            logger.warning(
                "Execution ancestry walk revisited execution %s; stopping",
                parent_id,
            )
            return False
        seen.add(str(parent_id))
        current = crud_flow_execution.get(
            db, id=str(parent_id), account_id=str(account_id)
        )
        steps += 1
    return False


def _iso(value: Any) -> Optional[str]:
    """Format a timestamp as ISO 8601, naive columns read as UTC."""
    from datetime import datetime, timezone

    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _status_timestamp(execution: FlowExecution) -> Optional[str]:
    """When the execution reached the state being reported."""
    for attribute in ("end_time", "updated_at", "start_time"):
        stamp = _iso(getattr(execution, attribute, None))
        if stamp:
            return stamp
    return None


def _cost(execution: FlowExecution) -> Optional[float]:
    """Estimated spend so far, as a JSON number, or None if unrecorded."""
    value = getattr(execution, "estimated_cost", None)
    if value is None:
        return None
    if isinstance(value, Decimal):
        value = float(value)
    try:
        cost = float(value)
    except (TypeError, ValueError):
        return None
    return cost if cost >= 0 else None


def _tokens(execution: FlowExecution) -> Optional[int]:
    """Tokens spent so far, or None if unrecorded."""
    value = getattr(execution, "total_tokens", None)
    if value is None:
        return None
    try:
        tokens = int(value)
    except (TypeError, ValueError):
        return None
    return tokens if tokens >= 0 else None


def _status_message(execution: FlowExecution) -> Optional[Dict[str, Any]]:
    """The A2A status message for an execution that ended badly.

    Carries the coarse ``failure_category`` next to the human readable
    ``error_message``: the category is what a parent branches on, the message
    is what a human reads. Absent when there is nothing to say.
    """
    category = getattr(execution, "failure_category", None)
    error = getattr(execution, "error_message", None)
    if not category and not error:
        return None

    text = str(error) if error else f"execution failed ({category})"
    message: Dict[str, Any] = {
        "messageId": f"{execution.id}-status",
        "role": "ROLE_AGENT",
        "parts": [{"text": text}],
    }
    if category:
        message["metadata"] = {FAILURE_CATEGORY_KEY: str(category)}
    return message


def _result_artifact(execution: FlowExecution, *, cap: int) -> Optional[Dict[str, Any]]:
    """The child's result as one A2A artifact, truncated if it is large.

    A result under the cap is returned as a data part, which is the whole
    document and is what a parent wants to read. Over the cap, and for a
    result that is not a JSON object, the artifact carries a text part
    instead: the serialised document, cut to the cap, with the truncation
    stated in the artifact's description and in its metadata, and with the
    path that still serves the whole thing.

    Truncation is reported rather than silent because a parent that acts on
    half a document is worse than one that knows it has half.
    """
    payload = getattr(execution, "result", None)
    if payload is None:
        return None

    try:
        serialised = json.dumps(payload, default=str)
    except (TypeError, ValueError):  # pragma: no cover - JSONB is JSON
        serialised = str(payload)
    size = len(serialised.encode("utf-8"))

    artifact: Dict[str, Any] = {
        "artifactId": f"{execution.id}-result",
        "name": "result",
    }
    fits = size <= cap
    if fits and isinstance(payload, dict):
        artifact["description"] = "The result document this execution reported."
        artifact["parts"] = [{"data": payload, "mediaType": "application/json"}]
        artifact["metadata"] = {
            ARTIFACT_TRUNCATED_KEY: False,
            ARTIFACT_BYTES_KEY: size,
            ARTIFACT_MAX_BYTES_KEY: cap,
        }
        return artifact

    pointer = _RESULT_PATH_TEMPLATE.format(execution_id=execution.id)
    # Cut on bytes, not on characters, so the cap holds for a result written
    # in a script where one character is several bytes; a cut that lands
    # inside a character drops that character rather than emitting a broken
    # one.
    truncated = (
        serialised.encode("utf-8")[:cap].decode("utf-8", errors="ignore")
        if not fits
        else serialised
    )
    if fits:
        artifact["description"] = (
            "The result this execution reported, serialised as JSON text "
            "because it is not a JSON object."
        )
    else:
        artifact["description"] = (
            f"Truncated: the result is {size} bytes and this tool returns at "
            f"most {cap}. The whole document stays readable at {pointer}."
        )
    artifact["parts"] = [{"text": truncated, "mediaType": "text/plain"}]
    artifact["metadata"] = {
        ARTIFACT_TRUNCATED_KEY: not fits,
        ARTIFACT_BYTES_KEY: size,
        ARTIFACT_MAX_BYTES_KEY: cap,
        ARTIFACT_POINTER_KEY: pointer,
    }
    return artifact


def execution_record(
    execution: FlowExecution,
    *,
    flow: Flow,
    include_result: bool = False,
    cap: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the A2A task record for one execution as its parent reads it.

    Unlike the record ``run_flow`` returns for a child that has just been
    created, this one reports what the execution has actually done: the cost
    and tokens it has spent so far, why it failed if it did, and its result
    when it has finished and the caller asked for it.

    Args:
        execution: The execution being read.
        flow: The flow that execution is running.
        include_result: Whether the caller asked for the result payload.
        cap: Largest result returned whole, in bytes. Defaults to the
            configured cap.

    Returns:
        One task record, valid against the frozen ``delegation_task`` schema.
    """
    status = str(execution.status)
    state = task_state_for_status(status)
    root_execution_id = getattr(execution, "root_execution_id", None)
    parent_execution_id = getattr(execution, "parent_execution_id", None)

    metadata: Dict[str, Any] = {
        "preloop.ai/kind": _TASK_KIND,
        "preloop.ai/executionId": str(execution.id),
        "preloop.ai/rootExecutionId": (
            str(root_execution_id) if root_execution_id is not None else None
        ),
        "preloop.ai/flowId": str(flow.id),
        "preloop.ai/flowName": str(flow.name),
        "preloop.ai/depth": int(getattr(execution, "delegation_depth", 0) or 0),
        "preloop.ai/status": status,
    }
    if parent_execution_id is not None:
        metadata["preloop.ai/parentExecutionId"] = str(parent_execution_id)
    cost = _cost(execution)
    if cost is not None:
        metadata["preloop.ai/cost"] = cost
    tokens = _tokens(execution)
    if tokens is not None:
        metadata["preloop.ai/tokens"] = tokens
    console_url = console_url_for(execution.id)
    if console_url:
        metadata["preloop.ai/consoleUrl"] = console_url

    task_status: Dict[str, Any] = {"state": state}
    timestamp = _status_timestamp(execution)
    if timestamp:
        task_status["timestamp"] = timestamp
    message = _status_message(execution)
    if message is not None:
        task_status["message"] = message

    record: Dict[str, Any] = {
        "id": str(execution.id),
        "contextId": str(root_execution_id or execution.id),
        "status": task_status,
        "metadata": metadata,
    }

    # An execution that is still working has produced nothing to hand over,
    # and a caller that did not ask for the payload does not pay for it.
    if include_result and state in TERMINAL_TASK_STATES:
        artifact = _result_artifact(
            execution, cap=result_size_cap() if cap is None else max(0, int(cap))
        )
        if artifact is not None:
            record["artifacts"] = [artifact]
    return record


def audit_execution_read(
    *,
    account_id: str,
    user_id: Optional[str],
    reference: str,
    include_result: bool,
    outcome: str,
    caller_execution_id: Any,
    correlation_id: Optional[str],
    runtime_session_id: Optional[str] = None,
    api_key_id: Optional[str] = None,
    api_key_name: Optional[str] = None,
) -> None:
    """Write one audit row for one read, permitted or refused.

    One row per call, on the helper every other tool call is audited through,
    carrying the calling execution and the correlation id of the call: the
    actor is the agent identity the execution is running under, which is what
    makes "who read this child" answerable without a log.
    """
    try:
        from preloop.services.dynamic_mcp_server import _log_tool_execution_async

        _log_tool_execution_async(
            account_id=account_id,
            user_id=user_id,
            tool_name=GET_EXECUTION_TOOL_NAME,
            tool_args={
                "execution_id": reference,
                "include_result": bool(include_result),
            },
            status=outcome,
            execution_id=(str(caller_execution_id) if caller_execution_id else None),
            correlation_id=correlation_id,
            runtime_session_id=runtime_session_id,
            api_key_id=api_key_id,
            api_key_name=api_key_name,
        )
    except Exception:  # pragma: no cover - auditing must not break a read
        logger.debug("Failed to audit execution read", exc_info=True)


def _caller(
    db: Session, *, account_id: str, caller_execution_id: Any
) -> Tuple[FlowExecution, Flow]:
    """Resolve the calling execution and its flow, or refuse to judge at all."""
    caller = crud_flow_execution.get(
        db, id=str(caller_execution_id), account_id=str(account_id)
    )
    if caller is None:
        raise DelegationUnavailableError(
            "get_execution is only available inside a flow execution"
        )
    caller_flow = crud_flow.get(db, id=str(caller.flow_id))
    if caller_flow is None or str(caller_flow.account_id) != str(account_id):
        raise DelegationUnavailableError(
            "get_execution is only available inside a flow execution"
        )
    return caller, caller_flow


def read_execution(
    db: Session,
    *,
    account_id: str,
    caller_execution_id: Any,
    reference: str,
    include_result: bool = False,
    correlation_id: Optional[str] = None,
    user_id: Optional[str] = None,
    runtime_session_id: Optional[str] = None,
    api_key_id: Optional[str] = None,
    api_key_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Read one execution the caller is entitled to, or refuse.

    Args:
        db: Database session.
        account_id: Account of the calling execution.
        caller_execution_id: The calling execution, from its runtime identity
            and never from a tool argument.
        reference: The execution id the agent asked for.
        include_result: Whether to return the result payload.
        correlation_id: Correlation id of the tool call, for the audit row.
        user_id: Calling identity, for the audit row.
        runtime_session_id: Runtime session of the call, for the audit row.
        api_key_id: Runtime token id, for the audit row.
        api_key_name: Runtime token name, for the audit row.

    Returns:
        One A2A shaped task record: the execution when the caller may read
        it, a rejected record carrying ``execution_not_found`` when it may
        not, whatever the reason it may not.

    Raises:
        DelegationUnavailableError: The caller is not a flow execution.
    """
    caller, caller_flow = _caller(
        db, account_id=account_id, caller_execution_id=caller_execution_id
    )

    def _refuse() -> Dict[str, Any]:
        message = _refusal_message(str(reference))
        audit_execution_read(
            account_id=str(account_id),
            user_id=user_id,
            reference=str(reference),
            include_result=include_result,
            outcome=f"refused:{NOT_VISIBLE_REASON}",
            caller_execution_id=caller.id,
            correlation_id=correlation_id,
            runtime_session_id=runtime_session_id,
            api_key_id=api_key_id,
            api_key_name=api_key_name,
        )
        logger.info("Execution read refused: execution_id=%s", caller.id)
        return refusal_record(
            reason=NOT_VISIBLE_REASON,
            message=message,
            flow_id=caller_flow.id,
            flow_name=caller_flow.name,
            depth=int(getattr(caller, "delegation_depth", 0) or 0),
            parent_execution_id=caller.id,
            root_execution_id=getattr(caller, "root_execution_id", None) or caller.id,
            attempt_id=correlation_id or str(uuid.uuid4()),
        )

    target_id = _parse_execution_id(reference)
    if target_id is None:
        return _refuse()

    target = crud_flow_execution.get(db, id=target_id, account_id=str(account_id))
    if target is None:
        return _refuse()
    if not is_visible_to(db, caller=caller, target=target, account_id=account_id):
        return _refuse()

    target_flow = crud_flow.get(db, id=str(target.flow_id))
    if target_flow is None or str(target_flow.account_id) != str(account_id):
        # A row whose flow is gone or has moved account is not readable
        # either, and is refused in the same words as anything else.
        return _refuse()

    record = execution_record(target, flow=target_flow, include_result=include_result)
    audit_execution_read(
        account_id=str(account_id),
        user_id=user_id,
        reference=str(reference),
        include_result=include_result,
        outcome=f"read:{record['status']['state']}",
        caller_execution_id=caller.id,
        correlation_id=correlation_id,
        runtime_session_id=runtime_session_id,
        api_key_id=api_key_id,
        api_key_name=api_key_name,
    )
    return record


__all__: List[str] = [
    "GET_EXECUTION_TOOL_NAME",
    "NOT_VISIBLE_REASON",
    "TERMINAL_TASK_STATES",
    "audit_execution_read",
    "execution_record",
    "is_visible_to",
    "read_execution",
    "result_size_cap",
]
