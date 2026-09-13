"""Pure schema/endpoint wiring checks for the pre-tool central rule gate."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from preloop.api.endpoints import agent_permission as endpoint


def test_permission_phase_is_explicit_and_validated() -> None:
    legacy = endpoint.AgentPermissionCheckRequest(tool_name="Bash")
    assert legacy.evaluation_phase == "permission_request"
    early = endpoint.AgentPermissionCheckRequest(
        tool_name="Bash", evaluation_phase="pre_tool_use"
    )
    assert early.client_decision is None
    with pytest.raises(ValidationError):
        endpoint.AgentPermissionCheckRequest(
            tool_name="Bash", evaluation_phase="skip_rules"
        )


@pytest.mark.asyncio
async def test_endpoint_forwards_pre_tool_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = endpoint.PermissionIdentity(
        account_id=str(uuid4()),
        user_id=uuid4(),
        api_key_id=uuid4(),
        managed_agent_id=uuid4(),
        runtime_session_id=None,
        managed_agent_name="Synthetic Codex agent",
    )
    monkeypatch.setattr(
        endpoint, "run_db_off_loop", AsyncMock(side_effect=[identity, None])
    )
    monkeypatch.setattr(
        endpoint, "_permission_check_base_url", lambda: "https://example.com"
    )
    service = AsyncMock(return_value=("allow", "No native rule", None, False))
    monkeypatch.setattr(endpoint, "request_agent_permission", service)
    response = await endpoint.agent_permission_check(
        endpoint.AgentPermissionCheckRequest(
            tool_name="Bash", source="codex_cli", evaluation_phase="pre_tool_use"
        ),
        authorization="Bearer synthetic-token",
    )
    assert response.decision == "allow"
    assert service.await_args.kwargs["evaluation_phase"] == "pre_tool_use"
    assert service.await_args.kwargs["client_decision"] is None
