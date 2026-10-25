"""Real note preparation, approval persistence, and MCP polling replay."""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_managed_agent, crud_user
from preloop.models.crud.agent_note_policy import seed_note_policy
from preloop.models.db.session import SyncApprovalSession
from preloop.services.approval_service import ApprovalService
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.initialize_mcp import initialize_mcp_with_tools
from preloop.services.agent_session_lineage import register_session_start


class ApprovalTestSession(SyncApprovalSession):
    """Keep the test's transaction open when the helper releases its session."""

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("decision_path", ["console", "token"])
@pytest.mark.parametrize(
    "decision", ["approved", "declined", "expired", "owner_changed", "rate_limited"]
)
async def test_note_approval_lifecycle(
    db_session: Session, test_user: models.User, decision: str, decision_path: str
) -> None:
    author = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=test_user.account_id,
        display_name="Coordinator",
        commit=True,
    )
    target = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=test_user.account_id,
        display_name="Worker",
        commit=True,
    )
    author.owner_user_id = target.owner_user_id = test_user.id
    account = db_session.get(models.Account, test_user.account_id)
    account.primary_user_id = test_user.id
    parent = register_session_start(
        db_session,
        account_id=test_user.account_id,
        principal_type="managed_agent",
        principal_id=str(author.id),
        principal_name="Coordinator",
        external_session_id="coordinator-run",
    )
    worker = register_session_start(
        db_session,
        account_id=test_user.account_id,
        principal_type="managed_agent",
        principal_id=str(target.id),
        principal_name="Worker",
        external_session_id="worker-run",
        alias="worker",
    )
    assert parent is not None and worker is not None
    author.runtime_session_id = UUID(parent.runtime_session_id)
    target.runtime_session_id = UUID(worker.runtime_session_id)
    seed_note_policy(db_session, account_id=test_user.account_id)
    db_session.flush()
    caller = UserContext(
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        username=test_user.username,
        managed_agent_id=str(author.id),
        runtime_principal_type="managed_agent",
        runtime_principal_id=str(author.id),
    )
    server = initialize_mcp_with_tools()
    note_tool = await server.get_tool("send_note")
    status_tool = await server.get_tool("get_approval_status")
    approval_db = ApprovalTestSession(db_session)

    @asynccontextmanager
    async def async_session():
        yield approval_db

    async def replay(name: str, arguments: dict, *, account_id: str) -> str:
        assert name == "send_note"
        assert account_id == caller.account_id
        return await note_tool.fn(**arguments)

    with (
        patch(
            "preloop.services.dynamic_fastmcp_http.get_current_user_context",
            return_value=caller,
        ),
        patch(
            "preloop.models.db.session.get_db_session",
            side_effect=lambda: iter([db_session]),
        ),
        patch("preloop.models.db.session.get_async_db_session", new=async_session),
        patch(
            "preloop.api.endpoints.public_approval.get_async_db_session",
            new=async_session,
        ),
        patch.object(db_session, "close"),
        patch(
            "preloop.services.approval_summary.generate_approval_summary",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            ApprovalService, "send_notifications", new=AsyncMock(return_value={})
        ) as notify,
        patch.object(ApprovalService, "_broadcast_approval_update", new=AsyncMock()),
        patch.object(ApprovalService, "_enqueue_approval_webhook", new=AsyncMock()),
        patch(
            "preloop.services.approval_service._log_approval_lifecycle_async"
        ) as audit_decision,
        patch("preloop.services.approval_service._log_approval_tool_executed_async"),
        patch("preloop.services.policy_evaluator._log_policy_decision_async"),
        patch.object(
            server, "call_registered_tool_without_policy", side_effect=replay
        ) as dispatch,
    ):
        pending = json.loads(
            await note_tool.fn(
                text="Report progress.",
                alias="worker",
                preloop_caller_session_id="coordinator-run",
            )
        )
        assert pending["status"] == "pending_approval", pending
        request_id = UUID(pending["request_id"])
        request = db_session.get(models.ApprovalRequest, request_id)
        assert request is not None
        assert request.tool_args["runtime_session_id"] == worker.runtime_session_id
        assert request.rule_context["note_facts"]["note_target_relation"] == "unrelated"
        assert request.approval_workflow.approver_user_ids == [test_user.id]
        assert (
            db_session.query(models.AgentControlCommand)
            .filter_by(account_id=test_user.account_id, kind="note")
            .count()
            == 0
        )
        service = ApprovalService(approval_db, "https://example.com")
        if decision == "rate_limited":
            for _ in range(2):
                more = json.loads(
                    await note_tool.fn(text="Report progress.", alias="worker")
                )
                assert more["status"] == "pending_approval", more
            limited = json.loads(
                await note_tool.fn(text="Report progress.", alias="worker")
            )
            assert limited["error"]["code"] == "note_approval_rate_limited", limited
            assert notify.await_count == 3
            assert db_session.query(models.AgentControlCommand).count() == 0
            assert (
                db_session.query(models.ApprovalRequest)
                .filter_by(account_id=test_user.account_id)
                .count()
                == 3
            )
            await service.decline_request(request_id, user_id=test_user.id)
            retry = json.loads(
                await note_tool.fn(text="Report progress.", alias="worker")
            )
            assert retry["status"] == "pending_approval", retry
            assert notify.await_count == 4
            assert db_session.query(models.AgentControlCommand).count() == 0
            return

        async def decide(action: str) -> None:
            if decision_path == "token":
                from preloop.api.endpoints.public_approval import (
                    ApprovalDecisionRequest,
                    _decide_with_token,
                )

                await _decide_with_token(
                    request_id,
                    ApprovalDecisionRequest(action=action),
                    request.approval_token,
                    db_session,
                )
            elif action == "approve":
                await service.approve_request(request_id, user_id=test_user.id)
            else:
                await service.decline_request(request_id, user_id=test_user.id)

        if decision in ("approved", "owner_changed"):
            await decide("approve")
            if decision == "owner_changed":
                new_owner = crud_user.create(
                    db_session,
                    obj_in={
                        "account_id": test_user.account_id,
                        "email": "new-owner@example.com",
                        "username": "new-owner",
                        "hashed_password": "unused",
                        "is_active": True,
                        "is_superuser": True,
                    },
                )
                author.owner_user_id = new_owner.id
                db_session.commit()
        elif decision == "declined":
            await decide("decline")
        else:
            request.expires_at = datetime.utcnow() - timedelta(seconds=1)
            db_session.flush()
            await service.approve_request(request_id, user_id=test_user.id)
        expected_status = "approved" if decision == "owner_changed" else decision
        assert request.status == expected_status
        assert audit_decision.called
        if decision == "approved":
            caller.managed_agent_id = str(target.id)
            blocked = json.loads(await status_tool.fn(request_id=str(request_id)))
            assert "Only the sending agent" in blocked["tool_execution_error"]
            dispatch.assert_not_awaited()
            caller.managed_agent_id = str(author.id)
        result = json.loads(await status_tool.fn(request_id=str(request_id)))
        assert result["status"] == expected_status, result
        if decision == "approved":
            assert json.loads(result["tool_result"]["text"])["ok"] is True, result
            dispatch.assert_awaited_once()
            await status_tool.fn(request_id=str(request_id))
            dispatch.assert_awaited_once()  # Polling again cannot duplicate the note.
        elif decision == "owner_changed":
            assert (
                json.loads(result["tool_result"]["text"])["error"]["code"]
                == "note_approval_changed"
            )
            dispatch.assert_awaited_once()
        else:
            dispatch.assert_not_awaited()
        notes = (
            db_session.query(models.AgentControlCommand)
            .filter_by(account_id=test_user.account_id, kind="note")
            .all()
        )
        assert len(notes) == (1 if decision == "approved" else 0)
        if notes:
            assert str(notes[0].runtime_session_id) == worker.runtime_session_id
            assert notes[0].created_by_managed_agent_id == author.id
