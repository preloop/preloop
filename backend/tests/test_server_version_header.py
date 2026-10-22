"""The REST release header is diagnostic, including on failure responses."""

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send

from preloop.api.app import create_app
from preloop.config import SERVER_VERSION
from preloop.models.db.session import get_db_session

HEADER = "Preloop-Server-Version"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Use the real middleware/handlers without starting external services."""
    monkeypatch.setenv("PRELOOP_SERVICE_ROLE", "api")
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: MagicMock()

    @app.get("/api/v1/_diagnostics/success")
    async def success() -> dict[str, str]:
        return {"result": "ok"}

    @app.get("/api/v1/_diagnostics/error/{status}")
    async def error(status: int) -> None:
        raise HTTPException(status, "example failure")

    @app.get("/api/v1/_diagnostics/unhandled")
    async def unhandled() -> None:
        raise RuntimeError("example failure")

    @app.get("/api/v1/_diagnostics/stream")
    async def stream() -> StreamingResponse:
        return StreamingResponse(iter([b"first", b"second"]))

    class EarlyResponseMiddleware:
        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http" and scope["path"].endswith("/early"):
                from starlette.responses import Response

                await Response("denied", status_code=403)(scope, receive, send)
                return
            await self.app(scope, receive, send)

    app.add_middleware(EarlyResponseMiddleware)
    # No context manager: lifespan starts database and event-bus resources.
    test_client = TestClient(app, raise_server_exceptions=False)
    yield test_client
    test_client.close()


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/api/v1/_diagnostics/success", 200),
        ("GET", "/api/v1/_diagnostics/stream", 200),
        ("GET", "/api/v1/_diagnostics/error/401", 401),
        ("GET", "/api/v1/_diagnostics/error/403", 403),
        ("GET", "/api/v1/_diagnostics/error/429", 429),
        ("GET", "/api/v1/_diagnostics/missing", 404),
        ("POST", "/api/v1/_diagnostics/success", 405),
        ("GET", "/api/v1/_diagnostics/error/invalid", 422),
        ("GET", "/api/v1/_diagnostics/unhandled", 500),
        ("GET", "/api/v1/_diagnostics/early", 403),
        ("GET", "/api/v1/_diagnostics/success/", 307),
    ],
)
def test_release_header_on_rest_responses(
    client: TestClient, method: str, path: str, status: int
) -> None:
    response = client.request(method, path, follow_redirects=False)
    assert response.status_code == status
    assert response.headers.get_list(HEADER) == [SERVER_VERSION]
    if path.endswith("/stream"):
        assert response.content == b"firstsecond"


def test_version_endpoint_matches_header(client: TestClient) -> None:
    response = client.get("/api/v1/version")
    assert response.status_code == 200
    assert response.headers.get(HEADER) == response.json()["server_version"]


@pytest.mark.parametrize("requested", ["1.0.0", "2", "2099-01-01", "invalid"])
@pytest.mark.parametrize(
    "path", ["/api/v1/_diagnostics/success", "/api/v1/_diagnostics/error/403"]
)
def test_request_header_does_not_select_behavior(
    client: TestClient, requested: str, path: str
) -> None:
    baseline = client.get(path)
    response = client.get(path, headers={HEADER: requested})
    assert response.status_code == baseline.status_code
    assert response.content == baseline.content
    assert response.headers.get(HEADER) == SERVER_VERSION


@pytest.mark.parametrize(
    "path",
    [
        "/api/v10/missing",
        "/api/v2/missing",
        "/oauth/missing",
        "/mcp/missing",
        "/openai/v1/missing",
        "/anthropic/v1/missing",
        "/gemini/v1beta/missing",
    ],
)
def test_unrelated_surfaces_have_no_rest_header(client: TestClient, path: str) -> None:
    assert HEADER not in client.get(path, follow_redirects=False).headers


def test_cors_preflight_has_release_header(client: TestClient) -> None:
    response = client.options(
        "/api/v1/_diagnostics/success",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert response.headers.get(HEADER) == SERVER_VERSION


@pytest.mark.parametrize("role", ["all", "api", "gateway", "chat"])
def test_header_on_every_service_role(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    monkeypatch.setenv("PRELOOP_SERVICE_ROLE", role)
    client = TestClient(create_app(), raise_server_exceptions=False)
    try:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.headers.get(HEADER) == SERVER_VERSION
    finally:
        client.close()


@pytest.mark.parametrize("path", ["/api/v1", "/api/v1/", "/api/v1/health"])
def test_header_with_deployment_root_path(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setenv("PRELOOP_SERVICE_ROLE", "api")
    client = TestClient(create_app(), root_path="/preloop")
    try:
        response = client.get("/preloop" + path, follow_redirects=False)
        assert response.headers.get(HEADER) == SERVER_VERSION
    finally:
        client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_type", ["websocket", "lifespan"])
async def test_non_http_messages_pass_through(scope_type: str) -> None:
    from unittest.mock import AsyncMock

    from preloop.api.middleware.server_version import ServerVersionMiddleware

    app = AsyncMock()
    receive, send = AsyncMock(), AsyncMock()
    scope = {"type": scope_type, "path": "/api/v1/ws"}
    await ServerVersionMiddleware(app)(scope, receive, send)
    app.assert_awaited_once_with(scope, receive, send)


def test_unhandled_exception_still_propagates(client: TestClient) -> None:
    """The header wrapper must not swallow errors used by server error logs."""
    raising_client = TestClient(client.app, raise_server_exceptions=True)
    try:
        with pytest.raises(RuntimeError, match="example failure"):
            raising_client.get("/api/v1/_diagnostics/unhandled")
    finally:
        raising_client.close()
