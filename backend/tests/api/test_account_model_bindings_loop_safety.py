"""The model-binding read must leave the API loop free during database waits."""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.api.endpoints import account as account_api
from preloop.models import models
from preloop.models.db.session import get_db_session


@pytest.mark.asyncio
async def test_model_binding_wait_does_not_block_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked summary lookup leaves the event loop responsive."""
    started = threading.Event()
    release = threading.Event()
    account_id = uuid4()
    agent_id = uuid4()
    actor = models.User(
        id=uuid4(), account_id=account_id, username="loop-test", is_superuser=True
    )
    app = FastAPI()
    app.include_router(account_api.router)
    app.dependency_overrides[get_current_active_user] = lambda: actor
    app.dependency_overrides[get_account_for_user] = lambda: SimpleNamespace(
        id=account_id
    )
    app.dependency_overrides[get_db_session] = object

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    def wait_for_database() -> None:
        started.set()
        # A watchdog bounds the baseline failure when its sync query blocks
        # the loop; the test releases the fixed route immediately below.
        release.wait(1.5)

    def summary_lookup(*args: object, **kwargs: object) -> dict[str, str]:
        wait_for_database()
        return {"id": str(agent_id)}

    monkeypatch.setattr(
        account_api.crud_managed_agent, "get_summary_for_account", summary_lookup
    )
    monkeypatch.setattr(
        account_api.crud_managed_agent_ai_model_binding,
        "list_for_agent",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        account_api.crud_ai_model, "get_by_account", lambda *args, **kwargs: []
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        request = asyncio.create_task(client.get(f"/agents/{agent_id}/model-bindings"))
        try:
            assert await asyncio.to_thread(started.wait, 3)
            ping_started = time.monotonic()
            assert (await client.get("/ping")).status_code == 200
            assert time.monotonic() - ping_started < 0.5
            assert not request.done(), "database wait blocked the application loop"
        finally:
            release.set()
        response = await request

    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_model_binding_visibility_receives_raw_summary_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resource authorizers keep the summary mapping they already consume."""
    agent_id = uuid4()
    account_id = uuid4()
    actor = models.User(
        id=uuid4(), account_id=account_id, username="loop-test", is_superuser=True
    )
    app = FastAPI()
    app.include_router(account_api.router)
    app.dependency_overrides[get_current_active_user] = lambda: actor
    app.dependency_overrides[get_account_for_user] = lambda: SimpleNamespace(
        id=account_id
    )
    app.dependency_overrides[get_db_session] = object

    summary_row = {
        "id": str(agent_id),
        "owner_username": "Jane Doe",
        "activity_status": "idle",
    }
    summary = Mock(return_value=summary_row)
    monkeypatch.setattr(
        account_api.crud_managed_agent, "get_summary_for_account", summary
    )
    visible = Mock(return_value=[summary_row])
    monkeypatch.setattr(account_api, "filter_viewable", visible)
    monkeypatch.setattr(
        account_api.crud_managed_agent_ai_model_binding,
        "list_for_agent",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        account_api.crud_ai_model, "get_by_account", lambda *args, **kwargs: []
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/agents/{agent_id}/model-bindings")

    assert response.status_code == 200, response.text
    assert response.json() == []
    summary.assert_called_once()
    assert visible.call_args.args[3] == [summary_row]


@pytest.mark.parametrize("visible", [False, True])
def test_model_bindings_hide_missing_or_denied_agents(
    monkeypatch: pytest.MonkeyPatch, visible: bool
) -> None:
    """The summary lookup preserves the endpoint's 404 visibility boundary."""
    account_id = uuid4()
    agent_id = uuid4()
    actor = models.User(
        id=uuid4(), account_id=account_id, username="viewer", is_superuser=True
    )
    app = FastAPI()
    app.include_router(account_api.router)
    app.dependency_overrides[get_current_active_user] = lambda: actor
    app.dependency_overrides[get_account_for_user] = lambda: SimpleNamespace(
        id=account_id
    )
    app.dependency_overrides[get_db_session] = object
    monkeypatch.setattr(
        account_api.crud_managed_agent,
        "get_summary_for_account",
        lambda *args, **kwargs: {"id": str(agent_id)} if visible else None,
    )
    monkeypatch.setattr(account_api, "filter_viewable", lambda *args: [])
    bindings = Mock(side_effect=AssertionError("hidden agent bindings were read"))
    monkeypatch.setattr(
        account_api.crud_managed_agent_ai_model_binding, "list_for_agent", bindings
    )

    from fastapi.testclient import TestClient

    response = TestClient(app).get(f"/agents/{agent_id}/model-bindings")
    assert response.status_code == 404
    bindings.assert_not_called()
