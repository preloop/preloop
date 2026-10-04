"""Regression tests for the MCP tool response schema."""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from preloop.models.schemas.mcp_tool import MCPToolResponse


def _tool_row() -> SimpleNamespace:
    """Build a stand-in for an ORM ``MCPTool`` row with UUID identifiers."""
    return SimpleNamespace(
        id=uuid4(),
        mcp_server_id=uuid4(),
        name="create_issue",
        description="Create an issue",
        input_schema={"type": "object", "properties": {}},
        discovered_at="2026-01-01T00:00:00Z",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def test_mcp_tool_response_accepts_uuid_identifiers():
    """UUID columns must be valid input, not a validation error."""
    tool = _tool_row()

    response = MCPToolResponse.model_validate(tool)

    assert response.id == tool.id
    assert response.mcp_server_id == tool.mcp_server_id


def test_mcp_tool_response_serializes_uuid_identifiers_as_strings():
    """UUID identifiers must serialize to JSON strings."""
    tool = _tool_row()

    response = MCPToolResponse.model_validate(tool)

    for mode in ("python", "json"):
        dumped = response.model_dump(mode=mode)
        assert isinstance(dumped["id"], str)
        assert isinstance(dumped["mcp_server_id"], str)
        assert dumped["id"] == str(tool.id)
        assert dumped["mcp_server_id"] == str(tool.mcp_server_id)


def test_mcp_tool_response_accepts_already_string_identifiers():
    """Pre-serialized string ids must round-trip as strings.

    Tool rows can reach the schema with identifiers that are already strings
    (for example cache payloads decoded from JSON). Pydantic must coerce them
    to ``UUID`` and the response must still emit strings, so the wire format
    does not depend on how the row was materialized.
    """
    tool = _tool_row()
    tool.id = str(tool.id)
    tool.mcp_server_id = str(tool.mcp_server_id)

    response = MCPToolResponse.model_validate(tool)

    assert str(response.id) == tool.id
    assert str(response.mcp_server_id) == tool.mcp_server_id

    dumped = response.model_dump(mode="json")
    assert dumped["id"] == tool.id
    assert dumped["mcp_server_id"] == tool.mcp_server_id
