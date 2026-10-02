"""Synthetic employee intake regressions; no network or telemetry."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from preloop.services import employee_events as service
from preloop.integrations.discord_employee_bridge import normalize_discord_event


@pytest.fixture
def intake(monkeypatch):
    account, agent_id, flow_id = uuid4(), uuid4(), uuid4()
    flow = SimpleNamespace(
        id=flow_id,
        account_id=account,
        is_enabled=True,
        timeout_seconds=120,
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
            "target_agent_id": str(agent_id),
            "limits": {
                "max_turns": 5,
                "max_total_tokens": 10000,
                "max_usd": 1,
            },
        },
    )
    agent = SimpleNamespace(id=agent_id, lifecycle_state="active")
    monkeypatch.setattr(
        service.crud_flow,
        "get",
        lambda db, **kw: flow if kw["account_id"] == str(account) else None,
    )
    monkeypatch.setattr(
        service.crud_managed_agent, "get_for_account", lambda *a, **kw: agent
    )
    monkeypatch.setattr(service, "flows_halted", lambda *args: False)
    monkeypatch.setattr(
        service, "prepare_execution_routing", lambda db, flow, event: event
    )
    rows = {}

    def reserve(db, **kwargs):
        key = kwargs["delivery_key"]
        if key in rows:
            return rows[key], True
        row = SimpleNamespace(
            id=uuid4(), status="PENDING", trigger_event_details=kwargs["event"]
        )
        rows[key] = row
        return row, False

    monkeypatch.setattr(service.crud_flow_execution, "reserve_employee_event", reserve)
    dispatch = AsyncMock()
    monkeypatch.setattr(
        service,
        "FlowTriggerService",
        lambda db: SimpleNamespace(_start_flow_execution=dispatch),
    )
    args = dict(
        account_id=account,
        flow_id=flow_id,
        source="discord",
        connection_id="connection-example",
        event_id="message-example",
        kind="channel_message",
        subject="guild:example:channel:help",
        payload={"content": "Help with this issue"},
    )
    return MagicMock(), args, flow, rows, dispatch


@pytest.mark.asyncio
async def test_replay_returns_same_durable_execution_without_dispatch(intake):
    db, args, _, rows, dispatch = intake
    first = await service.ingest_employee_event(db, **args)
    replay = await service.ingest_employee_event(db, **args)
    assert first.execution_id == replay.execution_id
    assert first.status == "PENDING" and not first.duplicate and replay.duplicate
    assert len(rows) == 1
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"account_id": uuid4()},
        {"connection_id": "forged-connection"},
        {"subject": "guild:other:channel:private"},
        {"source": "slack"},
        {"kind": "member_joined"},
        {"payload": {"bot": True}},
        {"payload": {"content": "x" * 65537}},
        {"occurred_at": datetime(2026, 1, 1)},
    ],
)
async def test_tenant_source_scope_limits_and_loop_denials(intake, override):
    db, args, _, rows, dispatch = intake
    with pytest.raises(service.EmployeeEventDenied):
        await service.ingest_employee_event(db, **(args | override))
    assert not rows
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_revocation_rechecked_on_duplicate(intake):
    db, args, flow, _, dispatch = intake
    await service.ingest_employee_event(db, **args)
    flow.is_enabled = False
    with pytest.raises(service.EmployeeEventDenied):
        await service.ingest_employee_event(db, **args)
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_unbounded_employee_refused(intake):
    db, args, flow, rows, _ = intake
    flow.agent_config["limits"].pop("max_usd")
    with pytest.raises(service.EmployeeEventDenied, match="limits"):
        await service.ingest_employee_event(db, **args)
    assert not rows


@pytest.mark.asyncio
async def test_dispatch_failure_keeps_receipt_for_existing_recovery(intake):
    db, args, _, rows, dispatch = intake
    dispatch.side_effect = RuntimeError("worker transport unavailable")
    with pytest.raises(RuntimeError):
        await service.ingest_employee_event(db, **args)
    assert len(rows) == 1
    dispatch.side_effect = None
    receipt = await service.ingest_employee_event(db, **args)
    assert receipt.duplicate and receipt.status == "PENDING"
    dispatch.assert_awaited_once()


def test_task_identity_isolated_by_tenant_employee_and_subject():
    key = service.employee_task_key(
        "account", "employee", "discord", "connection", "subject"
    )
    assert key == service.employee_task_key(
        "account", "employee", "discord", "connection", "subject"
    )
    for args in [
        ("other", "employee", "discord", "connection", "subject"),
        ("account", "other", "discord", "connection", "subject"),
        ("account", "employee", "discord", "connection", "other"),
    ]:
        assert key != service.employee_task_key(*args)


def test_discord_bridge_selected_channels_and_loop_prevention():
    args = dict(guild_id="guild-example", channel_ids=frozenset({"help"}))
    message = {
        "guild_id": "guild-example",
        "channel_id": "help",
        "id": "message-example",
        "author": {"id": "user-example"},
        "content": "hello",
    }
    event = normalize_discord_event("MESSAGE_CREATE", message, **args)
    assert event["event_id"] == "message-example"
    for override in [
        {"guild_id": "other"},
        {"channel_id": "private"},
        {"author": {"bot": True}},
        {"webhook_id": "webhook-example"},
    ]:
        assert (
            normalize_discord_event("MESSAGE_CREATE", message | override, **args)
            is None
        )
    join = normalize_discord_event(
        "GUILD_MEMBER_ADD",
        {
            "guild_id": "guild-example",
            "user": {"id": "user-example"},
            "joined_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        },
        **args,
    )
    assert (
        join["kind"] == "member_joined"
        and join["subject"] == "guild:guild-example:members"
    )
