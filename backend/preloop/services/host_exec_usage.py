"""Usage bookkeeping for flow runs on host-exec runner profiles.

A host profile runs the runner user's own CLI login, so the model gateway
never sees the spend. What the control plane can show instead:

* hook-pushed sessions and events for the CLI session, linked to the
  execution (the usage hook forwards ``PRELOOP_FLOW_EXECUTION_ID``; the
  completion back-links rows pushed without it), and
* for Copilot, the premium requests the CLI reported in its ``result``
  event, stored as one subscription-bound imported row.

None of these rows is gateway traffic: they use ``action_type
'imported_usage'`` and never carry an estimated cost.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.models.crud import crud_api_usage, crud_runtime_session

logger = logging.getLogger(__name__)

#: Ingest source labels a host harness's usage hook writes by default.
HOST_EXEC_HOOK_SOURCES: Dict[str, Tuple[str, ...]] = {
    "copilot": ("copilot_cli", "copilot"),
    "cursor": ("cursor",),
}

#: Source label of the Copilot premium-request row.
COPILOT_PREMIUM_SOURCE = "copilot_cli"

#: Upper bound accepted for one run's premium requests.
MAX_PREMIUM_REQUESTS = 100_000.0


def host_exec_premium_fingerprint(execution_id: Any) -> str:
    """Dedupe key of the premium-request row for one execution.

    Args:
        execution_id: Flow execution id.

    Returns:
        A stable fingerprint, so a replayed completion cannot double count.
    """
    return f"host_exec:{execution_id}"


def _premium_requests(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > MAX_PREMIUM_REQUESTS:
        return None
    return number


def _session_id(result: Mapping[str, Any]) -> Optional[str]:
    value = result.get("session_id")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:255] or None


def record_host_exec_completion_usage(
    db: Session,
    execution: Any,
    *,
    account_id: UUID,
    result: Optional[Mapping[str, Any]],
    pending_job: Optional[Mapping[str, Any]],
) -> None:
    """Link hook rows and store Copilot premium requests for a host run.

    Runs inside the completion transaction without committing. Failures are
    logged and swallowed inside a savepoint: usage bookkeeping must never
    block the terminal status.

    Args:
        db: Database session.
        execution: Flow execution row being completed.
        account_id: Account of the runner.
        result: Validated completion result.
        pending_job: Persisted lease; only ``host_exec`` leases are handled.
    """
    if not isinstance(pending_job, Mapping):
        return
    if pending_job.get("completion_protocol") != "host_exec":
        return
    if not isinstance(result, Mapping):
        return
    from preloop.services.host_exec import host_exec_harness

    agent_type = str(pending_job.get("agent_type") or "").lower()
    harness = host_exec_harness(agent_type)
    if harness is None or result.get("harness") != harness:
        return
    session_id = _session_id(result)
    try:
        with db.begin_nested():
            flow_session = crud_runtime_session.get_by_source(
                db,
                account_id=account_id,
                session_source_type="flow_execution",
                session_source_id=str(execution.id),
            )
            if session_id:
                _link_hook_session(
                    db,
                    account_id=account_id,
                    agent_type=agent_type,
                    session_id=session_id,
                    execution=execution,
                    flow_session_id=getattr(flow_session, "id", None),
                )
            premium = _premium_requests(result.get("premium_requests"))
            if agent_type == "copilot" and premium is not None:
                crud_api_usage.log_imported_usage_event(
                    db,
                    account_id=str(account_id),
                    timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
                    model_alias=(
                        str(result["model"])[:255]
                        if isinstance(result.get("model"), str)
                        else None
                    ),
                    source=COPILOT_PREMIUM_SOURCE,
                    cost_usd=None,
                    cost_source="subscription",
                    conversation_id=session_id,
                    runtime_session_id=getattr(flow_session, "id", None),
                    flow_id=execution.flow_id,
                    flow_execution_id=execution.id,
                    import_fingerprint=host_exec_premium_fingerprint(execution.id),
                    meta_data={
                        "event_type": "host_exec_result",
                        "harness": harness,
                        "premium_requests": premium,
                        "gateway_metered": False,
                    },
                    endpoint="/runners/host-exec/copilot",
                    commit=False,
                )
    except SQLAlchemyError:
        logger.warning(
            "Host execution usage bookkeeping failed for %s",
            execution.id,
            exc_info=True,
        )


def _link_hook_session(
    db: Session,
    *,
    account_id: UUID,
    agent_type: str,
    session_id: str,
    execution: Any,
    flow_session_id: Optional[UUID],
) -> None:
    providers = list(HOST_EXEC_HOOK_SOURCES.get(agent_type, ()))
    crud_api_usage.link_imported_rows_to_flow_execution(
        db,
        account_id=account_id,
        providers=providers,
        conversation_id=session_id,
        flow_id=execution.flow_id,
        flow_execution_id=execution.id,
    )
    if flow_session_id is None:
        return
    for provider in providers:
        hook_session = crud_runtime_session.get_by_source(
            db,
            account_id=account_id,
            session_source_type=provider,
            session_source_id=session_id,
        )
        if (
            hook_session is not None
            and hook_session.parent_session_id is None
            and hook_session.id != flow_session_id
        ):
            hook_session.parent_session_id = flow_session_id
            db.add(hook_session)
    db.flush()


def summarize_host_exec_usage(
    db: Session, *, account_id: UUID, execution_id: UUID
) -> Dict[str, Any]:
    """Hook sessions, event counts and premium requests of one execution.

    Args:
        db: Database session.
        account_id: Owning account id.
        execution_id: Flow execution id.

    Returns:
        A dict with ``sessions``, ``event_count``, ``premium_requests`` and
        ``gateway_metered`` (always False for these rows).
    """
    rows = crud_api_usage.list_imported_rows_for_flow_execution(
        db, account_id=account_id, flow_execution_id=execution_id
    )
    premium: Optional[float] = None
    sessions: Dict[str, Dict[str, Any]] = {}
    event_count = 0
    for row in rows:
        meta = row.meta_data or {}
        if meta.get("event_type") == "host_exec_result":
            value = _premium_requests(meta.get("premium_requests"))
            if value is not None:
                premium = (premium or 0.0) + value
            continue
        event_count += 1
        key = row.conversation_id or ""
        entry = sessions.get(key)
        if entry is None:
            entry = sessions[key] = {
                "conversation_id": row.conversation_id,
                "source": row.provider_name,
                "runtime_session_id": row.runtime_session_id,
                "event_count": 0,
                "event_types": {},
                "first_event_at": row.timestamp,
                "last_event_at": row.timestamp,
                "models": [],
            }
        entry["event_count"] += 1
        event_type = str(meta.get("event_type") or "usage")
        entry["event_types"][event_type] = entry["event_types"].get(event_type, 0) + 1
        entry["last_event_at"] = row.timestamp
        if entry["runtime_session_id"] is None:
            entry["runtime_session_id"] = row.runtime_session_id
        if row.model_alias and row.model_alias not in entry["models"]:
            entry["models"].append(row.model_alias)
    ordered: List[Dict[str, Any]] = sorted(
        sessions.values(), key=lambda item: item["first_event_at"]
    )
    return {
        "execution_id": execution_id,
        "sessions": ordered,
        "event_count": event_count,
        "premium_requests": premium,
        "gateway_metered": False,
    }
