"""Tests for notification payload builder."""

from datetime import datetime, UTC

from preloop.services.push_notifications.notification_payloads import (
    NotificationPayloadBuilder,
)


class TestNewApprovalRequestPayload:
    """Test new_approval_request payload generation."""

    def test_basic_payload_structure(self):
        """Test basic payload structure for new approval request."""
        payload = NotificationPayloadBuilder.new_approval_request(
            request_id="123e4567-e89b-12d3-a456-426614174000",
            tool_name="create_issue",
            priority="medium",
        )

        assert "aps" in payload
        assert "alert" in payload["aps"]
        assert "title" in payload["aps"]["alert"]
        assert "subtitle" in payload["aps"]["alert"]
        assert "body" in payload["aps"]["alert"]
        assert payload["aps"]["sound"] == "default"
        assert payload["aps"]["badge"] == 1
        assert payload["aps"]["category"] == "APPROVAL_REQUEST"
        assert payload["aps"]["thread-id"] == "approval-requests"

        # Custom data
        assert payload["type"] == "new_approval_request"
        assert payload["approval_request_id"] == "123e4567-e89b-12d3-a456-426614174000"
        assert payload["tool_name"] == "create_issue"
        assert payload["priority"] == "medium"
        assert payload["risk_level"] == "danger"
        assert payload["aps"]["category"] == "APPROVAL_REQUEST"

    def test_names_the_agent_in_the_subtitle_and_the_data(self):
        """A push is decided on a phone, so who asked travels with it."""
        payload = NotificationPayloadBuilder.new_approval_request(
            request_id="test-id",
            tool_name="force_push",
            tool_args={},
            agent_name="Claude Code (laptop)",
        )

        assert (
            payload["aps"]["alert"]["subtitle"] == "Force Push · Claude Code (laptop)"
        )
        # The fallback body names the agent instead of "AI agent".
        assert (
            payload["aps"]["alert"]["body"]
            == "Claude Code (laptop) needs approval for Force Push"
        )
        assert payload["agent_name"] == "Claude Code (laptop)"
        assert payload["data"]["agent_name"] == "Claude Code (laptop)"

    def test_says_ai_agent_only_when_no_agent_is_known(self):
        """An unattributed request reads exactly as it did before."""
        payload = NotificationPayloadBuilder.new_approval_request(
            request_id="test-id",
            tool_name="force_push",
            tool_args={},
        )

        assert payload["aps"]["alert"]["subtitle"] == "Force Push"
        assert (
            payload["aps"]["alert"]["body"] == "AI agent needs approval for Force Push"
        )
        assert "agent_name" not in payload

    def test_summary_keeps_the_body_and_still_names_the_agent(self):
        """The ask owns the body; the agent rides in the subtitle."""
        payload = NotificationPayloadBuilder.new_approval_request(
            request_id="test-id",
            tool_name="force_push",
            summary="Allow the coding agent to force-push branch main?",
            agent_name="Release Bot",
        )

        assert (
            payload["aps"]["alert"]["body"]
            == "Allow the coding agent to force-push branch main?"
        )
        assert payload["aps"]["alert"]["subtitle"] == "Force Push · Release Bot"
        assert payload["agent_name"] == "Release Bot"

    def test_low_risk_read_uses_low_risk_category(self):
        payload = NotificationPayloadBuilder.new_approval_request(
            request_id="read-id",
            tool_name="read",
            tool_args={"path": "README.md"},
        )
        assert payload["risk_level"] == "low"
        assert payload["aps"]["category"] == "LOW_RISK_APPROVAL"
        assert payload["data"]["risk_level"] == "low"

    def test_summary_becomes_notification_body(self):
        """Plain-language summary is the primary body; tool name is subtitle."""
        payload = NotificationPayloadBuilder.new_approval_request(
            request_id="test-id",
            tool_name="force_push",
            summary="Allow the coding agent to force-push branch main?",
            tool_args={"branch": "main"},
            agent_reasoning="Need to update prod",
        )

        assert (
            payload["aps"]["alert"]["body"]
            == "Allow the coding agent to force-push branch main?"
        )
        assert payload["aps"]["alert"]["subtitle"] == "Force Push"
        assert payload["summary"] == "Allow the coding agent to force-push branch main?"
        assert (
            payload["data"]["body"]
            == "Allow the coding agent to force-push branch main?"
        )
        assert (
            payload["data"]["summary"]
            == "Allow the coding agent to force-push branch main?"
        )

    def test_urgent_priority_notification(self):
        """Test urgent priority produces correct alert and sound."""
        payload = NotificationPayloadBuilder.new_approval_request(
            request_id="test-id", tool_name="delete_database", priority="urgent"
        )

        assert payload["aps"]["alert"]["title"] == "🚨 URGENT: Approval Needed"
        assert payload["aps"]["sound"] == "critical.caf"
        assert payload["aps"]["interruption-level"] == "critical"
        assert payload["aps"]["relevance-score"] == 1.0

    def test_high_priority_notification(self):
        """Test high priority produces correct alert."""
        payload = NotificationPayloadBuilder.new_approval_request(
            request_id="test-id", tool_name="modify_production", priority="high"
        )

        assert payload["aps"]["alert"]["title"] == "⚠️ High Priority Approval"
        assert payload["aps"]["sound"] == "default"
        assert payload["aps"]["interruption-level"] == "time-sensitive"
        assert payload["aps"]["relevance-score"] == 1.0

    def test_medium_priority_notification(self):
        """Test medium priority produces correct alert."""
        payload = NotificationPayloadBuilder.new_approval_request(
            request_id="test-id", tool_name="create_issue", priority="medium"
        )

        assert payload["aps"]["alert"]["title"] == "New Approval Request"
        assert payload["aps"]["sound"] == "default"
        assert payload["aps"]["interruption-level"] == "active"
        assert payload["aps"]["relevance-score"] == 0.5

    def test_low_priority_notification(self):
        """Test low priority produces correct alert."""
        payload = NotificationPayloadBuilder.new_approval_request(
            request_id="test-id", tool_name="add_comment", priority="low"
        )

        assert payload["aps"]["alert"]["title"] == "New Approval Request"
        assert payload["aps"]["interruption-level"] == "active"
        assert payload["aps"]["relevance-score"] == 0.5

    def test_tool_name_formatting(self):
        """Test tool name is formatted nicely in subtitle and body."""
        payload = NotificationPayloadBuilder.new_approval_request(
            request_id="test-id", tool_name="create_github_issue", priority="medium"
        )

        # Tool name should be formatted: underscores -> spaces, title case
        assert payload["aps"]["alert"]["subtitle"] == "Create Github Issue"
        assert "Create Github Issue" in payload["aps"]["alert"]["body"]

    def test_with_agent_reasoning(self):
        """Test payload includes agent reasoning in body."""
        reasoning = "This issue needs to be created to track the bug"
        payload = NotificationPayloadBuilder.new_approval_request(
            request_id="test-id",
            tool_name="create_issue",
            priority="medium",
            agent_reasoning=reasoning,
        )

        assert reasoning in payload["aps"]["alert"]["body"]

    def test_agent_reasoning_truncation(self):
        """Test agent reasoning is truncated to 100 chars."""
        long_reasoning = "A" * 150
        payload = NotificationPayloadBuilder.new_approval_request(
            request_id="test-id",
            tool_name="create_issue",
            priority="medium",
            agent_reasoning=long_reasoning,
        )

        # Should be truncated with ellipsis
        body = payload["aps"]["alert"]["body"]
        assert len(body) < len(long_reasoning) + 50  # Accounting for prefix text
        assert "..." in body

    def test_with_expires_at(self):
        """Test payload includes expiration timestamp."""
        expires_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        payload = NotificationPayloadBuilder.new_approval_request(
            request_id="test-id",
            tool_name="create_issue",
            priority="medium",
            expires_at=expires_at,
        )

        assert "expires_at" in payload
        assert payload["expires_at"] == "2025-01-01T12:00:00+00:00"

    def test_without_expires_at(self):
        """Test payload works without expiration timestamp."""
        payload = NotificationPayloadBuilder.new_approval_request(
            request_id="test-id", tool_name="create_issue", priority="medium"
        )

        assert "expires_at" not in payload


class TestRequestExpiringPayload:
    """Test request_expiring_soon payload generation."""

    def test_basic_structure(self):
        """Test basic structure of expiring request payload."""
        payload = NotificationPayloadBuilder.request_expiring_soon(
            request_id="test-id", tool_name="create_issue", minutes_remaining=15
        )

        assert payload["aps"]["alert"]["title"] == "⏰ Request Expiring Soon"
        assert "Create Issue" in payload["aps"]["alert"]["subtitle"]
        assert "expires in 15 minutes" in payload["aps"]["alert"]["body"]
        assert payload["aps"]["sound"] == "default"
        assert payload["aps"]["badge"] == 1
        assert payload["aps"]["category"] == "APPROVAL_REQUEST"
        assert payload["aps"]["interruption-level"] == "time-sensitive"

        assert payload["type"] == "request_expiring_soon"
        assert payload["approval_request_id"] == "test-id"
        assert payload["tool_name"] == "create_issue"
        assert payload["minutes_remaining"] == 15

    def test_minutes_remaining_formatting(self):
        """Test different minute values are formatted correctly."""
        payload_1 = NotificationPayloadBuilder.request_expiring_soon(
            request_id="test-id", tool_name="create_issue", minutes_remaining=1
        )
        assert "expires in 1 minutes" in payload_1["aps"]["alert"]["body"]

        payload_30 = NotificationPayloadBuilder.request_expiring_soon(
            request_id="test-id", tool_name="create_issue", minutes_remaining=30
        )
        assert "expires in 30 minutes" in payload_30["aps"]["alert"]["body"]


class TestRequestExpiredPayload:
    """Test request_expired payload generation."""

    def test_basic_structure(self):
        """Test basic structure of expired request payload."""
        payload = NotificationPayloadBuilder.request_expired(
            request_id="test-id", tool_name="create_issue"
        )

        assert payload["aps"]["alert"]["title"] == "Request Expired"
        assert "Create Issue" in payload["aps"]["alert"]["subtitle"]
        assert payload["aps"]["alert"]["body"] == "An approval request has expired"
        assert payload["aps"]["sound"] == "default"
        assert payload["aps"]["badge"] == 0  # Clear badge

        assert payload["type"] == "request_expired"
        assert payload["approval_request_id"] == "test-id"
        assert payload["tool_name"] == "create_issue"

    def test_badge_cleared(self):
        """Test that badge is cleared (set to 0) for expired requests."""
        payload = NotificationPayloadBuilder.request_expired(
            request_id="test-id", tool_name="create_issue"
        )

        assert payload["aps"]["badge"] == 0


class TestRequestResolvedPayload:
    """Test request_resolved payload generation."""

    def test_approved_notification(self):
        """Test resolved payload for approved request."""
        payload = NotificationPayloadBuilder.request_resolved(
            request_id="test-id",
            tool_name="create_issue",
            resolved_by="john@example.com",
            decision="approved",
        )

        assert payload["aps"]["alert"]["title"] == "Request Resolved"
        assert "Create Issue" in payload["aps"]["alert"]["subtitle"]
        assert (
            "john@example.com approved this request" in payload["aps"]["alert"]["body"]
        )
        assert payload["aps"]["sound"] == "default"
        assert payload["aps"]["badge"] == 0

        assert payload["type"] == "request_resolved"
        assert payload["approval_request_id"] == "test-id"
        assert payload["tool_name"] == "create_issue"
        assert payload["resolved_by"] == "john@example.com"
        assert payload["decision"] == "approved"

    def test_declined_notification(self):
        """Test resolved payload for declined request."""
        payload = NotificationPayloadBuilder.request_resolved(
            request_id="test-id",
            tool_name="delete_database",
            resolved_by="admin@example.com",
            decision="declined",
        )

        assert (
            "admin@example.com declined this request" in payload["aps"]["alert"]["body"]
        )
        assert payload["decision"] == "declined"

    def test_badge_cleared_on_resolution(self):
        """Test that badge is cleared when request is resolved."""
        payload = NotificationPayloadBuilder.request_resolved(
            request_id="test-id",
            tool_name="create_issue",
            resolved_by="user@example.com",
            decision="approved",
        )

        assert payload["aps"]["badge"] == 0


class TestQuestionPayloads:
    """`ask_user` questions must be pushed as QUESTIONS, not approvals.

    Clients branch on `is_question` / the aps category to show option buttons
    and an inline dictated answer instead of Approve/Decline.
    """

    def _question_payload(self, options=None, allow_free_text=True):
        from preloop.services.push_notifications.notification_payloads import (
            NotificationPayloadBuilder,
        )

        return NotificationPayloadBuilder.new_approval_request(
            request_id="req-1",
            tool_name="ask_user",
            tool_args={
                "is_question": True,
                "question": "Which rollout color?",
                "options": options if options is not None else ["blue", "green"],
                "allow_free_text": allow_free_text,
            },
            summary="Which rollout color?",
        )

    def test_question_uses_question_category_and_title(self):
        payload = self._question_payload()
        assert payload["aps"]["category"] == "QUESTION_2_OPTIONS"
        assert payload["aps"]["alert"]["title"] == "Agent question"
        assert payload["aps"]["alert"]["body"] == "Which rollout color?"

    def test_question_carries_options_and_free_text_flag(self):
        payload = self._question_payload()
        assert payload["is_question"] is True
        assert payload["question_options"] == ["blue", "green"]
        assert payload["allow_free_text"] is True
        assert payload["type"] == "question"
        # Android/FCM reads the same fields from the data block.
        assert payload["data"]["question_options"] == ["blue", "green"]
        assert payload["data"]["is_question"] is True

    def test_option_arity_selects_category(self):
        assert self._question_payload(["a", "b", "c"])["aps"]["category"] == (
            "QUESTION_3_OPTIONS"
        )
        assert self._question_payload(["a", "b", "c", "d"])["aps"]["category"] == (
            "QUESTION_4_OPTIONS"
        )
        # iOS shows at most 4 actions: beyond that clients open the app or use
        # the inline text answer.
        assert (
            self._question_payload(["a", "b", "c", "d", "e"])["aps"]["category"]
            == "QUESTION_REQUEST"
        )
        # Free-text-only question (no options).
        assert self._question_payload([])["aps"]["category"] == "QUESTION_REQUEST"

    def test_free_text_disabled_is_reported(self):
        payload = self._question_payload(allow_free_text=False)
        assert payload["allow_free_text"] is False

    def test_approval_payload_unchanged(self):
        from preloop.services.push_notifications.notification_payloads import (
            NotificationPayloadBuilder,
        )

        payload = NotificationPayloadBuilder.new_approval_request(
            request_id="req-2",
            tool_name="Bash",
            tool_args={"command": "rm -rf /tmp/x"},
        )
        assert payload["aps"]["category"] == "APPROVAL_REQUEST"
        assert payload["aps"]["alert"]["title"] == "New Approval Request"
        assert payload["type"] == "new_approval_request"
        assert "is_question" not in payload
        assert "question_options" not in payload
