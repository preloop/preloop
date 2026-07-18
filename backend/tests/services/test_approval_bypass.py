"""Tests for time-boxed approval bypasses.

These tests are deliberately weighted toward the *safety* properties rather
than the happy path, because this feature intentionally disables the product's
core guarantee. The invariants under test:

1. A bypass never applies unless every approver has one (no unilateral
   disarming of a shared control).
2. An expired or revoked bypass has no effect.
3. A ``mute_notifications`` bypass never auto-approves.
4. Auto-approved requests are **recorded** and are permanently distinguishable
   from human decisions.
5. Any failure while resolving a bypass fails closed (approval still required).
"""

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.models.models import ApprovalRequest, ApprovalWorkflow
from preloop.models.models.approval_bypass import (
    ApprovalBypass,
    ApprovalBypassMode,
    DEFAULT_BYPASS_DURATION,
    MAX_BYPASS_DURATION,
)
from preloop.models.crud.approval_bypass import _strongest
from preloop.models.schemas.approval_request import ApprovalRequestResponse
from preloop.services.approval_service import ApprovalService

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_db():
    """Create mock async database session."""
    return AsyncMock()


@pytest.fixture
def approval_service(mock_db):
    """Create ApprovalService with a mocked database."""
    return ApprovalService(mock_db, "https://app.test.com")


@pytest.fixture
def workflow():
    """Approval workflow with a single approver."""
    wf = MagicMock(spec=ApprovalWorkflow)
    wf.id = uuid.uuid4()
    wf.approval_type = "slack"
    wf.timeout_seconds = 300
    wf.approval_config = {}
    wf.approval_mode = "human"
    return wf


@pytest.fixture
def account_id():
    """Account id as the service receives it (a string)."""
    return str(uuid.uuid4())


def make_bypass(
    *,
    mode=ApprovalBypassMode.AUTO_APPROVE,
    expires_in=timedelta(hours=1),
    revoked=False,
    managed_agent_id=None,
    user_id=None,
    account_id=None,
):
    """Build an ApprovalBypass instance for tests."""
    bypass = ApprovalBypass(
        id=uuid.uuid4(),
        account_id=account_id or uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
        managed_agent_id=managed_agent_id,
        mode=mode,
        created_by_user_id=user_id or uuid.uuid4(),
        expires_at=datetime.utcnow() + expires_in,
        auto_approved_count=0,
    )
    if revoked:
        bypass.revoked_at = datetime.utcnow()
    return bypass


# --------------------------------------------------------------------------
# Model-level invariants
# --------------------------------------------------------------------------


class TestBypassModel:
    """Invariants on the ApprovalBypass model itself."""

    async def test_active_bypass_is_active(self):
        """A fresh, unrevoked bypass is in force."""
        assert make_bypass().is_active() is True

    async def test_expired_bypass_is_not_active(self):
        """An expired bypass stops applying without anyone intervening.

        This is the whole point of time-boxing: forgetting is harmless.
        """
        assert make_bypass(expires_in=timedelta(seconds=-1)).is_active() is False

    async def test_revoked_bypass_is_not_active(self):
        """A revoked bypass stops applying even before its expiry."""
        assert make_bypass(revoked=True).is_active() is False

    async def test_default_duration_is_bounded_and_short(self):
        """The default is an hour and the cap is not open-ended."""
        assert DEFAULT_BYPASS_DURATION == timedelta(hours=1)
        assert MAX_BYPASS_DURATION <= timedelta(hours=24)

    async def test_auto_approve_outranks_mute(self):
        """When both modes are active the stronger one wins."""
        mute = make_bypass(mode=ApprovalBypassMode.MUTE_NOTIFICATIONS)
        auto = make_bypass(mode=ApprovalBypassMode.AUTO_APPROVE)
        assert _strongest([mute, auto]) is auto
        assert _strongest([auto, mute]) is auto

    async def test_strongest_of_empty_is_none(self):
        """No candidates means no bypass."""
        assert _strongest([]) is None


# --------------------------------------------------------------------------
# Resolution: who a bypass applies to
# --------------------------------------------------------------------------


class TestBypassResolution:
    """_resolve_bypass must fail closed and must require unanimity."""

    async def test_no_approvers_means_no_bypass(
        self, approval_service, workflow, account_id
    ):
        """A workflow with no resolvable approvers is never bypassed."""
        approval_service._get_all_approver_user_ids = AsyncMock(return_value=[])
        result = await approval_service._resolve_bypass(
            workflow, account_id, None, ApprovalBypassMode.AUTO_APPROVE
        )
        assert result is None

    async def test_approver_lookup_failure_fails_closed(
        self, approval_service, workflow, account_id
    ):
        """If approvers cannot be resolved, enforce approval rather than skip it."""
        approval_service._get_all_approver_user_ids = AsyncMock(
            side_effect=RuntimeError("db down")
        )
        result = await approval_service._resolve_bypass(
            workflow, account_id, None, ApprovalBypassMode.AUTO_APPROVE
        )
        assert result is None

    async def test_invalid_account_id_fails_closed(self, approval_service, workflow):
        """A malformed account id must not be interpreted as 'bypass everything'."""
        approval_service._get_all_approver_user_ids = AsyncMock(
            return_value=[uuid.uuid4()]
        )
        result = await approval_service._resolve_bypass(
            workflow, "not-a-uuid", None, ApprovalBypassMode.AUTO_APPROVE
        )
        assert result is None

    async def test_single_approver_with_bypass_resolves(
        self, approval_service, workflow, account_id
    ):
        """The common case: one approver who muted their own agents."""
        approver = uuid.uuid4()
        approval_service._get_all_approver_user_ids = AsyncMock(return_value=[approver])
        bypass = make_bypass(user_id=approver)

        with patch(
            "preloop.services.approval_service.get_active_bypass_async",
            AsyncMock(return_value=bypass),
        ):
            result = await approval_service._resolve_bypass(
                workflow, account_id, None, ApprovalBypassMode.AUTO_APPROVE
            )

        assert result is bypass

    async def test_partial_coverage_does_not_bypass(
        self, approval_service, workflow, account_id
    ):
        """One approver's bypass must not disable the gate for the others.

        This is the key multi-user safety property: a bypass is a personal
        escape hatch, not a way to unilaterally revoke a shared control.
        """
        approver_a, approver_b = uuid.uuid4(), uuid.uuid4()
        approval_service._get_all_approver_user_ids = AsyncMock(
            return_value=[approver_a, approver_b]
        )
        bypass_a = make_bypass(user_id=approver_a)

        async def _lookup(db, *, account_id, user_id, managed_agent_id=None, now=None):
            return bypass_a if user_id == approver_a else None

        with patch(
            "preloop.services.approval_service.get_active_bypass_async",
            AsyncMock(side_effect=_lookup),
        ):
            result = await approval_service._resolve_bypass(
                workflow, account_id, None, ApprovalBypassMode.AUTO_APPROVE
            )

        assert result is None

    async def test_mute_bypass_does_not_satisfy_auto_approve(
        self, approval_service, workflow, account_id
    ):
        """Muting notifications must never silently become auto-approval.

        These are separate controls precisely so a user can quiet their phone
        without surrendering the gate.
        """
        approver = uuid.uuid4()
        approval_service._get_all_approver_user_ids = AsyncMock(return_value=[approver])
        mute = make_bypass(user_id=approver, mode=ApprovalBypassMode.MUTE_NOTIFICATIONS)

        with patch(
            "preloop.services.approval_service.get_active_bypass_async",
            AsyncMock(return_value=mute),
        ):
            result = await approval_service._resolve_bypass(
                workflow, account_id, None, ApprovalBypassMode.AUTO_APPROVE
            )

        assert result is None

    async def test_auto_approve_satisfies_mute_requirement(
        self, approval_service, workflow, account_id
    ):
        """auto_approve subsumes muting, so it satisfies a mute check."""
        approver = uuid.uuid4()
        approval_service._get_all_approver_user_ids = AsyncMock(return_value=[approver])
        auto = make_bypass(user_id=approver, mode=ApprovalBypassMode.AUTO_APPROVE)

        with patch(
            "preloop.services.approval_service.get_active_bypass_async",
            AsyncMock(return_value=auto),
        ):
            result = await approval_service._resolve_bypass(
                workflow, account_id, None, ApprovalBypassMode.MUTE_NOTIFICATIONS
            )

        assert result is auto


# --------------------------------------------------------------------------
# Auto-approval is RECORDED and distinguishable
# --------------------------------------------------------------------------


class TestAutoApproveViaBypass:
    """A bypassed approval must leave a clearly-marked audit record."""

    @pytest.fixture
    def pending_request(self, account_id):
        """A pending approval request."""
        req = MagicMock(spec=ApprovalRequest)
        req.id = uuid.uuid4()
        req.account_id = uuid.UUID(account_id)
        req.tool_name = "Bash"
        req.execution_id = None
        req.managed_agent_id = None
        req.status = "pending"
        return req

    async def test_records_bypass_markers(self, approval_service, pending_request):
        """The row is updated with auto_approved_reason and the bypass id."""
        bypass = make_bypass()
        updated = MagicMock(spec=ApprovalRequest)
        updated.id = pending_request.id
        updated.account_id = pending_request.account_id
        updated.tool_name = "Bash"
        updated.execution_id = None
        updated.managed_agent_id = None

        approval_service.update_approval_request = AsyncMock(return_value=updated)
        approval_service._broadcast_approval_update = AsyncMock()

        with (
            patch(
                "preloop.services.approval_service.record_bypass_use_async",
                AsyncMock(),
            ),
            patch(
                "preloop.services.approval_service._log_approval_lifecycle_async"
            ) as mock_log,
        ):
            result = await approval_service._auto_approve_via_bypass(
                pending_request, bypass
            )

        assert result is updated
        update_arg = approval_service.update_approval_request.call_args[0][1]
        assert update_arg.status == "approved"
        assert update_arg.auto_approved_reason == "bypass"
        assert update_arg.auto_approval_bypass_id == bypass.id
        # Crucially NOT marked as an AI decision - an AI judged nothing here.
        assert update_arg.decided_by_ai is False

        # Audit entry has no human approver and is tagged [BYPASS].
        audit_kwargs = mock_log.call_args.kwargs
        assert audit_kwargs["approver_id"] is None
        assert audit_kwargs["reason"].startswith("[BYPASS]")

    async def test_increments_usage_counter(self, approval_service, pending_request):
        """Each auto-approval is counted so the console can show the blast radius."""
        bypass = make_bypass()
        updated = MagicMock(spec=ApprovalRequest)
        updated.id = pending_request.id
        updated.account_id = pending_request.account_id
        updated.tool_name = "Bash"
        updated.execution_id = None
        updated.managed_agent_id = None

        approval_service.update_approval_request = AsyncMock(return_value=updated)
        approval_service._broadcast_approval_update = AsyncMock()
        record_mock = AsyncMock()

        with (
            patch(
                "preloop.services.approval_service.record_bypass_use_async", record_mock
            ),
            patch("preloop.services.approval_service._log_approval_lifecycle_async"),
        ):
            await approval_service._auto_approve_via_bypass(pending_request, bypass)

        record_mock.assert_awaited_once()
        assert record_mock.await_args.kwargs["bypass_id"] == bypass.id

    async def test_comment_names_the_expiry(self, approval_service, pending_request):
        """The recorded comment says when the bypass ends, not just that it exists."""
        bypass = make_bypass()
        updated = MagicMock(spec=ApprovalRequest)
        updated.id = pending_request.id
        updated.account_id = pending_request.account_id
        updated.tool_name = "Bash"
        updated.execution_id = None
        updated.managed_agent_id = None

        approval_service.update_approval_request = AsyncMock(return_value=updated)
        approval_service._broadcast_approval_update = AsyncMock()

        with (
            patch(
                "preloop.services.approval_service.record_bypass_use_async", AsyncMock()
            ),
            patch("preloop.services.approval_service._log_approval_lifecycle_async"),
        ):
            await approval_service._auto_approve_via_bypass(pending_request, bypass)

        comment = approval_service.update_approval_request.call_args[0][1]
        assert bypass.expires_at.isoformat() in comment.approver_comment
        assert "without review" in comment.approver_comment


# --------------------------------------------------------------------------
# Statistics must never count a bypass as a human approval
# --------------------------------------------------------------------------


class TestStatisticsDistinction:
    """decided_by_human is the field every stat surface must filter on."""

    def _response(self, **overrides):
        """Build an ApprovalRequestResponse with sane defaults."""
        payload = {
            "id": uuid.uuid4(),
            "account_id": uuid.uuid4(),
            "tool_configuration_id": uuid.uuid4(),
            "approval_workflow_id": uuid.uuid4(),
            "tool_name": "Bash",
            "tool_args": {},
            "status": "approved",
            "requested_at": datetime.utcnow(),
            "resolved_at": datetime.utcnow(),
            "expires_at": None,
            "approver_comment": None,
            "webhook_posted_at": None,
            "webhook_error": None,
        }
        payload.update(overrides)
        return ApprovalRequestResponse(**payload)

    async def test_human_approval_counts_as_human(self):
        """A plain human approval is counted."""
        resp = self._response()
        assert resp.decided_by_human is True
        assert resp.was_bypassed is False

    async def test_bypassed_approval_is_not_human(self):
        """A bypassed approval must never inflate the approval rate."""
        resp = self._response(auto_approved_reason="bypass")
        assert resp.decided_by_human is False
        assert resp.was_bypassed is True

    async def test_ai_approval_is_not_human(self):
        """AI decisions were already non-human; that stays true."""
        resp = self._response(decided_by_ai=True)
        assert resp.decided_by_human is False
        assert resp.was_bypassed is False

    async def test_pending_request_is_not_a_decision(self):
        """Pending requests are not decisions of any kind."""
        resp = self._response(status="pending", resolved_at=None)
        assert resp.decided_by_human is False


# --------------------------------------------------------------------------
# create_and_notify wiring
# --------------------------------------------------------------------------


class TestCreateAndNotifyIntegration:
    """The bypass must short-circuit before notifications are sent."""

    async def test_bypass_suppresses_notifications_and_approves(
        self, approval_service, workflow, account_id
    ):
        """No notification is dispatched when auto-approving under a bypass."""
        created = MagicMock(spec=ApprovalRequest)
        created.id = uuid.uuid4()
        created.account_id = uuid.UUID(account_id)
        created.tool_name = "Bash"
        created.execution_id = None
        created.managed_agent_id = None
        created.status = "pending"

        approved = MagicMock(spec=ApprovalRequest)
        approved.status = "approved"

        bypass = make_bypass()

        approval_service.create_approval_request = AsyncMock(return_value=created)
        approval_service._resolve_bypass = AsyncMock(return_value=bypass)
        approval_service._auto_approve_via_bypass = AsyncMock(return_value=approved)
        approval_service.send_notifications = AsyncMock()
        approval_service.update_approval_request = AsyncMock(return_value=created)

        with patch(
            "preloop.services.approval_summary.generate_approval_summary",
            AsyncMock(return_value=None),
        ):
            result = await approval_service.create_and_notify(
                account_id=account_id,
                tool_configuration_id=uuid.uuid4(),
                approval_workflow=workflow,
                tool_name="Bash",
                tool_args={"command": "ls"},
            )

        assert result is approved
        approval_service.send_notifications.assert_not_called()
        approval_service._auto_approve_via_bypass.assert_awaited_once()

    async def test_bypass_error_falls_back_to_normal_approval(
        self, approval_service, workflow, account_id
    ):
        """A crash in bypass resolution must not auto-approve anything."""
        created = MagicMock(spec=ApprovalRequest)
        created.id = uuid.uuid4()
        created.account_id = uuid.UUID(account_id)
        created.tool_name = "Bash"
        created.execution_id = None
        created.managed_agent_id = None
        created.status = "pending"

        approval_service.create_approval_request = AsyncMock(return_value=created)
        approval_service._resolve_bypass = AsyncMock(side_effect=RuntimeError("boom"))
        approval_service._auto_approve_via_bypass = AsyncMock()
        approval_service.send_notifications = AsyncMock()
        approval_service.update_approval_request = AsyncMock(return_value=created)

        with patch(
            "preloop.services.approval_summary.generate_approval_summary",
            AsyncMock(return_value=None),
        ):
            result = await approval_service.create_and_notify(
                account_id=account_id,
                tool_configuration_id=uuid.uuid4(),
                approval_workflow=workflow,
                tool_name="Bash",
                tool_args={"command": "ls"},
            )

        approval_service._auto_approve_via_bypass.assert_not_called()
        approval_service.send_notifications.assert_awaited_once()
        assert result is created


class TestSendNotificationsMute:
    """send_notifications honors a mute without resolving the request."""

    async def test_mute_skips_all_channels(self, approval_service, workflow):
        """Muted approvals send nothing but stay pending."""
        req = MagicMock(spec=ApprovalRequest)
        req.id = uuid.uuid4()
        req.account_id = uuid.uuid4()
        req.managed_agent_id = None
        req.requested_at = datetime.utcnow()
        req.status = "pending"

        mute = make_bypass(mode=ApprovalBypassMode.MUTE_NOTIFICATIONS)
        approval_service._resolve_bypass = AsyncMock(return_value=mute)
        approval_service._send_email_notification = AsyncMock()
        approval_service._send_push_notification = AsyncMock()

        result = await approval_service.send_notifications(req, workflow)

        assert result["skipped"] is True
        assert result["reason"] == "notifications_muted"
        assert result["bypass_id"] == str(mute.id)
        approval_service._send_email_notification.assert_not_called()
        approval_service._send_push_notification.assert_not_called()
        # The request itself is untouched - it still blocks the agent.
        assert req.status == "pending"
