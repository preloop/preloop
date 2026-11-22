"""Reference tools accept the argument shapes weaker models send.

The calls go through FastMCP's own argument validation (``Tool.run``), which
is where an integer issue or pull request number used to be refused.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest

from preloop.api.endpoints import mcp as mcp_router
from preloop.services.initialize_mcp import initialize_mcp_with_tools
from preloop.tools.builtin_defs import PR_REFERENCE_FORMS


@pytest.fixture
def mcp_server() -> Any:
    return initialize_mcp_with_tools()


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> Dict[str, AsyncMock]:
    """Allow every call and record what reaches the router."""
    monkeypatch.setattr(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        lambda: SimpleNamespace(account_id="account"),
    )
    monkeypatch.setattr(
        "preloop.services.initialize_mcp.require_approval",
        AsyncMock(return_value=(True, None)),
    )
    mocks: Dict[str, AsyncMock] = {}
    for name in ("get_issue", "update_issue", "get_pull_request", "add_comment"):
        response = MagicMock()
        response.model_dump_json.return_value = "{}"
        mocks[name] = AsyncMock(return_value=response)
        monkeypatch.setattr(mcp_router, name, mocks[name])
    mocks["update_pull_request"] = AsyncMock(return_value=response)
    monkeypatch.setattr(mcp_router, "update_pull_request", mocks["update_pull_request"])
    return mocks


def _passed(mock: AsyncMock, key: str) -> Any:
    call = mock.await_args
    return call.kwargs[key] if key in call.kwargs else call.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,arguments,key",
    [
        ("get_issue", {"issue": 272}, "issue"),
        ("update_issue", {"issue": 272, "title": "New title"}, "issue"),
        ("get_pull_request", {"pull_request": 350}, "pull_request"),
        (
            "update_pull_request",
            {"pull_request": 350, "add_reaction": "eyes"},
            "pull_request",
        ),
        ("add_comment", {"target": 272, "comment": "Looks good"}, "target"),
    ],
)
async def test_integer_reference_reaches_router_as_string(
    mcp_server: Any,
    router: Dict[str, AsyncMock],
    name: str,
    arguments: Dict[str, Any],
    key: str,
) -> None:
    tool = await mcp_server.get_tool(name)
    result = await tool.run(arguments)
    assert not result.is_error
    passed = _passed(router[name], key)
    assert passed == str(arguments[key])
    assert isinstance(passed, str)


@pytest.mark.asyncio
async def test_pull_request_accepts_project_next_to_a_number(
    mcp_server: Any, router: Dict[str, AsyncMock]
) -> None:
    tool = await mcp_server.get_tool("get_pull_request")
    await tool.run({"pull_request": "!350", "project": "grp/proj"})
    call = router["get_pull_request"].await_args
    assert call.kwargs["pull_request"] == "!350"
    assert call.kwargs["project"] == "grp/proj"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["get_pull_request", "update_pull_request"])
async def test_pull_request_tools_document_accepted_forms(
    mcp_server: Any, name: str
) -> None:
    from preloop.api.endpoints.tools import BUILTIN_TOOLS

    tool = await mcp_server.get_tool(name)
    advertised = tool.parameters["properties"]["pull_request"]["description"]
    assert PR_REFERENCE_FORMS in advertised
    assert "group/project!12" in advertised
    entry = next(item for item in BUILTIN_TOOLS if item["name"] == name)
    catalog = entry["schema"]["properties"]
    assert PR_REFERENCE_FORMS in catalog["pull_request"]["description"]
    assert set(tool.parameters["properties"]) <= set(catalog) | {"ctx"}
    assert "project" in catalog
    if name == "get_pull_request":
        assert tool.description == entry["description"]
        assert PR_REFERENCE_FORMS in tool.description
