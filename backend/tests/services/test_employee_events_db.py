"""Durable employee reservation against synthetic PostgreSQL fixtures."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from preloop.models.crud import crud_flow, crud_managed_agent
from preloop.models.schemas.flow import FlowCreate
from preloop.services import employee_events as service


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_kind", ["codex", "nanobot"])
async def test_both_employee_runtime_identities_persist_owned_deduplicated_task(
    db_session,
    test_user,
    monkeypatch,
    runtime_kind,
):
    agent = crud_managed_agent.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "owner_user_id": test_user.id,
            "agent_kind": runtime_kind,
            "session_source_type": runtime_kind,
            "session_source_id": f"employee-{uuid4()}",
            "display_name": "Example employee",
            "lifecycle_state": "active",
            "lifecycle_updated_at": datetime.now(UTC),
            "last_seen_at": datetime.now(UTC),
        },
    )
    flow = crud_flow.create(
        db_session,
        flow_in=FlowCreate(
            name="Example employee flow",
            prompt_template="Handle {{trigger_event.payload}}",
            account_id=test_user.account_id,
            agent_type="codex",
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            trigger_event_source="discord",
            trigger_event_types=["channel_message"],
            trigger_config={
                "employee_events": {
                    "source": "discord",
                    "connection_id": "connection-example",
                    "kinds": ["channel_message"],
                    "subjects": ["guild:example:channel:help"],
                }
            },
            agent_config={
                "execution_path": "persistent",
                "target_agent_id": str(agent.id),
                "limits": {"max_turns": 5, "max_total_tokens": 10000, "max_usd": 1},
            },
            timeout_seconds=120,
        ),
        account_id=test_user.account_id,
    )
    monkeypatch.setattr(
        service, "prepare_execution_routing", lambda db, flow, event: event
    )
    dispatch = AsyncMock()
    monkeypatch.setattr(service.FlowTriggerService, "_start_flow_execution", dispatch)
    args = dict(
        account_id=test_user.account_id,
        flow_id=flow.id,
        source="discord",
        connection_id="connection-example",
        event_id="message-example",
        kind="channel_message",
        subject="guild:example:channel:help",
        payload={"content": "Synthetic task"},
    )
    first = await service.ingest_employee_event(db_session, **args)
    replay = await service.ingest_employee_event(db_session, **args)
    assert first.execution_id == replay.execution_id
    assert first.status == "PENDING" and replay.duplicate
    dispatch.assert_awaited_once()
    row = dispatch.await_args.kwargs["precreated_execution"]
    assert row.trigger_event_details["employee"]["managed_agent_id"] == str(agent.id)
    assert row.webhook_delivery_key.startswith("delivery:employee:")
    with pytest.raises(service.EmployeeEventDenied):
        await service.ingest_employee_event(
            db_session, **(args | {"account_id": uuid4()})
        )
