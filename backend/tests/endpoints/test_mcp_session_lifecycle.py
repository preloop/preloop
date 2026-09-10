"""Native MCP tools must not retain database connections between calls."""

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any, Generator
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from preloop.api.endpoints import mcp


@pytest.fixture
def tool_pool(monkeypatch: pytest.MonkeyPatch) -> Generator[Any, None, None]:
    """Use a real single-connection pool, without external database services."""
    engine = create_engine(
        "sqlite://", poolclass=QueuePool, pool_size=1, max_overflow=0, pool_timeout=0.01
    )
    sessions: list[Session] = []

    def get_db() -> Generator[Session, None, None]:
        db = Session(engine)
        sessions.append(db)  # Do not let garbage collection hide missing cleanup.
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(mcp, "get_db", get_db)
    monkeypatch.setattr(
        mcp,
        "get_http_request",
        lambda: SimpleNamespace(headers={"authorization": "Bearer test"}),
    )
    try:
        yield engine.pool
    finally:
        for db in sessions:
            db.close()
        engine.dispose()


TOOL_NAMES = (
    "get_issue",
    "create_issue",
    "update_issue",
    "search",
    "estimate_compliance",
    "improve_compliance",
    "add_comment",
    "get_pull_request",
    "update_pull_request",
    "create_pull_request",
    "update_comment",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", TOOL_NAMES)
async def test_all_native_tools_release_connection_on_auth_failure(
    tool_name: str, tool_pool: QueuePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def invalid_token(token: str, db: Session) -> None:
        db.execute(text("SELECT 1"))
        return None

    monkeypatch.setattr(mcp, "get_user_from_token_if_valid", invalid_token)
    tool = getattr(mcp, tool_name)
    arguments = {
        name: ["EX-1"] if name == "issues" else "example"
        for name, parameter in inspect.signature(tool).parameters.items()
        if parameter.default is inspect.Parameter.empty
    }
    # Repeated denied calls must not exhaust even a minimal pool.
    for _ in range(3):
        with pytest.raises(HTTPException) as error:
            await tool(**arguments)
        assert error.value.status_code == 401
        assert tool_pool.checkedout() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_concurrent_pr_reads_release_pool_before_provider_wait(
    outcome: str, tool_pool: QueuePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    count = 30
    entered = 0
    all_entered = asyncio.Event()
    release = asyncio.Event()

    async def valid_token(token: str, db: Session) -> Any:
        db.execute(text("SELECT 1"))
        return SimpleNamespace(account_id="account")

    async def get_pr(number: int) -> dict[str, Any]:
        nonlocal entered
        entered += 1
        if entered == count:
            all_entered.set()
        await release.wait()
        if outcome == "error":
            raise RuntimeError("Provider unavailable")
        return {
            "id": "1",
            "number": number,
            "title": "Example",
            "state": "open",
            "url": "https://github.com/example/repo/pull/1",
        }

    monkeypatch.setattr(mcp, "get_user_from_token_if_valid", valid_token)
    monkeypatch.setattr(
        mcp,
        "_find_pr_project",
        lambda *args, **kwargs: SimpleNamespace(
            id="project", organization_id="organization"
        ),
    )
    monkeypatch.setattr(
        mcp,
        "get_tracker_client",
        AsyncMock(
            return_value=SimpleNamespace(tracker_type="github", get_pull_request=get_pr)
        ),
    )
    tasks = [
        asyncio.create_task(
            mcp.get_pull_request("https://github.com/example/repo/pull/1")
        )
        for _ in range(count)
    ]
    try:
        await asyncio.wait_for(all_entered.wait(), timeout=2)
        assert tool_pool.checkedout() == 0
        if outcome == "cancel":
            for task in tasks:
                task.cancel()
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if outcome == "success":
            assert all(not isinstance(result, BaseException) for result in results)
        elif outcome == "error":
            assert all(
                isinstance(result, HTTPException) and result.status_code == 502
                for result in results
            )
        else:
            assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert tool_pool.checkedout() == 0
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_stage", ["connect", "call"])
async def test_external_mcp_waits_do_not_hold_database_connections(
    wait_stage: str, tool_pool: QueuePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from preloop.services import approval_helper, dynamic_fastmcp

    count = 30
    entered = 0
    all_entered = asyncio.Event()
    release = asyncio.Event()

    async def wait_at_provider() -> None:
        nonlocal entered
        entered += 1
        if entered == count:
            all_entered.set()
        await release.wait()

    async def call_tool(*args: Any) -> list[Any]:
        if wait_stage == "call":
            await wait_at_provider()
        return []

    async def get_client(**kwargs: Any) -> Any:
        if wait_stage == "connect":
            await wait_at_provider()
        return SimpleNamespace(call_tool=call_tool)

    def get_server(db: Session, **kwargs: Any) -> Any:
        db.execute(text("SELECT 1"))
        return SimpleNamespace(
            name="Example",
            url="https://example.com/mcp",
            auth_type="none",
            auth_config={},
            transport="http",
        )

    server = dynamic_fastmcp.DynamicFastMCP("pool-test")
    server.set_user_context_provider(
        lambda: SimpleNamespace(account_id="account", username="example")
    )
    monkeypatch.setattr(dynamic_fastmcp, "get_db", mcp.get_db)
    monkeypatch.setattr(dynamic_fastmcp.crud_mcp_server, "get", get_server)
    monkeypatch.setattr(
        dynamic_fastmcp,
        "get_mcp_client_pool",
        lambda: SimpleNamespace(get_client=get_client),
    )
    monkeypatch.setattr(
        approval_helper, "require_approval", AsyncMock(return_value=(True, None))
    )
    monkeypatch.setattr(server, "_halt_dispatch_denial", AsyncMock(return_value=None))
    wrapper = server._create_proxied_tool_wrapper(
        "example", "server", "account", "Example", {"properties": {}}
    )
    tasks = [asyncio.create_task(wrapper()) for _ in range(count)]
    try:
        await asyncio.wait_for(all_entered.wait(), timeout=2)
        assert tool_pool.checkedout() == 0
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert tool_pool.checkedout() == 0
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_tool_cancellation_during_auth_releases_connection(
    tool_pool: QueuePool, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()

    async def authenticate(token: str, db: Session) -> None:
        db.execute(text("SELECT 1"))
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(mcp, "get_user_from_token_if_valid", authenticate)
    task = asyncio.create_task(
        mcp.get_pull_request("https://github.com/example/repo/pull/1")
    )
    await entered.wait()
    assert tool_pool.checkedout() == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tool_pool.checkedout() == 0
