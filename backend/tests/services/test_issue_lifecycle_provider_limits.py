"""Provider lookups stay bounded and stop after an idempotency marker matches."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from preloop.services.issue_lifecycle_provider import GitHubLifecycleProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow", [False, True])
async def test_exact_scan_limit_checks_exhaustion(overflow: bool) -> None:
    async def request(
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        assert params is not None
        page = params["page"]
        return [{}] * 100 if page <= 100 else ([{}] if overflow else [])

    client = SimpleNamespace(_request=AsyncMock(side_effect=request))
    provider = GitHubLifecycleProvider(client, "example/project")
    if overflow:
        with pytest.raises(ValueError, match="results_truncated"):
            await provider._pages("/rows")
    else:
        assert len(await provider._pages("/rows")) == 10000
    assert client._request.await_count == 101


@pytest.mark.asyncio
@pytest.mark.parametrize("follow_up", [False, True])
async def test_marker_lookup_stops_on_first_page(follow_up: bool) -> None:
    marker = "<!-- synthetic-marker -->"
    existing = {"id": 1, "body": marker, "html_url": "https://example.test/issue/1"}
    client = SimpleNamespace(
        _request=AsyncMock(
            side_effect=[
                [existing] + [{"body": "unrelated"}] * 99,
                existing,
            ]
        )
    )
    provider = GitHubLifecycleProvider(client, "example/project")
    if follow_up:
        assert (
            await provider.ensure_follow_up(1, marker, "title", "body")
            == existing["html_url"]
        )
        assert client._request.await_count == 1
    else:
        assert await provider.upsert_comment(1, marker, "body") == existing["html_url"]
        assert client._request.await_count == 2
        assert client._request.await_args_list[-1].args[0] == "PATCH"
