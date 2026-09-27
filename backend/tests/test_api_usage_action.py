"""Usage actions for the issue routes.

``create_issue`` is only the collection POST. Nested POSTs that contain
``/issues`` must not be counted as issue creation.
"""

from typing import Any, Optional
from uuid import uuid4

import pytest
from starlette.requests import Request
from starlette.responses import Response

from preloop.api.app import ApiUsageMiddleware, api_usage_action_type
from preloop.services.api_usage_recorder import ApiUsageRecord


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("POST", "/api/v1/issues", "create_issue"),
        ("POST", "/api/v1/issues/", "create_issue"),
        ("POST", "/api/v1/issues/abc/comments", None),
        ("POST", "/api/v1/issues/abc/lifecycle/refine", None),
        ("POST", "/api/v1/issues/search", None),
        ("POST", "/api/v1/cost/by-issue", None),
        ("PUT", "/api/v1/issues/abc", "update_issue"),
        ("PATCH", "/api/v1/issues/abc", "update_issue"),
        ("DELETE", "/api/v1/issues/abc", "delete_issue"),
        ("GET", "/api/v1/issues", None),
        ("POST", "/api/v1/issues-count", None),
    ],
)
def test_api_usage_action_type_matches_the_issue_routes(
    method: str, path: str, expected: Optional[str]
) -> None:
    """Only the issue collection and item routes get an issue action."""
    assert api_usage_action_type(method, path) == expected


def _request(method: str, path: str) -> Request:
    """Build a request the usage middleware can classify."""
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer token")],
            "client": ("127.0.0.1", 123),
            "server": ("test", 80),
        }
    )


@pytest.mark.asyncio
async def test_usage_middleware_does_not_count_nested_issue_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /api/v1/issues/{id}/comments is not create_issue."""

    async def _app(scope: Any, receive: Any, send: Any) -> None:
        return None

    recorded: list[ApiUsageRecord] = []

    def _capture(record: ApiUsageRecord) -> None:
        recorded.append(record)

    class _Token:
        def __init__(self, sub: str) -> None:
            self.sub = sub

    def _decode(token: str) -> _Token:
        return _Token(str(uuid4()))

    monkeypatch.setattr("preloop.api.app.record_api_usage", _capture)
    monkeypatch.setattr("preloop.api.auth.jwt.decode_token", _decode)

    async def call_next(request: Request) -> Response:
        return Response(status_code=201)

    middleware = ApiUsageMiddleware(_app)
    await middleware.dispatch(
        _request("POST", "/api/v1/issues/abc/comments"), call_next
    )
    await middleware.dispatch(_request("POST", "/api/v1/issues"), call_next)

    assert [record.action_type for record in recorded] == [None, "create_issue"]
