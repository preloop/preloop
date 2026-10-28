"""The Overview transport preserves independent route authorization."""

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.endpoints.overview import overview_reads, router

PARAMS = {
    "start_date": "2026-10-01T00:00:00.000Z",
    "prior_start": "2026-09-01T00:00:00.000Z",
    "prior_end": "2026-10-01T00:00:00.000Z",
    "now": "2026-10-28T01:00:00.000Z",
}


@pytest.mark.asyncio
async def test_overview_keeps_tenant_role_and_partial_failures() -> None:
    app = FastAPI()
    active = 0
    peak = 0
    seen: list[str] = []

    async def identity(request: Request) -> str:
        token = request.headers.get("authorization", "")
        if not token.startswith("Bearer tenant-"):
            raise HTTPException(401)
        return token.removeprefix("Bearer ")

    app.dependency_overrides[get_current_active_user] = identity
    app.include_router(router, prefix="/api/v1")

    @app.get("/api/v1/{path:path}")
    async def authorized_read(
        path: str, tenant: str = Depends(identity)
    ) -> dict[str, str]:
        nonlocal active, peak
        seen.append(path)
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.001)
            if path == "users" and tenant != "tenant-admin":
                raise HTTPException(403)
            if path == "trackers":
                raise HTTPException(503)
            return {"tenant": tenant, "path": path}
        finally:
            active -= 1

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        assert (
            await client.get("/api/v1/overview/bootstrap", params=PARAMS)
        ).status_code == 401
        tenants = ("tenant-reader", "tenant-admin", "tenant-other")
        responses = await asyncio.gather(
            *(
                client.get(
                    "/api/v1/overview/bootstrap",
                    params=PARAMS,
                    headers={"Authorization": f"Bearer {tenant}"},
                )
                for tenant in tenants
            )
        )
        for tenant, response in zip(tenants, responses, strict=False):
            assert response.status_code == 200
            lines = [json.loads(line) for line in response.text.splitlines()]
            results = lines[1:]
            assert len(results) == len(lines[0]["paths"]) == 32
            for result in results:
                if result["url"].startswith("/api/v1/users?"):
                    assert result["status"] == (
                        200 if tenant == "tenant-admin" else 403
                    )
                elif result["url"] == "/api/v1/trackers":
                    assert result["status"] == 503
                else:
                    assert result["status"] == 200
                    assert json.loads(result["body"])["tenant"] == tenant
            assert response.headers["cache-control"] == "no-store"
    assert 1 < peak <= 4
    assert active == 0
    assert all("bootstrap" not in path for path in seen)


def test_overview_manifest_has_fixed_rolling_attention_windows() -> None:
    paths = overview_reads(
        PARAMS["start_date"],
        PARAMS["prior_start"],
        PARAMS["prior_end"],
        datetime(2026, 10, 28, 1, tzinfo=timezone.utc),
    )
    from urllib.parse import parse_qs, urlparse

    queries = [parse_qs(urlparse(path).query) for path in paths]
    assert any(
        query.get("start_date") == ["2026-09-28T01:00:00.000Z"]
        and query.get("breakdown") == ["models"]
        for query in queries
    )
    assert any(
        query.get("start_date") == ["2026-10-21T01:00:00.000Z"]
        and query.get("limit") == ["100"]
        for query in queries
    )
    assert len(set(paths)) == len(paths)


@pytest.mark.asyncio
async def test_disconnect_cancels_queued_and_running_reads() -> None:
    from preloop.api.endpoints.overview import stream_reads

    app = FastAPI()
    started = asyncio.Event()
    active = 0

    @app.get("/slow/{item}")
    async def slow(item: str) -> None:
        nonlocal active
        active += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    request = Request(
        {"type": "http", "app": app, "headers": [], "client": ("127.0.0.1", 1234)}
    )
    response = stream_reads(request, [f"/slow/{i}" for i in range(12)])
    iterator = response.body_iterator
    await anext(iterator)
    await asyncio.wait_for(started.wait(), timeout=1)
    await iterator.aclose()
    assert active == 0
    assert app.state.overview_read_slots._value == 4
    assert app.state.overview_pending_reads == 0


@pytest.mark.asyncio
async def test_pending_limit_returns_unavailable_without_dispatch() -> None:
    from preloop.api.endpoints.overview import stream_reads

    app = FastAPI()
    app.state.overview_pending_reads = 64
    request = Request(
        {
            "type": "http",
            "app": app,
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("localhost", 80),
            "scheme": "http",
            "path": "/",
        }
    )
    response = stream_reads(request, ["/would-not-run"])
    lines = [json.loads(line) async for line in response.body_iterator]
    assert lines[1]["status"] == 503
    assert app.state.overview_pending_reads == 64


def test_gateway_refresh_manifest_keeps_only_its_four_reads() -> None:
    paths = overview_reads(
        PARAMS["start_date"],
        PARAMS["prior_start"],
        PARAMS["prior_end"],
        datetime(2026, 10, 28, tzinfo=timezone.utc),
        "gateway",
    )
    assert len(paths) == 4
    assert all("/account/gateway-usage/" in path for path in paths)
    assert "limit=25" in paths[-1]


@pytest.mark.asyncio
async def test_stream_deadline_includes_queued_and_not_yet_started_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.api.endpoints import overview

    monkeypatch.setattr(overview, "READ_DEADLINE_SECONDS", 0.01)
    app = FastAPI()

    @app.get("/slow/{item}")
    async def slow(item: str) -> None:
        await asyncio.Event().wait()

    request = Request(
        {
            "type": "http",
            "app": app,
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("localhost", 80),
            "scheme": "http",
            "path": "/",
        }
    )
    response = overview.stream_reads(request, [f"/slow/{i}" for i in range(12)])
    lines = [json.loads(line) async for line in response.body_iterator]
    assert len(lines) == 13
    assert all(result["status"] == 503 for result in lines[1:])
    assert app.state.overview_pending_reads == 0
    assert app.state.overview_read_slots._value == 4
