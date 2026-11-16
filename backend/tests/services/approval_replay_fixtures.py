"""Synthetic durable grants for replay tests of downstream behavior."""

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from preloop.services.dynamic_fastmcp import DynamicFastMCP, _approved_id_var
from preloop.services.policy_evaluator import PolicyDecision


@contextmanager
def approved_mcp_replay(
    monkeypatch: pytest.MonkeyPatch,
    mcp: DynamicFastMCP,
    *,
    account_id: str,
    tool_name: str,
    server_id: UUID,
) -> Iterator[None]:
    """Supply a valid approved request while exercising the real replay guard.

    The downstream test owns upstream/grant assertions. Persistence and current
    policy are mocked here, and route/account/status verification remains live.
    """
    request: Any = SimpleNamespace(
        id=uuid4(),
        account_id=account_id,
        status="approved",
        tool_name=tool_name,
        tool_configuration_id=uuid4(),
        approval_workflow_id=uuid4(),
        execution_id=None,
        api_key_id=None,
        managed_agent_id=None,
        runtime_session_id=None,
    )
    config = SimpleNamespace(mcp_server_id=server_id, tool_source="mcp")
    mcp._proxied_tool_servers[tool_name] = str(server_id)
    with monkeypatch.context() as patch:
        patch.setattr(
            "preloop.models.crud.approval_request.get_approval_request_async",
            AsyncMock(return_value=request),
        )
        patch.setattr(
            "preloop.models.crud.tool_configuration.get_tool_config_by_id_async",
            AsyncMock(return_value=config),
        )
        patch.setattr(
            "preloop.services.policy_evaluator.evaluate_policy_async",
            AsyncMock(return_value=PolicyDecision("allow", None, None, matched=False)),
        )
        token = _approved_id_var.set(str(request.id))
        try:
            yield
        finally:
            _approved_id_var.reset(token)
