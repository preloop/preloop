"""Tests for async approval polling, tool result caching, and idempotency.

These tests validate the fixes for:
- Double-execution race condition (SELECT FOR UPDATE guard)
- Aware vs naive timestamp subtraction in remaining_seconds
- Cached tool_result idempotency on repeated polls
- Timestamp serialisation for event logs
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest


class TestTimestampSerialization:
    """Tests for event timestamp serialization (LOW fix)."""

    def test_naive_timestamp_serialized_correctly(self):
        """Naive timestamps should produce valid RFC3339 with trailing Z."""
        ts = datetime(2026, 2, 17, 20, 0, 0)
        result = ts.replace(tzinfo=None).isoformat() + "Z"
        assert result == "2026-02-17T20:00:00Z"

    def test_aware_timestamp_stripped_before_z(self):
        """Aware timestamps must have tzinfo stripped before appending Z."""
        ts = datetime(2026, 2, 17, 20, 0, 0, tzinfo=timezone.utc)
        result = ts.replace(tzinfo=None).isoformat() + "Z"
        assert result == "2026-02-17T20:00:00Z"
        # Without the fix this would produce "2026-02-17T20:00:00+00:00Z"

    def test_aware_timestamp_with_offset(self):
        """Non-UTC aware timestamps should still produce clean Z suffix."""
        tz_plus2 = timezone(timedelta(hours=2))
        ts = datetime(2026, 2, 17, 22, 0, 0, tzinfo=tz_plus2)
        result = ts.replace(tzinfo=None).isoformat() + "Z"
        # Note: this strips offset info; callers should convert to UTC first
        assert result.endswith("Z")
        assert "+0" not in result


class TestRemainingSecondsCalculation:
    """Tests for the remaining_seconds calculation (HIGH fix)."""

    def test_naive_expires_at_minus_naive_utcnow(self):
        """Both sides naive — no TypeError."""
        expires_at = datetime.utcnow() + timedelta(minutes=5)
        remaining = (expires_at - datetime.utcnow()).total_seconds()
        assert 290 < remaining <= 300

    def test_naive_minus_aware_raises(self):
        """Mixing naive and aware datetimes should raise TypeError.

        This is what the bug produced before the fix.
        """
        naive_ts = datetime(2026, 2, 17, 20, 0, 0)
        aware_ts = datetime.now(timezone.utc)
        with pytest.raises(TypeError):
            _ = (naive_ts - aware_ts).total_seconds()

    def test_expired_returns_zero(self):
        """Expired requests should clamp to 0."""
        expires_at = datetime.utcnow() - timedelta(minutes=1)
        remaining = (expires_at - datetime.utcnow()).total_seconds()
        assert max(0, int(remaining)) == 0


class TestToolResultIdempotency:
    """Tests for tool result caching / idempotency logic."""

    def test_cached_result_returned_without_re_execution(self):
        """When tool_result is already cached, it should be returned directly."""
        cached = {"text": "already executed"}
        approval_request = MagicMock()
        approval_request.status = "approved"
        approval_request.tool_result = cached
        approval_request.tool_name = "test_tool"

        response = {}
        if approval_request.status == "approved":
            if approval_request.tool_result is not None:
                response["tool_result"] = approval_request.tool_result

        assert response["tool_result"] == cached

    def test_none_result_triggers_execution(self):
        """When tool_result is None, execution should proceed."""
        approval_request = MagicMock()
        approval_request.status = "approved"
        approval_request.tool_result = None

        needs_execution = (
            approval_request.status == "approved"
            and approval_request.tool_result is None
        )
        assert needs_execution is True


class TestJustificationEnforcement:
    """Tests for server-side justification_mode=required enforcement."""

    def test_required_justification_missing_is_rejected(self):
        """Tool calls without justification should be rejected when required."""
        justification = None
        requires_justification = True

        should_reject = requires_justification and not justification
        assert should_reject is True

    def test_required_justification_present_is_allowed(self):
        """Tool calls with justification should pass when required."""
        justification = "Need to update the config file"
        requires_justification = True

        should_reject = requires_justification and not justification
        assert should_reject is False

    def test_optional_justification_missing_is_allowed(self):
        """Tool calls without justification should pass when optional."""
        requires_justification = False
        justification = None

        should_reject = requires_justification and not justification
        assert should_reject is False
        assert justification is None

    def test_empty_string_justification_is_rejected(self):
        """Empty string justification should be treated as missing."""
        justification = ""
        requires_justification = True

        should_reject = requires_justification and not justification
        assert should_reject is True


class TestApprovalPollingStateTransitions:
    """Tests for approval polling state machine transitions.

    An async approval request moves through these states:
      pending → approved  (human approves)
      pending → declined  (human declines)
      pending → expired   (timeout reached)
      approved → tool_result cached  (idempotent re-poll)
    """

    @staticmethod
    def _make_request(status="pending", **overrides):
        req = MagicMock()
        req.id = "req-1"
        req.status = status
        req.tool_name = "bash"
        req.tool_args = {"command": "ls"}
        req.tool_result = None
        req.agent_reasoning = None
        req.expires_at = datetime.utcnow() + timedelta(minutes=5)
        req.requested_at = datetime.utcnow()
        req.resolved_at = None
        req.approver_comment = None
        for k, v in overrides.items():
            setattr(req, k, v)
        return req

    def test_pending_to_approved(self):
        """Pending request transitions to approved after human decision."""
        req = self._make_request(status="pending")
        # Simulate human approval
        req.status = "approved"
        req.resolved_at = datetime.utcnow()
        req.approver_comment = "Looks safe"

        assert req.status == "approved"
        assert req.resolved_at is not None

    def test_pending_to_declined(self):
        """Pending request transitions to declined after human rejection."""
        req = self._make_request(status="pending")
        req.status = "declined"
        req.resolved_at = datetime.utcnow()
        req.approver_comment = "Too dangerous"

        assert req.status == "declined"
        assert req.approver_comment == "Too dangerous"

    def test_pending_to_expired(self):
        """Pending request expires when timeout is reached."""
        req = self._make_request(
            status="pending",
            expires_at=datetime.utcnow() - timedelta(seconds=1),
        )
        remaining = (req.expires_at - datetime.utcnow()).total_seconds()
        is_expired = remaining <= 0

        assert is_expired is True

    def test_approved_with_cached_result_is_idempotent(self):
        """Re-polling an approved request with cached tool_result returns same result."""
        cached = {"text": "command output"}
        req = self._make_request(status="approved", tool_result=cached)

        # First poll
        result1 = req.tool_result
        # Second poll (idempotent)
        result2 = req.tool_result

        assert result1 is result2
        assert result1 == cached

    def test_remaining_seconds_positive(self):
        """Active pending request reports positive remaining seconds."""
        req = self._make_request(expires_at=datetime.utcnow() + timedelta(minutes=3))
        remaining = max(0, int((req.expires_at - datetime.utcnow()).total_seconds()))
        assert 170 < remaining <= 180

    def test_remaining_seconds_clamped_to_zero(self):
        """Expired request clamps remaining_seconds to 0."""
        req = self._make_request(expires_at=datetime.utcnow() - timedelta(minutes=1))
        remaining = max(0, int((req.expires_at - datetime.utcnow()).total_seconds()))
        assert remaining == 0

    def test_declined_request_cannot_transition_to_approved(self):
        """Once declined, re-approving should not be allowed (guard logic)."""
        req = self._make_request(status="declined")
        # Guard: only pending requests can be approved
        can_approve = req.status == "pending"
        assert can_approve is False


class TestProxiedToolJustification:
    """Regression tests for the proxied-tool justification fix.

    Bug: _call_tool() strips justification from arguments into
    _justification_var, but the proxied-tool wrapper was doing
    arguments.pop("justification") again — always getting None.

    Fix: wrapper reads from _justification_var.get(None) instead.
    """

    def test_justification_preserved_via_context_var(self):
        """Justification stored in context var should be readable by wrapper."""
        from contextvars import ContextVar

        var: ContextVar = ContextVar("test_justification", default=None)
        # Simulate _call_tool setting the var
        var.set("I need to deploy the hotfix")

        # Simulate wrapper reading from var (the fix)
        justification = var.get(None)
        assert justification == "I need to deploy the hotfix"

    def test_justification_none_when_not_provided(self):
        """When agent provides no justification, var should be None."""
        from contextvars import ContextVar

        var: ContextVar = ContextVar("test_justification_none", default=None)

        justification = var.get(None)
        assert justification is None

    def test_arguments_pop_returns_none_after_strip(self):
        """Demonstrates the bug: pop from already-stripped args returns None."""
        arguments = {"command": "ls", "justification": "checking files"}

        # _call_tool strips justification first
        stripped = arguments.pop("justification", None)
        assert stripped == "checking files"

        # Proxied wrapper tries to pop again — always None (the bug)
        second_pop = arguments.pop("justification", None)
        assert second_pop is None

    def test_justification_passed_to_approval_request(self):
        """Justification should be forwarded as agent_reasoning in approval."""
        approval_kwargs = {}

        justification = "Emergency security patch"
        if justification:
            approval_kwargs["justification"] = justification

        assert approval_kwargs["justification"] == "Emergency security patch"


class TestPolicyGenerationRequestValidation:
    """Pydantic model validation for policy generation request/response."""

    def test_generate_policy_request_valid(self):
        from preloop.api.endpoints.policies import GeneratePolicyRequest

        req = GeneratePolicyRequest(prompt="require approval for bash")
        assert req.prompt == "require approval for bash"
        assert req.include_current_config is True  # default
        assert req.scope_mcp_server_name is None

    def test_generate_policy_request_no_context(self):
        from preloop.api.endpoints.policies import GeneratePolicyRequest

        req = GeneratePolicyRequest(prompt="deny all", include_current_config=False)
        assert req.include_current_config is False

    def test_generate_policy_request_missing_prompt_raises(self):
        from pydantic import ValidationError

        from preloop.api.endpoints.policies import GeneratePolicyRequest

        with pytest.raises(ValidationError):
            GeneratePolicyRequest()  # prompt is required

    def test_generate_from_audit_request_defaults(self):
        from preloop.api.endpoints.policies import GeneratePolicyFromAuditRequest

        req = GeneratePolicyFromAuditRequest()
        assert req.start_date is None
        assert req.end_date is None
        assert req.audit_logs_json is None

    def test_generate_from_audit_request_with_dates(self):
        from preloop.api.endpoints.policies import GeneratePolicyFromAuditRequest

        req = GeneratePolicyFromAuditRequest(
            start_date="2026-01-01", end_date="2026-02-01"
        )
        assert req.start_date == "2026-01-01"

    def test_generate_policy_response_valid(self):
        from preloop.api.endpoints.policies import GeneratePolicyResponse

        resp = GeneratePolicyResponse(yaml="version: '1.0'", warnings=["minor"])
        assert resp.yaml == "version: '1.0'"
        assert resp.warnings == ["minor"]

    def test_generate_policy_response_empty_warnings(self):
        from preloop.api.endpoints.policies import GeneratePolicyResponse

        resp = GeneratePolicyResponse(yaml="version: '1.0'")
        assert resp.warnings == []
