"""Native hook calls must reach execution counters and loop detection."""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from preloop.api.endpoints import agent_permission as endpoint
from preloop.models.crud import crud_runtime_session_activity
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator


@pytest.mark.parametrize("flow_bound", [True, False])
def test_native_calls_are_attributed_from_authenticated_identity(
    flow_bound: bool,
) -> None:
    """Execution-bound hooks reach the existing loop detector; local ones do not."""
    execution_id = uuid4() if flow_bound else None
    session_id = uuid4()
    identity = endpoint.PermissionIdentity(
        account_id=str(uuid4()),
        user_id=None,
        api_key_id=uuid4(),
        managed_agent_id=None,
        runtime_session_id=session_id,
        managed_agent_name="Synthetic review",
        flow_id=uuid4() if flow_bound else None,
        flow_execution_id=execution_id,
    )
    db = MagicMock()
    now = datetime.now(timezone.utc)
    # Exercise the real CRUD writer; only its database and secondary index are
    # replaced. This catches an unbound row, not just a changed mock signature.
    with (
        patch.object(
            endpoint, "get_session_factory", return_value=lambda: nullcontext(db)
        ),
        patch.object(crud_runtime_session_activity, "_touch_runtime_session_and_agent"),
        patch.object(crud_runtime_session_activity, "_index_tool_call_chunks"),
        patch("preloop.services.account_realtime.emit_account_event"),
    ):
        for _ in range(4):
            endpoint._record_native_tool_call(
                identity,
                runtime_session_id=session_id,
                payload=endpoint.AgentPermissionCheckRequest(
                    tool_name="Bash",
                    tool_input={"command": "printf done"},
                    source="codex_cli",
                    flow_execution_id=str(uuid4()),
                ),
                decision="allow",
                reason=None,
                approval_request_id=None,
                timed_out=False,
                elapsed_ms=5,
            )
    rows = [call.args[0] for call in db.add.call_args_list]
    assert len(rows) == 4
    assert all(row.flow_execution_id == execution_id for row in rows)
    assert all(row.runtime_session_id == session_id for row in rows)
    assert all("printf done" not in str(row.metadata_) for row in rows)
    if not flow_bound:
        return
    for index, row in enumerate(rows):
        row.timestamp = now + timedelta(seconds=3 * index)
    orchestrator = FlowExecutionOrchestrator(
        db=db, flow_id=str(identity.flow_id), trigger_event_data={}, nats_client=None
    )
    orchestrator.execution_log = SimpleNamespace(id=execution_id)
    with patch.object(
        crud_runtime_session_activity,
        "get_recent_tool_calls_by_flow_execution",
        return_value=list(reversed(rows)),
    ) as recent:
        signatures = orchestrator._get_recent_runtime_tool_activity_signatures()
    recent.assert_called_once_with(db, flow_execution_id=execution_id, limit=12)
    assert orchestrator._detect_repeated_tool_cycle(signatures) is not None
