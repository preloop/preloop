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
    app.dependency_overrides[get_db_session] = object
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


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", [False, True])
async def test_upload_consumes_stream_on_app_loop_before_worker_persistence(
    monkeypatch: pytest.MonkeyPatch, oversized: bool
) -> None:
    """Read actual ASGI chunks; never persist an oversized or partial stream."""
    from collections.abc import AsyncIterator
    from preloop.models.schemas.flow_artifact import ArtifactReference

    app = FastAPI()
    app.include_router(flow_artifacts.router)
    app.dependency_overrides[get_db_session] = object
    claims = {
        "account_id": uuid4(),
        "flow_id": uuid4(),
        "thread_id": "test",
        "kind": "workspace",
    }
    app.dependency_overrides[flow_artifacts.artifact_claims] = lambda: claims
    monkeypatch.setattr(flow_artifacts, "authorize", lambda *args: None)
    monkeypatch.setattr(
        flow_artifacts.settings, "workspace_snapshot_max_bytes", 5 if oversized else 6
    )
    origin = threading.get_ident()
    bodies: list[bytes] = []

    def persist(db: Any, **kwargs: Any) -> ArtifactReference:
        assert threading.get_ident() != origin
        bodies.append(kwargs["archive"])
        return ArtifactReference(
            artifact_id=uuid4(),
            execution_id=kwargs["execution_id"],
            manifest_sha256="a" * 64,
        )

    monkeypatch.setattr(flow_artifacts, "put_artifact", persist)

    async def chunks() -> AsyncIterator[bytes]:
        assert threading.get_ident() == origin
        yield b"abc"
        await asyncio.sleep(0)
        assert threading.get_ident() == origin
        yield b"def"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.put(
            f"/flows/executions/{uuid4()}/artifacts", content=chunks()
        )
    assert result.status_code == (413 if oversized else 200), result.text
    assert bodies == ([] if oversized else [b"abcdef"])
