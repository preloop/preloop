"""Tests for the approval workflow-history timeline (issue #335).

Every lifecycle transition must land a persisted ApprovalEvent so the
approval detail pages can render the complete history: requested, notified,
opened, voted, resolved/expired.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session
from preloop.models import models
from preloop.models.crud import crud_account_halt
from preloop.models.models.approval_event import ApprovalEvent
from preloop.models.models import ApprovalRequest, ApprovalWorkflow
from preloop.services.approval_service import ApprovalService

pytestmark = pytest.mark.asyncio


def _events_added_to(db) -> list[ApprovalEvent]:
    """Collect ApprovalEvent instances passed to a mock session's add()."""
    events = []
    for call in db.add.call_args_list:
        arg = call.args[0] if call.args else None
        if isinstance(arg, ApprovalEvent):
            events.append(arg)
    return events


def _first_commit_index(db) -> int:
    """Index of the first commit() call in the mock's recorded calls."""
    for index, call in enumerate(db.mock_calls):
        if call[0] == "commit":
            return index
    return -1


def _add_call_index(db, event: ApprovalEvent) -> int:
    """Index of the add() call that recorded ``event``."""
    for index, call in enumerate(db.mock_calls):
        if call[0] == "add" and call[1][0] is event:
            return index
    return -1


def make_service():
    db = AsyncMock()
    # AsyncSession.run_sync returns no tools halt for ordinary timeline cases.
    db.run_sync.return_value = None
    return ApprovalService(db, "https://app.test.com"), db


def make_request(status: str = "pending") -> MagicMock:
    request = MagicMock(spec=ApprovalRequest)
    request.id = uuid.uuid4()
    request.account_id = uuid.uuid4()
    request.tool_configuration_id = uuid.uuid4()
    request.approval_workflow_id = uuid.uuid4()
    request.tool_name = "test_tool"
    request.tool_args = {"arg1": "value1"}
    request.agent_reasoning = None
    request.status = status
    request.requested_at = datetime.utcnow()
    request.expires_at = datetime.utcnow() + timedelta(minutes=5)
    request.approval_token = "tok"
    request.responses = []
    return request


class TestDecisionChannelEvents:
    """Decisions must record who decided and through which channel."""

    async def test_approve_records_vote_with_channel_and_actor(self):
        service, db = make_service()
        request = make_request()
        workflow = MagicMock(spec=ApprovalWorkflow)
        workflow.approvals_required = 1
        request.approval_workflow = workflow
        user_id = uuid.uuid4()

        service.get_approval_request_for_update = AsyncMock(return_value=request)
        service.update_approval_request = AsyncMock(return_value=request)
        service._broadcast_approval_update = AsyncMock()

        await service.approve_request(
            request.id, "ship it", user_id=user_id, channel="console"
        )

        events = {e.event_type: e for e in _events_added_to(db)}
        vote = events["vote_received"]
        assert vote.actor_id == user_id
        assert "Approved" in vote.detail
        assert "via console" in vote.detail
        assert "1/1" in vote.detail
        assert "approval_complete" in events

    async def test_decline_records_vote_with_channel_and_actor(self):
        service, db = make_service()
        request = make_request()
        workflow = MagicMock(spec=ApprovalWorkflow)
        workflow.approvals_required = 1
        request.approval_workflow = workflow
        user_id = uuid.uuid4()

        service.get_approval_request_for_update = AsyncMock(return_value=request)
        service.update_approval_request = AsyncMock(return_value=request)
        service._broadcast_approval_update = AsyncMock()

        await service.decline_request(
            request.id, "too risky", user_id=user_id, channel="console"
        )

        events = {e.event_type: e for e in _events_added_to(db)}
        vote = events["vote_received"]
        assert vote.actor_id == user_id
        assert "Declined" in vote.detail
        assert "via console" in vote.detail

    async def test_token_decision_records_token_channel(self):
        service, db = make_service()
        request = make_request()
        workflow = MagicMock(spec=ApprovalWorkflow)
        workflow.approvals_required = 1
        request.approval_workflow = workflow

        service.get_approval_request_for_update = AsyncMock(return_value=request)
        service.update_approval_request = AsyncMock(return_value=request)
        service._broadcast_approval_update = AsyncMock()

        await service.approve_request(
            request.id, None, user_id=None, channel="token link"
        )

        events = {e.event_type: e for e in _events_added_to(db)}
        vote = events["vote_received"]
        assert vote.actor_id is None
        assert "via token link" in vote.detail


class TestExpiryEvent:
    """Expiry transitions must land an `expired` timeline entry."""

    async def test_late_decision_marks_expired_and_records_event(self):
        service, db = make_service()
        request = make_request(status="pending")
        request.expires_at = datetime.utcnow() - timedelta(seconds=1)
        expired = make_request(status="expired")

        service.get_approval_request_for_update = AsyncMock(return_value=request)
        service.update_approval_request = AsyncMock(return_value=expired)

        result = await service.approve_request(request.id, "too late")

        assert result.status == "expired"
        expired_events = [e for e in _events_added_to(db) if e.event_type == "expired"]
        assert len(expired_events) == 1
        assert "no response within" in expired_events[0].detail

    async def test_poll_timeout_records_expired_event(self):
        """wait_for_approval's expiry branch must persist the expired event."""
        service, db = make_service()
        request = make_request()
        request.expires_at = datetime.utcnow() - timedelta(seconds=1)
        workflow = MagicMock(spec=ApprovalWorkflow)
        workflow.escalation_user_ids = None
        workflow.escalation_team_ids = None
        workflow.timeout_seconds = 300
        request.approval_workflow = workflow

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = request
        db.execute.return_value = mock_result

        @asynccontextmanager
        async def _fake_poll_session():
            yield db

        with patch(
            "preloop.services.approval_service.get_task_publisher",
            new=AsyncMock(return_value=None),
        ):
            with patch(
                "preloop.models.db.session.get_async_db_session",
                new=_fake_poll_session,
            ):
                with pytest.raises(TimeoutError, match="expired without response"):
                    await asyncio.wait_for(
                        service.wait_for_approval(request.id, poll_interval=0.01),
                        timeout=2,
                    )

        expired_events = [e for e in _events_added_to(db) if e.event_type == "expired"]
        assert len(expired_events) == 1
        # The event must be flushed BEFORE the status commit: the poll session
        # closes right after, and nothing else would commit it.
        add_index = _add_call_index(db, expired_events[0])
        commit_index = _first_commit_index(db)
        assert add_index != -1 and commit_index != -1
        assert add_index < commit_index

    @pytest.mark.parametrize("halt_active", [False, True])
    async def test_overdue_approval_expiry_respects_persisted_tools_halt(
        self, db_session: Session, test_user: models.User, halt_active: bool
    ) -> None:
        """A real halt row freezes a deadline; an absent halt permits expiry."""
        if halt_active:
            crud_account_halt.set_scopes(
                db_session,
                account_id=test_user.account_id,
                scopes=["tools"],
                active=True,
                user_id=test_user.id,
                now=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        service, db = make_service()
        # Execute the production halt CRUD callback against the local test DB.
        db.run_sync.side_effect = lambda callback: callback(db_session)
        request = make_request()
        request.account_id = test_user.account_id
        request.expires_at = datetime.utcnow() - timedelta(seconds=1)
        expired = make_request(status="expired")
        service.update_approval_request = AsyncMock(return_value=expired)

        result = await service._reject_if_not_actionable(request)

        if halt_active:
            assert result is None
            service.update_approval_request.assert_not_awaited()
        else:
            assert result is expired
            service.update_approval_request.assert_awaited_once()


class TestNotificationEvents:
    """Channel fan-outs must be visible on the timeline."""

    async def test_audit_notification_result_records_notification_sent(self):
        service, db = make_service()
        request = make_request()
        recipient = uuid.uuid4()

        await service._audit_notification_result(
            approval_request=request,
            channel="email",
            channel_result={"success": True, "sent": 1},
            recipient_user_ids=[recipient],
            correlation_id=None,
        )

        events = [
            e for e in _events_added_to(db) if e.event_type == "notification_sent"
        ]
        assert len(events) == 1
        assert "email" in events[0].detail
        assert "sent" in events[0].detail
        # The event must be committed: nothing else commits this session.
        assert db.commit.called

    async def test_audit_notification_result_stores_emails_for_console(self):
        """Authenticated timeline keeps recipient emails; public path redacts."""
        service, db = make_service()
        request = make_request()
        user = MagicMock()
        user.id = uuid.uuid4()
        user.email = "jane@example.com"
        user.username = "jane"
        result = MagicMock()
        result.scalars.return_value = [user]
        db.execute.return_value = result

        await service._audit_notification_result(
            approval_request=request,
            channel="email",
            channel_result={"success": True, "sent": 1},
            recipient_user_ids=[user.id],
            correlation_id=None,
        )

        events = [
            e for e in _events_added_to(db) if e.event_type == "notification_sent"
        ]
        assert len(events) == 1
        assert "jane@example.com" in events[0].detail

    async def test_audit_notification_result_handles_recipient_lookup_failure(self):
        service, db = make_service()
        request = make_request()
        db.execute.side_effect = Exception("boom")

        await service._audit_notification_result(
            approval_request=request,
            channel="mobile_push",
            channel_result={"success": False},
            recipient_user_ids=[uuid.uuid4()],
            correlation_id=None,
        )

        events = [
            e for e in _events_added_to(db) if e.event_type == "notification_sent"
        ]
        assert len(events) == 1


class TestCreationEvent:
    """Every creation path must open the timeline with approval_requested."""

    async def test_create_approval_request_records_requested_event(self):
        service, db = make_service()
        workflow = MagicMock(spec=ApprovalWorkflow)
        workflow.id = uuid.uuid4()

        def _refresh(obj):
            obj.created_at = datetime.utcnow()
            obj.updated_at = datetime.utcnow()

        db.refresh.side_effect = _refresh
        result = MagicMock()
        result.scalar_one_or_none.return_value = workflow
        db.execute.return_value = result

        with patch(
            "preloop.services.approval_service.get_task_publisher",
            new=AsyncMock(return_value=None),
        ):
            created = await service.create_approval_request(
                account_id=str(uuid.uuid4()),
                tool_configuration_id=uuid.uuid4(),
                approval_workflow_id=workflow.id,
                tool_name="deploy",
                tool_args={"target": "staging"},
                managed_agent_name="helper",
            )

        events = [
            e for e in _events_added_to(db) if e.event_type == "approval_requested"
        ]
        assert len(events) == 1
        assert "deploy" in events[0].detail
        assert "helper" in events[0].detail
        # Committed immediately: muted flows return without further commits.
        assert db.commit.called
        assert created.status == "pending"


class TestAutoResolutionEvents:
    """Bypass/AI resolutions must close the timeline they opened."""

    async def test_auto_approve_without_review_records_event(self) -> None:
        service, db = make_service()
        request = make_request()

        service.update_approval_request = AsyncMock(return_value=request)
        service._broadcast_approval_update = AsyncMock()

        await service._auto_approve_without_review(
            request, reason_code="bypass", reason="bypass active"
        )

        events = [
            e for e in _events_added_to(db) if e.event_type == "approval_complete"
        ]
        assert len(events) == 1
        assert "without review" in events[0].detail

    async def test_auto_approve_by_ai_records_event(self) -> None:
        service, db = make_service()
        request = make_request()

        service.update_approval_request = AsyncMock(return_value=request)
        service._broadcast_approval_update = AsyncMock()

        await service._auto_approve_request(
            request.id, reason="safe read", ai_model="gpt"
        )

        events = [
            e for e in _events_added_to(db) if e.event_type == "approval_complete"
        ]
        assert len(events) == 1
        assert "Auto-approved by AI" in events[0].detail
        assert db.commit.called
