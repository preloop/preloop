"""Service-level tests for the account kill switch (#157)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.models.crud import crud_account_halt
from preloop.services.approval_service import ApprovalService
from preloop.services.kill_switch import (
    KILL_SWITCH_ERROR_CLASS,
    KILL_SWITCH_ERROR_CODE,
    gateway_halt_error,
    halted_scopes,
    invalidate_kill_switch_cache,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_kill_switch_cache():
    invalidate_kill_switch_cache()
    yield
    invalidate_kill_switch_cache()


class TestHaltedScopesCache:
    """The hot path reads halt state through a short-lived process cache."""

    def test_lookup_is_cached_per_account(self, db_session, test_user):
        account_id = test_user.account_id
        with patch(
            "preloop.services.kill_switch.crud_account_halt.active_scopes",
            wraps=crud_account_halt.active_scopes,
        ) as mock_active:
            assert halted_scopes(db_session, account_id) == set()
            assert halted_scopes(db_session, account_id) == set()
            assert mock_active.call_count == 1

    def test_invalidate_forces_a_fresh_lookup(self, db_session, test_user):
        account_id = test_user.account_id
        halted_scopes(db_session, account_id)
        crud_account_halt.set_scopes(
            db_session,
            account_id=account_id,
            scopes=["gateway"],
            active=True,
            user_id=test_user.id,
        )
        # Stale without invalidation (this is the TTL window other
        # processes live with)...
        assert halted_scopes(db_session, account_id) == set()
        # ...and immediate for the process that served the toggle.
        invalidate_kill_switch_cache(account_id)
        assert halted_scopes(db_session, account_id) == {"gateway"}

    def test_accounts_do_not_share_cache_entries(self, db_session, test_user):
        other_account = str(uuid.uuid4())
        halted_scopes(db_session, test_user.account_id)
        with patch(
            "preloop.services.kill_switch.crud_account_halt.active_scopes",
            return_value={"gateway", "tools", "flows"},
        ):
            assert halted_scopes(db_session, other_account) == {
                "gateway",
                "tools",
                "flows",
            }


class TestGatewayHaltError:
    """Blocked requests carry a distinct, clearly-attributed error."""

    def test_openai_shape(self):
        error = gateway_halt_error(provider="openai")
        assert error.status_code == 403
        assert error.code == KILL_SWITCH_ERROR_CODE
        assert error.error_class == KILL_SWITCH_ERROR_CLASS
        payload = error.to_payload()
        assert payload["error"]["code"] == "preloop_account_halted"
        assert "kill switch" in payload["error"]["message"].lower()
        assert error.response_headers()["X-Preloop-Error-Class"] == "kill_switch"

    def test_anthropic_shape(self):
        error = gateway_halt_error(provider="anthropic", reason="runaway spend")
        payload = error.to_payload()
        assert payload["type"] == "error"
        assert payload["error"]["type"] == "permission_error"
        assert "runaway spend" in payload["error"]["message"]

    def test_error_code_is_distinct_from_budget_denials(self):
        error = gateway_halt_error(provider="openai")
        assert error.code != "budget_limit_exceeded"
        assert error.code == "preloop_account_halted"


class TestApprovalFreeze:
    """Pending approvals are frozen, not auto-denied, during a tools halt."""

    @pytest.fixture
    def approval_service(self):
        return ApprovalService(MagicMock(), "https://app.test.com")

    @pytest.fixture
    def expired_pending_request(self):
        request = MagicMock()
        request.id = uuid.uuid4()
        request.account_id = "test_account"
        request.status = "pending"
        request.expires_at = datetime.utcnow() - timedelta(seconds=1)
        request.approval_workflow = None
        return request

    async def test_late_decision_lands_during_halt(
        self, approval_service, expired_pending_request
    ):
        """A decision on a past-deadline request is accepted, not expired."""
        approval_service.get_approval_request_for_update = AsyncMock(
            return_value=expired_pending_request
        )
        approval_service.update_approval_request = AsyncMock(
            return_value=expired_pending_request
        )
        with patch(
            "preloop.services.approval_service.ApprovalService._approvals_frozen",
            new=AsyncMock(return_value=True),
        ):
            result = await approval_service._reject_if_not_actionable(
                expired_pending_request
            )
        assert result is None  # actionable: the decision may proceed

    async def test_late_decision_expires_without_halt(
        self, approval_service, expired_pending_request
    ):
        approval_service.get_approval_request_for_update = AsyncMock(
            return_value=expired_pending_request
        )
        approval_service.update_approval_request = AsyncMock(
            return_value=expired_pending_request
        )
        with patch(
            "preloop.services.approval_service.ApprovalService._approvals_frozen",
            new=AsyncMock(return_value=False),
        ):
            result = await approval_service._reject_if_not_actionable(
                expired_pending_request
            )
        assert result is expired_pending_request
        update_arg = approval_service.update_approval_request.call_args.args[1]
        assert update_arg.status == "expired"

    async def test_freeze_lookup_preserves_pending_on_error(self, approval_service):
        """Lookup failure cannot expire a pending human decision."""
        approval_service.db.run_sync = AsyncMock(
            side_effect=RuntimeError("db unavailable")
        )
        assert await approval_service._approvals_frozen("test_account") is True

    async def test_wait_loop_preserves_deadline_and_releases_session_before_sleep(
        self, approval_service, expired_pending_request
    ):
        """No deadline mutation or held connection while awaiting a halted approval."""
        original_deadline = expired_pending_request.expires_at
        expired_pending_request.escalation_triggered_at = None

        class _StopPollingError(Exception):
            pass

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                new=_fake_poll_session,
            ),
            patch.object(
                ApprovalService,
                "get_approval_request_for_update",
                return_value=expired_pending_request,
            ),
            patch(
                "preloop.services.approval_service.ApprovalService._approvals_frozen",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "preloop.services.approval_service.asyncio.sleep",
                new=AsyncMock(side_effect=_StopPollingError()),
            ),
        ):
            with pytest.raises(_StopPollingError):
                await approval_service.wait_for_approval(expired_pending_request.id)

        assert expired_pending_request.expires_at == original_deadline


def _fake_poll_session():
    """Factory matching get_async_db_session's call shape (async CM)."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session():
        yield AsyncMock()

    return _session()


class TestCrudAccountHalt:
    def test_unknown_scope_rejected(self, db_session, test_user):
        with pytest.raises(ValueError):
            crud_account_halt.set_scopes(
                db_session,
                account_id=test_user.account_id,
                scopes=["nonsense"],
                active=True,
                user_id=test_user.id,
            )
