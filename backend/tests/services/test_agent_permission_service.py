"""Tests for onboarded-agent native tool permission checks."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.services import agent_permission_service as svc

pytestmark = pytest.mark.asyncio


def _request_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "base_url": "https://app.test",
        "account_id": "acct-1",
        "user_id": uuid.uuid4(),
        "managed_agent_id": uuid.uuid4(),
        "runtime_session_id": uuid.uuid4(),
        "managed_agent_name": "Test Agent",
        "source": "claude_code",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "agent_reasoning": "Need to list files",
        "client_decision": None,
    }
    base.update(overrides)
    return base


class TestClientPolicyShortCircuit:
    """Client-side allow/deny should bypass the approval pipeline."""

    async def test_honors_client_allow(self) -> None:
        decision, reason, request_id = await svc.request_agent_permission(
            **_request_kwargs(client_decision="allow")
        )
        assert decision == "allow"
        assert reason == ""
        assert request_id is None

    async def test_honors_client_deny(self) -> None:
        decision, reason, request_id = await svc.request_agent_permission(
            **_request_kwargs(client_decision="deny")
        )
        assert decision == "deny"
        assert reason == "Denied by client policy"
        assert request_id is None


def _approval_setup(*, status: str = "pending", timeout_seconds: int = 4):
    workflow = MagicMock()
    workflow.id = uuid.uuid4()
    workflow.timeout_seconds = timeout_seconds

    config = MagicMock()
    config.id = uuid.uuid4()

    approval = MagicMock()
    approval.id = uuid.uuid4()
    approval.status = status
    approval.approver_comment = None

    return workflow, config, approval


@asynccontextmanager
async def _session_cm(db: AsyncMock):
    yield db


class TestApprovalPipeline:
    """Human approval path via ApprovalService."""

    @patch("preloop.services.approval_service.ApprovalService")
    @patch("preloop.services.agent_permission_service.get_async_db_session")
    @patch.object(svc, "_resolve_tool_config", new_callable=AsyncMock)
    @patch.object(svc, "_resolve_workflow", new_callable=AsyncMock)
    async def test_immediate_approval_skips_polling(
        self,
        mock_resolve_workflow: AsyncMock,
        mock_resolve_tool_config: AsyncMock,
        mock_get_session: MagicMock,
        mock_service_cls: MagicMock,
    ) -> None:
        workflow, config, approval = _approval_setup(status="approved")
        approval.approver_comment = "Looks fine"
        mock_resolve_workflow.return_value = workflow
        mock_resolve_tool_config.return_value = config
        mock_get_session.side_effect = lambda: _session_cm(AsyncMock())
        mock_service_cls.return_value.create_and_notify = AsyncMock(
            return_value=approval
        )

        decision, reason, request_id = await svc.request_agent_permission(
            **_request_kwargs()
        )

        assert decision == "allow"
        assert reason == "Looks fine"
        assert request_id == str(approval.id)
        mock_service_cls.return_value.create_and_notify.assert_awaited_once()

    @patch("preloop.services.approval_service.ApprovalService")
    @patch("preloop.services.agent_permission_service.get_async_db_session")
    @patch.object(svc, "_resolve_tool_config", new_callable=AsyncMock)
    @patch.object(svc, "_resolve_workflow", new_callable=AsyncMock)
    async def test_immediate_decline_skips_polling(
        self,
        mock_resolve_workflow: AsyncMock,
        mock_resolve_tool_config: AsyncMock,
        mock_get_session: MagicMock,
        mock_service_cls: MagicMock,
    ) -> None:
        workflow, config, approval = _approval_setup(status="declined")
        mock_resolve_workflow.return_value = workflow
        mock_resolve_tool_config.return_value = config
        mock_get_session.side_effect = lambda: _session_cm(AsyncMock())
        mock_service_cls.return_value.create_and_notify = AsyncMock(
            return_value=approval
        )

        decision, reason, request_id = await svc.request_agent_permission(
            **_request_kwargs()
        )

        assert decision == "deny"
        assert "declined" in reason
        assert request_id == str(approval.id)

    @patch(
        "preloop.models.crud.approval_request.get_approval_request_async",
        new_callable=AsyncMock,
    )
    @patch(
        "preloop.services.agent_permission_service.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("preloop.services.approval_service.ApprovalService")
    @patch("preloop.services.agent_permission_service.get_async_db_session")
    @patch.object(svc, "_resolve_tool_config", new_callable=AsyncMock)
    @patch.object(svc, "_resolve_workflow", new_callable=AsyncMock)
    async def test_poll_until_approved(
        self,
        mock_resolve_workflow: AsyncMock,
        mock_resolve_tool_config: AsyncMock,
        mock_get_session: MagicMock,
        mock_service_cls: MagicMock,
        mock_sleep: AsyncMock,
        mock_get_request: AsyncMock,
    ) -> None:
        workflow, config, approval = _approval_setup(status="pending")
        mock_resolve_workflow.return_value = workflow
        mock_resolve_tool_config.return_value = config
        mock_get_session.side_effect = lambda: _session_cm(AsyncMock())
        mock_service_cls.return_value.create_and_notify = AsyncMock(
            return_value=approval
        )

        polled = MagicMock()
        polled.status = "approved"
        polled.approver_comment = "Ship it"
        mock_get_request.return_value = polled

        decision, reason, request_id = await svc.request_agent_permission(
            **_request_kwargs()
        )

        assert decision == "allow"
        assert reason == "Ship it"
        assert request_id == str(approval.id)
        mock_get_request.assert_awaited()
        mock_sleep.assert_awaited()

    @patch(
        "preloop.models.crud.approval_request.get_approval_request_async",
        new_callable=AsyncMock,
    )
    @patch(
        "preloop.services.agent_permission_service.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("preloop.services.approval_service.ApprovalService")
    @patch("preloop.services.agent_permission_service.get_async_db_session")
    @patch.object(svc, "_resolve_tool_config", new_callable=AsyncMock)
    @patch.object(svc, "_resolve_workflow", new_callable=AsyncMock)
    async def test_poll_times_out(
        self,
        mock_resolve_workflow: AsyncMock,
        mock_resolve_tool_config: AsyncMock,
        mock_get_session: MagicMock,
        mock_service_cls: MagicMock,
        mock_sleep: AsyncMock,
        mock_get_request: AsyncMock,
    ) -> None:
        workflow, config, approval = _approval_setup(
            status="pending", timeout_seconds=0
        )
        mock_resolve_workflow.return_value = workflow
        mock_resolve_tool_config.return_value = config
        mock_get_session.side_effect = lambda: _session_cm(AsyncMock())
        mock_service_cls.return_value.create_and_notify = AsyncMock(
            return_value=approval
        )

        pending = MagicMock()
        pending.status = "pending"
        pending.approver_comment = None
        mock_get_request.return_value = pending

        decision, reason, request_id = await svc.request_agent_permission(
            **_request_kwargs()
        )

        assert decision == "deny"
        assert reason == "Approval request timed out"
        assert request_id == str(approval.id)

    @patch(
        "preloop.models.crud.approval_request.get_approval_request_async",
        new_callable=AsyncMock,
    )
    @patch(
        "preloop.services.agent_permission_service.asyncio.sleep",
        new_callable=AsyncMock,
    )
    @patch("preloop.services.approval_service.ApprovalService")
    @patch("preloop.services.agent_permission_service.get_async_db_session")
    @patch.object(svc, "_resolve_tool_config", new_callable=AsyncMock)
    @patch.object(svc, "_resolve_workflow", new_callable=AsyncMock)
    async def test_poll_missing_request_denies(
        self,
        mock_resolve_workflow: AsyncMock,
        mock_resolve_tool_config: AsyncMock,
        mock_get_session: MagicMock,
        mock_service_cls: MagicMock,
        mock_sleep: AsyncMock,
        mock_get_request: AsyncMock,
    ) -> None:
        workflow, config, approval = _approval_setup(status="pending")
        mock_resolve_workflow.return_value = workflow
        mock_resolve_tool_config.return_value = config
        mock_get_session.side_effect = lambda: _session_cm(AsyncMock())
        mock_service_cls.return_value.create_and_notify = AsyncMock(
            return_value=approval
        )
        mock_get_request.return_value = None

        decision, reason, request_id = await svc.request_agent_permission(
            **_request_kwargs()
        )

        assert decision == "deny"
        assert reason == "Approval request not found"
        assert request_id == str(approval.id)
