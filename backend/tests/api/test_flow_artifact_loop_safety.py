"""Artifact pool waits must leave the application's event loop responsive."""

import asyncio
import threading
import time
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from preloop.api.endpoints import flow_artifacts
from preloop.models.db.session import get_db_session


@pytest.mark.asyncio
async def test_artifact_authorization_pool_wait_does_not_block_ping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real upload route while authorization waits on a pool."""
    app = FastAPI()
    app.include_router(flow_artifacts.router)
    app.dependency_overrides[get_db_session] = lambda: object()
    app.dependency_overrides[flow_artifacts.artifact_claims] = lambda: {}
    started = threading.Event()

    def saturated_authorize(*args: Any) -> None:
        started.set()
        time.sleep(0.5)
        raise HTTPException(503, "pool exhausted")

    monkeypatch.setattr(flow_artifacts, "authorize", saturated_authorize)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        upload = asyncio.create_task(
            client.put(f"/flows/executions/{uuid4()}/artifacts", content=b"test")
        )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            assert not upload.done()
            response = await client.get("/ping")
            assert response.status_code == 200
            assert not upload.done(), "pool wait blocked the event loop"
        finally:
            result = await upload
        assert result.status_code == 503
