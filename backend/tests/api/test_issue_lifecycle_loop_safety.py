"""Lifecycle pool waits and dispatched background work use separate loops."""

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import issue_lifecycle
from preloop.models import models
from preloop.models.db.session import get_db_session
from preloop.services.flow_trigger_service import FlowTriggerService


def lifecycle_app() -> FastAPI:
    """Mount the production routes with isolated authentication and DB fixtures."""
    app = FastAPI()
    app.include_router(issue_lifecycle.router)
    actor = models.User(id=uuid4(), account_id=uuid4(), username="loop-test")
    app.dependency_overrides[get_current_active_user] = lambda: actor
    app.dependency_overrides[get_db_session] = object

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.mark.parametrize(
    ("suffix", "body"),
    [
        ("", None),
        ("/refine", {"issue_revision": "revision"}),
        ("/ready", {"issue_revision": "revision"}),
        ("/audit/reconcile", {}),
        (
            "/pickup/reconcile",
            {
                "issue_revision": "revision",
                "previous_execution_id": str(uuid4()),
                "reason": "Approve changed scope",
            },
        ),
        (
            "/deployment/verify",
            {
                "merge_sha": "a" * 40,
                "deployed_revision": "b" * 40,
                "deployment_evidence": "https://ci.example.test/deployment/1",
                "target": "test",
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_lifecycle_database_wait_keeps_ping_responsive(
    monkeypatch: pytest.MonkeyPatch, suffix: str, body: dict[str, Any] | None
) -> None:
    """Every real route keeps a blocked scoped issue lookup off the app loop."""
    started = threading.Event()

    def blocked_lookup(*args: Any, **kwargs: Any) -> None:
        started.set()
        time.sleep(0.5)
        return None

    monkeypatch.setattr(
        issue_lifecycle.crud_issue_lifecycle, "get_issue", blocked_lookup
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=lifecycle_app()), base_url="http://test"
    ) as client:
        url = f"/issues/{uuid4()}/lifecycle{suffix}"
        request = asyncio.create_task(
            client.get(url) if body is None else client.post(url, json=body)
        )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert not request.done()
            assert (await client.get("/ping")).status_code == 200
            assert not request.done(), "lifecycle DB wait blocked the application loop"
        finally:
            response = await request
        assert response.status_code == 409


@pytest.mark.asyncio
async def test_reconcile_dispatch_survives_lifecycle_worker_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detached flow work runs on the persistent application loop after response."""
    origin = asyncio.get_running_loop()
    release = asyncio.Event()
    finished = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    flow = SimpleNamespace(id=uuid4(), is_enabled=True)
    execution = SimpleNamespace(id=uuid4(), status="PENDING", trigger_event_details={})

    async def background() -> None:
        await release.wait()
        finished.set()

    async def start(*args: Any, **kwargs: Any) -> Any:
        assert asyncio.get_running_loop() is origin
        tasks.append(asyncio.create_task(background()))
        return execution

    async def schedule(flow: Any, dispatch: Any) -> Any:
        assert asyncio.get_running_loop() is not origin
        await dispatch(execution)
        return execution

    service = SimpleNamespace(
        policy={"audit_flow_id": str(flow.id)}, schedule_audit=schedule
    )
    monkeypatch.setattr(
        issue_lifecycle, "build_lifecycle_service", AsyncMock(return_value=service)
    )
    monkeypatch.setattr(issue_lifecycle.crud_flow, "get", lambda *args, **kwargs: flow)
    monkeypatch.setattr(FlowTriggerService, "_start_flow_execution", start)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=lifecycle_app()), base_url="http://test"
        ) as client:
            response = await client.post(f"/issues/{uuid4()}/lifecycle/audit/reconcile")
        assert response.status_code == 200, response.text
        assert response.json()["execution_id"] == str(execution.id)
        assert len(tasks) == 1 and not tasks[0].done()
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
    finally:
        release.set()
        await asyncio.gather(*tasks)


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.asyncio
async def test_lifecycle_hook_database_wait_stays_off_application_loop(
    monkeypatch: pytest.MonkeyPatch, terminal: bool
) -> None:
    """Tracker and completion hooks retain the same whole-operation isolation."""
    from preloop.services import issue_lifecycle_runtime

    started = threading.Event()

    def blocked_lookup(*args: Any, **kwargs: Any) -> None:
        started.set()
        time.sleep(0.5)
        return None

    monkeypatch.setattr(
        issue_lifecycle_runtime.crud_issue_lifecycle,
        "get_issue" if terminal else "get_project",
        blocked_lookup,
    )
    flow = SimpleNamespace(
        account_id=uuid4(), agent_config={"lifecycle_kind": "merge_audit"}
    )
    if terminal:
        operation = issue_lifecycle_runtime.lifecycle_execution_finished(
            object(),
            SimpleNamespace(
                trigger_event_details={
                    "payload": {"lifecycle": {"issue_id": str(uuid4())}}
                }
            ),
            flow,
        )
    else:
        operation = issue_lifecycle_runtime.lifecycle_flow_entry(
            SimpleNamespace(db=object()), flow, {"project_id": str(uuid4())}, None
        )
    task = asyncio.create_task(operation)
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert not task.done(), "hook blocked the application loop on database work"
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        if terminal:
            with pytest.raises(ValueError, match="lifecycle_issue_not_found"):
                await task
        else:
            assert await task == (False, None)


@pytest.mark.asyncio
async def test_strict_lifecycle_local_dispatch_survives_worker_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker-disabled pickup keeps detached work on the persistent app loop."""
    from preloop.services.issue_lifecycle_worker import (
        dispatch_lifecycle_execution,
        lifecycle_worker_hook,
    )

    origin = asyncio.get_running_loop()
    release = asyncio.Event()
    finished = asyncio.Event()
    tasks: list[asyncio.Task[None]] = []
    monkeypatch.setattr(
        "preloop.services.flow_execution_dispatcher.flow_execution_worker_enabled",
        lambda: False,
    )

    async def background() -> None:
        await release.wait()
        finished.set()

    async def local_dispatch() -> None:
        assert asyncio.get_running_loop() is origin
        tasks.append(asyncio.create_task(background()))

    @lifecycle_worker_hook
    async def operation() -> None:
        await dispatch_lifecycle_execution(uuid4(), local_dispatch)

    try:
        await operation()
        assert len(tasks) == 1 and not tasks[0].done()
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
    finally:
        release.set()
        await asyncio.gather(*tasks)
