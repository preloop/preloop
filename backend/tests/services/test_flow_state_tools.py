"""Flow state: CRUD rules and the get/put tools over the registered MCP server."""

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastmcp import Client
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from preloop.models import models
from preloop.models.crud import crud_api_key
from preloop.models.crud import flow_state as crud_flow_state
from preloop.services import flow_state_tools
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.initialize_mcp import initialize_mcp_with_tools

NAMES = ["get_flow_state", "put_flow_state"]


def _flow(db: Session, account_id: Any, name: str = "Ledger") -> models.Flow:
    flow = models.Flow(
        name=name,
        account_id=account_id,
        trigger_event_source="schedule",
        schedule_config={"type": "interval", "every": 1, "unit": "hours"},
        prompt_template="Keep a ledger",
        agent_type="openhands",
        agent_config={},
        allowed_mcp_tools=[{"name": n} for n in NAMES],
    )
    db.add(flow)
    db.flush()
    return flow


class TestCrud:
    def test_versions_and_compare_and_set(
        self, db_session: Session, test_user: models.User
    ) -> None:
        flow = _flow(db_session, test_user.account_id)
        scope = {"account_id": test_user.account_id, "flow_id": flow.id}
        row = crud_flow_state.put_state(
            db_session, **scope, key="ledger", value={"a": 1}, expected_version=0
        )
        assert (row.version, row.size_bytes) == (1, len('{"a":1}'))
        with pytest.raises(crud_flow_state.FlowStateError) as exc:
            crud_flow_state.put_state(
                db_session, **scope, key="ledger", value={}, expected_version=0
            )
        assert exc.value.code == "flow_state_version_conflict"
        row = crud_flow_state.put_state(
            db_session, **scope, key="ledger", value={"a": 2}, expected_version=1
        )
        assert row.version == 2
        with pytest.raises(crud_flow_state.FlowStateError):
            crud_flow_state.put_state(
                db_session, **scope, key="ledger", value={}, expected_version=1
            )
        row = crud_flow_state.put_state(db_session, **scope, key="ledger", value=[])
        assert (row.version, row.value) == (3, [])
        assert crud_flow_state.delete_state(db_session, **scope, key="ledger")
        assert crud_flow_state.get_state(db_session, **scope, key="ledger") is None

    def test_limits_and_account_scope(
        self, db_session: Session, test_user: models.User
    ) -> None:
        flow = _flow(db_session, test_user.account_id)
        scope = {"account_id": test_user.account_id, "flow_id": flow.id}
        for bad in ("", "Upper", ".lead", "x" * 129, "a/b"):
            with pytest.raises(crud_flow_state.FlowStateError):
                crud_flow_state.put_state(db_session, **scope, key=bad, value=1)
        big = "x" * crud_flow_state.MAX_VALUE_BYTES
        with pytest.raises(crud_flow_state.FlowStateError) as exc:
            crud_flow_state.put_state(db_session, **scope, key="big", value=big)
        assert exc.value.code == "flow_state_value_too_large"
        with pytest.raises(crud_flow_state.FlowStateError):
            crud_flow_state.put_state(
                db_session, **scope, key="nan", value=float("nan")
            )
        for i in range(crud_flow_state.MAX_KEYS_PER_FLOW):
            crud_flow_state.put_state(db_session, **scope, key=f"k{i}", value=i)
        with pytest.raises(crud_flow_state.FlowStateError) as exc:
            crud_flow_state.put_state(db_session, **scope, key="one-more", value=1)
        assert exc.value.code == "flow_state_too_many_keys"
        # Overwriting an existing key is still allowed at the cap.
        crud_flow_state.put_state(db_session, **scope, key="k0", value="again")
        other = models.Account(organization_name="Other")
        db_session.add(other)
        db_session.flush()
        with pytest.raises(crud_flow_state.FlowStateError) as exc:
            crud_flow_state.put_state(
                db_session, account_id=other.id, flow_id=flow.id, key="x", value=1
            )
        assert exc.value.code == "flow_state_flow_not_found"
        assert (
            crud_flow_state.get_state(
                db_session, account_id=other.id, flow_id=flow.id, key="k0"
            )
            is None
        )


class TestCrudConcurrency:
    def test_locked_update_replaces_a_stale_pre_read(
        self, db_session: Session, test_user: models.User
    ) -> None:
        """A write committed between the pre-read and the lock still advances
        the version, so a later compare-and-set cannot match a reused one."""
        from sqlalchemy import update

        flow = _flow(db_session, test_user.account_id)
        scope = {"account_id": test_user.account_id, "flow_id": flow.id}
        crud_flow_state.put_state(db_session, **scope, key="k", value=1)
        cached = crud_flow_state.get_state(db_session, **scope, key="k")
        assert cached is not None and cached.version == 1
        # Another writer's commit, invisible to the identity map.
        db_session.execute(
            update(models.FlowState)
            .where(models.FlowState.id == cached.id)
            .values(version=5, value=2)
            .execution_options(synchronize_session=False)
        )
        row = crud_flow_state.put_state(db_session, **scope, key="k", value=3)
        assert row.version == 6
        assert row.value == 3

    def test_key_creation_locks_the_flow_and_listing_skips_values(
        self, db_session: Session, test_user: models.User
    ) -> None:
        from sqlalchemy import event, inspect

        flow = _flow(db_session, test_user.account_id)
        scope = {"account_id": test_user.account_id, "flow_id": flow.id}
        statements: list[str] = []

        def record(conn, cursor, statement, *args):  # type: ignore[no-untyped-def]
            statements.append(" ".join(statement.split()))

        engine = db_session.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            crud_flow_state.put_state(db_session, **scope, key="new", value=[1] * 50)
            created = list(statements)
            statements.clear()
            crud_flow_state.put_state(db_session, **scope, key="new", value=[2])
            updated = list(statements)
            statements.clear()
            db_session.expunge_all()
            rows = crud_flow_state.list_states(db_session, **scope)
            listing = list(statements)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert any("FROM flow" in q and "FOR UPDATE" in q for q in created)
        # Replacing an existing key does not take the flow lock.
        assert not any("FROM flow " in q and "FOR UPDATE" in q for q in updated)
        assert "value" in inspect(rows[0]).unloaded
        assert all("flow_state.value" not in q for q in listing)


@dataclass
class Scenario:
    client: Client
    actor: UserContext
    flow: models.Flow
    execution: models.FlowExecution
    policy: AsyncMock

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.client.call_tool(name, arguments)
        return result.structured_content


@pytest_asyncio.fixture
async def scope(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> Any:
    policy_engine = create_async_engine(
        db_session.get_bind().engine.url.set(drivername="postgresql+asyncpg"),
        poolclass=NullPool,
    )
    monkeypatch.setattr(
        "preloop.models.db.session.get_async_session_factory",
        lambda: async_sessionmaker(policy_engine),
    )
    flow = _flow(db_session, test_user.account_id)
    run = models.FlowExecution(flow_id=flow.id, status="RUNNING")
    db_session.add(run)
    db_session.flush()
    key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="Flow run",
        account_id=test_user.account_id,
        user_id=test_user.id,
        scopes=["mcp:read", "mcp:write"],
        commit=False,
        context_data={
            "flow_execution_id": str(run.id),
            "flow_id": str(flow.id),
            "allowed_mcp_tools": flow.allowed_mcp_tools,
        },
    )
    actor = UserContext(
        api_key_id=str(key.id),
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        username=test_user.username,
        has_tracker=False,
        flow_id=str(flow.id),
        flow_execution_id=str(run.id),
        runtime_principal_type="flow_execution",
        runtime_principal_id=str(run.id),
        allowed_flow_tools=list(NAMES),
    )
    server = initialize_mcp_with_tools()
    server.set_user_context_provider(lambda: actor)
    monkeypatch.setattr(flow_state_tools, "get_current_user_context", lambda: actor)
    monkeypatch.setattr(
        flow_state_tools,
        "get_session_factory",
        lambda: lambda: nullcontext(db_session),
    )
    policy = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(flow_state_tools, "require_approval", policy)
    monkeypatch.setattr(server, "_persist_tool_call_activity", lambda *a, **kw: None)
    async with Client(server) as client:
        yield Scenario(client, actor, flow, run, policy)


@pytest.mark.asyncio
async def test_round_trip_over_the_wire(scope: Scenario) -> None:
    tools = {t.name for t in await scope.client.list_tools()}
    assert set(NAMES) <= tools
    missing = await scope.call("get_flow_state", {"key": "ledger"})
    assert (missing["value"], missing["version"]) == (None, 0)
    stored = await scope.call(
        "put_flow_state",
        {"key": "ledger", "value": {"seen": ["a"]}, "expected_version": 0},
    )
    assert stored["version"] == 1
    assert stored["updated_by_execution_id"] == str(scope.execution.id)
    read = await scope.call("get_flow_state", {"key": "ledger"})
    assert read["value"] == {"seen": ["a"]}
    listing = await scope.call("get_flow_state", {})
    assert [k["key"] for k in listing["keys"]] == ["ledger"]
    assert listing["keys"][0]["value"] is None
    with pytest.raises(ToolError, match="version_conflict"):
        await scope.call(
            "put_flow_state", {"key": "ledger", "value": 1, "expected_version": 0}
        )


@pytest.mark.asyncio
async def test_reserved_keys_policy_and_binding(
    scope: Scenario, db_session: Session
) -> None:
    with pytest.raises(ToolError, match="reserved"):
        await scope.call("put_flow_state", {"key": "preloop.ci_health", "value": 1})
    scope.policy.return_value = (False, "Denied by tool policy")
    with pytest.raises(ToolError, match="Denied"):
        await scope.call("put_flow_state", {"key": "ledger", "value": 1})
    scope.policy.return_value = (True, None)
    # The flow's current allow-list is re-read: revoking the tool revokes it.
    scope.flow.allowed_mcp_tools = [{"name": "get_flow_state"}]
    db_session.flush()
    with pytest.raises(ToolError, match="credential"):
        await scope.call("put_flow_state", {"key": "ledger", "value": 1})
    scope.actor.flow_execution_id = None
    with pytest.raises(ToolError, match="credential"):
        await scope.call("get_flow_state", {})


@pytest.mark.asyncio
async def test_policy_sees_and_replays_the_full_write(
    scope: Scenario, db_session: Session
) -> None:
    """The approval record holds the value, so a parked approval that replays
    the stored arguments writes what was approved."""
    await scope.call("put_flow_state", {"key": "ledger", "value": {"n": 1}})
    recorded = scope.policy.call_args.kwargs["arguments"]
    assert recorded == {"key": "ledger", "value": {"n": 1}, "expected_version": None}
    replayed = {**recorded, "value": {"n": 2}, "expected_version": 1}
    await scope.call("put_flow_state", replayed)
    stored = await scope.call("get_flow_state", {"key": "ledger"})
    assert (stored["value"], stored["version"]) == ({"n": 2}, 2)


def test_put_flow_state_is_not_advertised_as_idempotent() -> None:
    from preloop.tools.builtin_defs import BUILTIN_TOOL_ANNOTATIONS

    hints = BUILTIN_TOOL_ANNOTATIONS["put_flow_state"]
    assert hints["idempotentHint"] is False
    assert hints["destructiveHint"] is True


class TestRest:
    def test_owner_routes(
        self, client: Any, db_session: Session, test_user: models.User
    ) -> None:
        flow = _flow(db_session, test_user.account_id)
        base = f"/api/v1/flows/{flow.id}/state"
        put = client.put(f"{base}/notes", json={"value": {"x": 1}})
        assert put.status_code == 200, put.text
        assert put.json()["version"] == 1
        assert client.get(f"{base}/notes").json()["value"] == {"x": 1}
        conflict = client.put(f"{base}/notes", json={"value": 2, "expected_version": 5})
        assert conflict.status_code == 409
        assert client.put(f"{base}/preloop.x", json={"value": 1}).status_code == 403
        crud_flow_state.put_state(
            db_session,
            account_id=test_user.account_id,
            flow_id=flow.id,
            key="preloop.x",
            value={"platform": True},
        )
        listing = client.get(base).json()
        assert {(k["key"], k["reserved"]) for k in listing["keys"]} == {
            ("notes", False),
            ("preloop.x", True),
        }
        assert client.delete(f"{base}/preloop.x").status_code == 204
        assert client.get(f"{base}/preloop.x").status_code == 404
        assert client.get(f"{base}/Bad Key").status_code == 422
