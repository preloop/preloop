"""Tests for the Claude Code ``permission_prompt`` builtin adapter."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.models import models
from preloop.services.permission_prompt import (
    CONSUMED_KEY,
    FINGERPRINT_KEY,
    PENDING_MARKER,
    TOOL_USE_ID_KEY,
    _map_terminal,
    _wait_budget_seconds,
    dedupe_stmt,
    evaluate_permission_prompt,
    permission_prompt_fingerprint,
)


def _mock_session_ctx(db):
    """A get_async_db_session replacement yielding ``db``."""
    ctx = MagicMock()
    ctx.return_value.__aenter__ = AsyncMock(return_value=db)
    ctx.return_value.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _scalars_result(rows):
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


def _approval_row(
    *,
    status: str,
    fingerprint: str,
    resolved_seconds_ago: float | None = None,
    consumed: bool = False,
    expires_in_seconds: float | None = 300,
    comment: str | None = None,
) -> models.ApprovalRequest:
    """A real (detached) ApprovalRequest so flag_modified works."""
    now = datetime.utcnow()
    row = models.ApprovalRequest(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        tool_configuration_id=uuid.uuid4(),
        approval_workflow_id=uuid.uuid4(),
        tool_name="Bash",
        tool_args={
            "command": "ls",
            FINGERPRINT_KEY: fingerprint,
            **({CONSUMED_KEY: True} if consumed else {}),
        },
        status=status,
        requested_at=now,
    )
    if resolved_seconds_ago is not None:
        row.resolved_at = now - timedelta(seconds=resolved_seconds_ago)
    if expires_in_seconds is not None:
        row.expires_at = now + timedelta(seconds=expires_in_seconds)
    row.approver_comment = comment
    return row


class TestFingerprint:
    def test_stable_for_identical_calls(self):
        a = permission_prompt_fingerprint("Bash", {"command": "ls", "cwd": "/x"})
        b = permission_prompt_fingerprint("Bash", {"cwd": "/x", "command": "ls"})
        assert a == b

    def test_differs_by_input(self):
        a = permission_prompt_fingerprint("Bash", {"command": "ls"})
        b = permission_prompt_fingerprint("Bash", {"command": "rm -rf /"})
        assert a != b

    def test_differs_by_tool(self):
        a = permission_prompt_fingerprint("Bash", {"command": "ls"})
        b = permission_prompt_fingerprint("Write", {"command": "ls"})
        assert a != b


class TestBehaviorMapping:
    def test_approved_returns_allow_with_updated_input(self):
        behavior = _map_terminal("approved", None, {"command": "ls"}, "rid")
        assert behavior == {"behavior": "allow", "updatedInput": {"command": "ls"}}

    def test_declined_returns_deny_with_comment(self):
        behavior = _map_terminal("declined", "too risky", {}, "rid")
        assert behavior["behavior"] == "deny"
        assert "too risky" in behavior["message"]
        assert PENDING_MARKER not in behavior["message"]

    def test_expired_returns_retryable_deny(self):
        behavior = _map_terminal("expired", None, {}, "rid")
        assert behavior["behavior"] == "deny"
        assert "Retry" in behavior["message"]


class TestWaitBudget:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("PRELOOP_PERMISSION_PROMPT_WAIT_SECONDS", raising=False)
        assert _wait_budget_seconds() == 25.0

    def test_env_override_and_clamp(self, monkeypatch):
        monkeypatch.setenv("PRELOOP_PERMISSION_PROMPT_WAIT_SECONDS", "10")
        assert _wait_budget_seconds() == 10.0
        monkeypatch.setenv("PRELOOP_PERMISSION_PROMPT_WAIT_SECONDS", "99999")
        assert _wait_budget_seconds() == 600.0
        monkeypatch.setenv("PRELOOP_PERMISSION_PROMPT_WAIT_SECONDS", "garbage")
        assert _wait_budget_seconds() == 25.0


@pytest.mark.asyncio
class TestEvaluatePermissionPrompt:
    async def _run(self, db, **kwargs):
        defaults = dict(
            base_url="http://localhost",
            account_id=str(uuid.uuid4()),
            user_id=uuid.uuid4(),
            managed_agent_id=None,
            runtime_session_id=None,
            managed_agent_name="Claude Code",
            source="claude_code",
            tool_name="Bash",
            tool_input={"command": "ls"},
            wait_seconds=0,
        )
        defaults.update(kwargs)
        with patch(
            "preloop.services.permission_prompt.get_async_db_session",
            new=_mock_session_ctx(db),
        ):
            return await evaluate_permission_prompt(**defaults)

    async def test_reattaches_to_pending_request_without_new_approval(self):
        fp = permission_prompt_fingerprint("Bash", {"command": "ls"})
        pending = _approval_row(status="pending", fingerprint=fp)
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result([pending]))

        with (
            patch(
                "preloop.models.crud.approval_request.get_approval_request_async",
                new=AsyncMock(return_value=pending),
            ),
            patch("preloop.services.approval_service.ApprovalService") as service_cls,
        ):
            behavior = await self._run(db)

        service_cls.assert_not_called()
        assert behavior["behavior"] == "deny"
        assert behavior["message"].startswith(PENDING_MARKER)
        assert str(pending.id) in behavior["message"]

    async def test_delivers_decision_made_between_retries_and_consumes_it(self):
        fp = permission_prompt_fingerprint("Bash", {"command": "ls"})
        approved = _approval_row(
            status="approved", fingerprint=fp, resolved_seconds_ago=5
        )
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result([approved]))

        behavior = await self._run(db)

        assert behavior == {
            "behavior": "allow",
            "updatedInput": {"command": "ls"},
        }
        # The row is single-use: consumed flag set and committed.
        assert approved.tool_args[CONSUMED_KEY] is True
        db.commit.assert_awaited()

    async def test_consumed_and_stale_rows_are_skipped(self):
        fp = permission_prompt_fingerprint("Bash", {"command": "ls"})
        consumed = _approval_row(
            status="approved", fingerprint=fp, resolved_seconds_ago=5, consumed=True
        )
        stale = _approval_row(
            status="approved", fingerprint=fp, resolved_seconds_ago=100000
        )
        dead_pending = _approval_row(
            status="pending", fingerprint=fp, expires_in_seconds=-10
        )
        fresh = MagicMock()
        fresh.id = uuid.uuid4()
        fresh.status = "pending"
        fresh.approver_comment = None
        fresh.expires_at = None

        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=_scalars_result([consumed, stale, dead_pending])
        )

        workflow = MagicMock()
        workflow.id = uuid.uuid4()
        config = MagicMock()
        config.id = uuid.uuid4()

        with (
            patch(
                "preloop.services.permission_prompt._native_tool_approvals_disabled",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "preloop.services.permission_prompt._resolve_workflow",
                new=AsyncMock(return_value=workflow),
            ),
            patch(
                "preloop.services.permission_prompt._resolve_tool_config",
                new=AsyncMock(return_value=config),
            ),
            patch("preloop.services.approval_service.ApprovalService") as service_cls,
            patch(
                "preloop.models.crud.approval_request.get_approval_request_async",
                new=AsyncMock(return_value=fresh),
            ),
        ):
            service = AsyncMock()
            service.create_and_notify = AsyncMock(return_value=fresh)
            service_cls.return_value = service

            behavior = await self._run(db)

        # None of the old rows satisfied the call: a new approval was raised.
        service.create_and_notify.assert_awaited_once()
        assert behavior["behavior"] == "deny"
        assert behavior["message"].startswith(PENDING_MARKER)

    async def test_fresh_request_records_fingerprint_and_tool_use_id(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result([]))

        workflow = MagicMock()
        workflow.id = uuid.uuid4()
        config = MagicMock()
        config.id = uuid.uuid4()
        approved = _approval_row(
            status="approved",
            fingerprint=permission_prompt_fingerprint("Bash", {"command": "ls"}),
            resolved_seconds_ago=0,
        )

        with (
            patch(
                "preloop.services.permission_prompt._native_tool_approvals_disabled",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "preloop.services.permission_prompt._resolve_workflow",
                new=AsyncMock(return_value=workflow),
            ),
            patch(
                "preloop.services.permission_prompt._resolve_tool_config",
                new=AsyncMock(return_value=config),
            ),
            patch("preloop.services.approval_service.ApprovalService") as service_cls,
        ):
            service = AsyncMock()
            service.create_and_notify = AsyncMock(return_value=approved)
            service_cls.return_value = service

            behavior = await self._run(db, tool_use_id="toolu_123")

        # Instantly-approved (e.g. AI workflow) → allow, and consumed.
        assert behavior["behavior"] == "allow"
        assert approved.tool_args[CONSUMED_KEY] is True

        kwargs = service.create_and_notify.await_args.kwargs
        assert kwargs["tool_name"] == "Bash"
        assert kwargs["tool_args"][FINGERPRINT_KEY] == (
            permission_prompt_fingerprint("Bash", {"command": "ls"})
        )
        assert kwargs["tool_args"][TOOL_USE_ID_KEY] == "toolu_123"
        assert kwargs["tool_args"]["_preloop_source"] == "claude_code"
        assert kwargs["tool_args"]["command"] == "ls"

    async def test_declined_fresh_request_maps_to_deny(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result([]))

        workflow = MagicMock()
        workflow.id = uuid.uuid4()
        config = MagicMock()
        config.id = uuid.uuid4()
        declined = _approval_row(
            status="declined",
            fingerprint="fp",
            resolved_seconds_ago=0,
            comment="not on prod",
        )

        with (
            patch(
                "preloop.services.permission_prompt._native_tool_approvals_disabled",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "preloop.services.permission_prompt._resolve_workflow",
                new=AsyncMock(return_value=workflow),
            ),
            patch(
                "preloop.services.permission_prompt._resolve_tool_config",
                new=AsyncMock(return_value=config),
            ),
            patch("preloop.services.approval_service.ApprovalService") as service_cls,
        ):
            service = AsyncMock()
            service.create_and_notify = AsyncMock(return_value=declined)
            service_cls.return_value = service

            behavior = await self._run(db)

        assert behavior["behavior"] == "deny"
        assert "not on prod" in behavior["message"]
        assert PENDING_MARKER not in behavior["message"]

    async def test_native_tool_approvals_off_passes_standing_bypass(self):
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result([]))

        workflow = MagicMock()
        workflow.id = uuid.uuid4()
        config = MagicMock()
        config.id = uuid.uuid4()
        auto = _approval_row(
            status="approved", fingerprint="fp", resolved_seconds_ago=0
        )

        with (
            patch(
                "preloop.services.permission_prompt._native_tool_approvals_disabled",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "preloop.services.permission_prompt._resolve_workflow",
                new=AsyncMock(return_value=workflow),
            ),
            patch(
                "preloop.services.permission_prompt._resolve_tool_config",
                new=AsyncMock(return_value=config),
            ),
            patch("preloop.services.approval_service.ApprovalService") as service_cls,
        ):
            service = AsyncMock()
            service.create_and_notify = AsyncMock(return_value=auto)
            service_cls.return_value = service

            behavior = await self._run(db, managed_agent_id=uuid.uuid4())

        assert behavior["behavior"] == "allow"
        kwargs = service.create_and_notify.await_args.kwargs
        assert (
            kwargs["standing_bypass_reason"]
            == models.AutoApprovedReason.NATIVE_TOOL_APPROVALS_OFF
        )


def test_dedupe_stmt_executes_on_real_database(db_session, test_user):
    """The JSONB fingerprint extraction must execute on real Postgres.

    Mock-based tests never run the ``tool_args[...].astext`` SQL; this seeds a
    real approval request and runs the exact statement shape used by
    ``evaluate_permission_prompt``.
    """
    fp = permission_prompt_fingerprint("Bash", {"command": "ls"})

    workflow = models.ApprovalWorkflow(
        account_id=test_user.account_id,
        name="permission-prompt-test-workflow",
        approval_type="manual",
        channel="email",
    )
    db_session.add(workflow)
    db_session.flush()

    config = models.ToolConfiguration(
        account_id=test_user.account_id,
        tool_name="Bash",
        tool_source="agent",
        approval_workflow_id=workflow.id,
        is_enabled=True,
        custom_config={},
    )
    db_session.add(config)
    db_session.flush()

    request = models.ApprovalRequest(
        account_id=test_user.account_id,
        tool_configuration_id=config.id,
        approval_workflow_id=workflow.id,
        tool_name="Bash",
        tool_args={"command": "ls", FINGERPRINT_KEY: fp},
        status="pending",
    )
    other = models.ApprovalRequest(
        account_id=test_user.account_id,
        tool_configuration_id=config.id,
        approval_workflow_id=workflow.id,
        tool_name="Bash",
        tool_args={"command": "rm", FINGERPRINT_KEY: "different"},
        status="pending",
    )
    db_session.add_all([request, other])
    db_session.flush()

    rows = list(
        db_session.execute(dedupe_stmt(str(test_user.account_id), fp)).scalars().all()
    )
    assert [r.id for r in rows] == [request.id]


@pytest.mark.asyncio
class TestMcpRegistration:
    async def test_permission_prompt_tool_registered(self):
        from preloop.services.initialize_mcp import initialize_mcp_with_tools

        mcp = initialize_mcp_with_tools()
        tool = await mcp.get_tool("permission_prompt")
        assert tool is not None

    async def test_no_user_context_fails_closed_with_behavior_schema(self):
        from preloop.services.initialize_mcp import initialize_mcp_with_tools

        mcp = initialize_mcp_with_tools()
        tool = await mcp.get_tool("permission_prompt")
        with patch(
            "preloop.services.dynamic_fastmcp_http.get_current_user_context",
            return_value=None,
        ):
            raw = await tool.fn(tool_name="Bash", input={"command": "ls"})
        behavior = json.loads(raw)
        assert behavior["behavior"] == "deny"
        assert "message" in behavior

    async def test_evaluator_error_fails_closed(self):
        from preloop.services.initialize_mcp import initialize_mcp_with_tools

        mcp = initialize_mcp_with_tools()
        tool = await mcp.get_tool("permission_prompt")
        user_context = MagicMock()
        user_context.account_id = str(uuid.uuid4())
        user_context.user_id = str(uuid.uuid4())
        user_context.managed_agent_id = None
        user_context.runtime_session_id = None
        user_context.runtime_principal_name = "Claude Code"
        with (
            patch(
                "preloop.services.dynamic_fastmcp_http.get_current_user_context",
                return_value=user_context,
            ),
            patch(
                "preloop.services.permission_prompt.evaluate_permission_prompt",
                new=AsyncMock(side_effect=RuntimeError("db down")),
            ),
        ):
            raw = await tool.fn(tool_name="Bash", input={"command": "ls"})
        behavior = json.loads(raw)
        assert behavior["behavior"] == "deny"
        assert "safety" in behavior["message"]

    async def test_allow_passes_through_behavior_json(self):
        from preloop.services.initialize_mcp import initialize_mcp_with_tools

        mcp = initialize_mcp_with_tools()
        tool = await mcp.get_tool("permission_prompt")
        user_context = MagicMock()
        user_context.account_id = str(uuid.uuid4())
        user_context.user_id = str(uuid.uuid4())
        user_context.managed_agent_id = str(uuid.uuid4())
        user_context.runtime_session_id = None
        user_context.runtime_principal_name = "Claude Code"
        with (
            patch(
                "preloop.services.dynamic_fastmcp_http.get_current_user_context",
                return_value=user_context,
            ),
            patch(
                "preloop.services.permission_prompt.evaluate_permission_prompt",
                new=AsyncMock(
                    return_value={
                        "behavior": "allow",
                        "updatedInput": {"command": "ls"},
                    }
                ),
            ) as evaluator,
        ):
            raw = await tool.fn(
                tool_name="Bash",
                input={"command": "ls"},
                tool_use_id="toolu_1",
            )
        behavior = json.loads(raw)
        assert behavior == {"behavior": "allow", "updatedInput": {"command": "ls"}}
        kwargs = evaluator.await_args.kwargs
        assert kwargs["source"] == "claude_code"
        assert kwargs["tool_use_id"] == "toolu_1"
        assert str(kwargs["managed_agent_id"]) == user_context.managed_agent_id
