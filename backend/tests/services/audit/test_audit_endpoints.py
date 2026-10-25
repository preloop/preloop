"""Tests for audit log API endpoints."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models.crud import crud_audit_log
from preloop.models.models.user import User


@pytest.fixture
def audit_user(test_user: User) -> User:
    """Return test user who already has owner role with view_audit_logs permission."""
    # test_user fixture already has owner role assigned which includes view_audit_logs
    return test_user


class TestAuditLogEndpoints:
    """Test audit log API endpoints."""

    def test_list_audit_logs(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        """Test listing audit logs."""
        # Create some audit logs
        for i in range(5):
            crud_audit_log.log_action(
                db_session,
                account_id=audit_user.account_id,
                user_id=audit_user.id,
                action=f"test_action_{i}",
                status="success",
            )

        response = audit_client.get("/api/v1/audit-logs")
        assert response.status_code == 200
        data = response.json()

        assert "logs" in data
        assert "total" in data
        assert "skip" in data
        assert "limit" in data

        # Filter out automatic permission check logs from RBAC
        test_logs = [log for log in data["logs"] if log["action"] != "permission_check"]
        assert len(test_logs) == 5
        # Total includes the permission check log
        assert data["total"] >= 5

    def test_list_audit_logs_with_action_filter(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        """Test filtering audit logs by action."""
        # Create logs with different actions (use a unique action name to avoid confusion)
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="custom_action",
            status="success",
        )
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="role_assigned",
            status="success",
        )

        response = audit_client.get("/api/v1/audit-logs?action=custom_action")
        assert response.status_code == 200
        data = response.json()

        assert len(data["logs"]) == 1
        assert data["logs"][0]["action"] == "custom_action"

    def test_list_audit_logs_with_status_filter(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        """Test filtering audit logs by status."""
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="permission_check",
            status="success",
        )
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="permission_check",
            status="denied",
        )

        response = audit_client.get("/api/v1/audit-logs?status_filter=denied")
        assert response.status_code == 200
        data = response.json()

        assert len(data["logs"]) == 1
        assert data["logs"][0]["status"] == "denied"

    def test_list_audit_logs_with_pagination(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        """Test pagination of audit logs."""
        # Create 10 logs
        for i in range(10):
            crud_audit_log.log_action(
                db_session,
                account_id=audit_user.account_id,
                user_id=audit_user.id,
                action=f"action_{i}",
                status="success",
            )

        # Get first page
        response = audit_client.get("/api/v1/audit-logs?skip=0&limit=5")
        assert response.status_code == 200
        data = response.json()

        assert len(data["logs"]) == 5
        # Total includes our 10 logs plus the permission check log
        assert data["total"] >= 10
        assert data["skip"] == 0
        assert data["limit"] == 5

        # Get second page
        response = audit_client.get("/api/v1/audit-logs?skip=5&limit=5")
        assert response.status_code == 200
        data = response.json()

        assert len(data["logs"]) == 5
        assert data["total"] >= 10

    def test_list_grouped_audit_logs_includes_session_events_and_tool_metadata(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        """Test grouped timeline includes runtime session events and correlated tool calls."""
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="runtime_session_created",
            resource_type="runtime_session",
            resource_id="runtime-session-1",
            status="created",
            details={
                "runtime_session_id": "runtime-session-1",
                "session_source_type": "claude_code",
                "session_source_id": "workspace-42",
                "session_reference": "claude-session-42",
                "runtime_principal_name": "Claude Workspace",
                "api_key_name": "Claude Workspace Token",
            },
        )
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="tool_call",
            resource_type="tool",
            resource_id="search",
            status="executed",
            details={
                "tool_name": "search",
                "tool_args": {"query": "deployment risk"},
                "correlation_id": "corr-1",
                "runtime_session_id": "runtime-session-1",
                "api_key_name": "Claude Workspace Token",
            },
        )
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="policy_allow",
            resource_type="policy",
            resource_id="search",
            status="allow",
            details={
                "decision": "allow",
                "correlation_id": "corr-1",
                "api_key_name": "Claude Workspace Token",
            },
        )

        response = audit_client.get("/api/v1/audit-logs/grouped")
        assert response.status_code == 200
        data = response.json()

        session_group = next(
            (
                group
                for group in data["groups"]
                if group["primary_event"]["action"] == "runtime_session_created"
            ),
            None,
        )
        tool_group = next(
            (
                group
                for group in data["groups"]
                if group["primary_event"]["action"] == "tool_call"
                and group["primary_event"]["resource_id"] == "search"
            ),
            None,
        )

        assert session_group is not None
        assert (
            session_group["primary_event"]["details"]["api_key_name"]
            == "Claude Workspace Token"
        )
        assert tool_group is not None
        assert (
            tool_group["primary_event"]["details"]["runtime_session_id"]
            == "runtime-session-1"
        )
        assert len(tool_group["sub_events"]) == 1
        assert tool_group["sub_events"][0]["action"] == "policy_allow"

    def test_list_grouped_audit_logs_full_approval_lifecycle(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        """A tool call that required approval should expose its full story.

        The grouped timeline must chain together — by ``correlation_id`` and
        then by ``approval_id`` — the policy decision, the approval request,
        each per-channel notification fan-out, the approval/decline event and
        finally the post-approval tool execution outcome. The group's outcome
        should reflect the *final* tool execution.
        """
        from uuid import uuid4

        approval_id = str(uuid4())
        correlation_id = "corr-pay-50"

        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="tool_call",
            resource_type="tool",
            resource_id="pay",
            status="executed",
            details={
                "tool_name": "pay",
                "tool_args": {"amount": 50, "to": "Jill"},
                "correlation_id": correlation_id,
            },
        )
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="policy_require_approval",
            resource_type="policy",
            resource_id="pay",
            status="require_approval",
            details={
                "decision": "require_approval",
                "correlation_id": correlation_id,
                "rule_description": "Default Rule",
            },
        )
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="approval_created",
            resource_type="approval",
            resource_id=approval_id,
            status="created",
            details={
                "approval_id": approval_id,
                "correlation_id": correlation_id,
                "tool_name": "pay",
                "timeout_seconds": 300,
            },
        )
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=None,
            action="approval_notification_sent",
            resource_type="approval",
            resource_id=approval_id,
            status="sent",
            details={
                "approval_id": approval_id,
                "channel": "email",
                "tool_name": "pay",
                "sent_count": 1,
                "failed_count": 0,
                "skipped_count": 0,
                "recipient_user_ids": [str(audit_user.id)],
                "recipient_count": 1,
            },
        )
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=None,
            action="approval_notification_sent",
            resource_type="approval",
            resource_id=approval_id,
            status="no_devices",
            details={
                "approval_id": approval_id,
                "channel": "mobile_push",
                "tool_name": "pay",
                "sent_count": 0,
                "failed_count": 0,
                "recipient_user_ids": [str(audit_user.id)],
                "recipient_count": 1,
            },
        )
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="approval_approved",
            resource_type="approval",
            resource_id=approval_id,
            status="approved",
            details={
                "approval_id": approval_id,
                "approver_id": str(audit_user.id),
                "reason": "Looks fine",
                "tool_name": "pay",
            },
        )
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=None,
            action="approval_tool_executed",
            resource_type="approval",
            resource_id=approval_id,
            status="executed",
            details={
                "approval_id": approval_id,
                "tool_name": "pay",
                "duration_ms": 234,
                "result_preview": "Paid $50 to Jill",
            },
        )

        response = audit_client.get("/api/v1/audit-logs/grouped?tool_name=pay")
        assert response.status_code == 200
        data = response.json()

        tool_group = next(
            (
                group
                for group in data["groups"]
                if group["primary_event"]["action"] == "tool_call"
                and group["primary_event"]["resource_id"] == "pay"
            ),
            None,
        )
        assert tool_group is not None

        sub_actions = [s["action"] for s in tool_group["sub_events"]]
        # All approval lifecycle steps must be chained into the group.
        assert "policy_require_approval" in sub_actions
        assert "approval_created" in sub_actions
        assert sub_actions.count("approval_notification_sent") == 2
        assert "approval_approved" in sub_actions
        assert "approval_tool_executed" in sub_actions

        # Final outcome should be promoted to the post-approval execution.
        assert tool_group["outcome"] == "executed"

        # Notification rows should expose channel + recipient details.
        notif_subs = [
            s
            for s in tool_group["sub_events"]
            if s["action"] == "approval_notification_sent"
        ]
        channels = {s["details"]["channel"] for s in notif_subs}
        assert channels == {"email", "mobile_push"}
        assert all(
            s["details"]["recipient_user_ids"] == [str(audit_user.id)]
            for s in notif_subs
        )

        # The execution sub-event should expose duration + result preview.
        exec_sub = next(
            s
            for s in tool_group["sub_events"]
            if s["action"] == "approval_tool_executed"
        )
        assert exec_sub["status"] == "executed"
        assert exec_sub["details"]["duration_ms"] == 234
        assert exec_sub["details"]["result_preview"] == "Paid $50 to Jill"

    def test_list_grouped_audit_logs_includes_gateway_failures(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        """Gateway failures should appear as standalone grouped timeline rows."""
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="model_gateway_request",
            resource_type="model_gateway",
            resource_id="openai/gpt-5",
            status="budget_denied",
            details={
                "endpoint": "/openai/v1/responses",
                "endpoint_kind": "responses",
                "status_code": 403,
                "requested_model": "openai/gpt-5",
                "model_alias": "openai/gpt-5",
                "provider_name": "openai",
                "gateway_provider": "preloop",
                "api_key_name": "Claude Workspace Token",
                "runtime_session_id": "runtime-session-1",
                "error_detail": "Model gateway budget exceeded: account monthly limit reached",
                "error_type": "budget_limit_exceeded",
                "budget": {"hard_limit_exceeded": True},
            },
        )

        response = audit_client.get(
            "/api/v1/audit-logs/grouped?event_type=model_gateway_request&outcome=budget_denied"
        )
        assert response.status_code == 200
        data = response.json()

        gateway_group = next(
            (
                group
                for group in data["groups"]
                if group["primary_event"]["action"] == "model_gateway_request"
            ),
            None,
        )
        assert gateway_group is not None
        assert gateway_group["outcome"] == "budget_denied"
        assert (
            gateway_group["primary_event"]["details"]["api_key_name"]
            == "Claude Workspace Token"
        )
        assert gateway_group["primary_event"]["details"]["status_code"] == 403
        assert gateway_group["sub_events"] == []

    def test_get_audit_stats(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        """Test getting audit statistics."""
        # Create various logs
        for _ in range(3):
            crud_audit_log.log_action(
                db_session,
                account_id=audit_user.account_id,
                user_id=audit_user.id,
                action="permission_check",
                status="success",
            )
        for _ in range(2):
            crud_audit_log.log_action(
                db_session,
                account_id=audit_user.account_id,
                user_id=audit_user.id,
                action="permission_check",
                status="denied",
            )

        response = audit_client.get("/api/v1/audit-logs/stats?days=30")
        assert response.status_code == 200
        data = response.json()

        assert "action_stats" in data
        assert "user_activity" in data
        assert "total_count" in data
        assert "period_days" in data
        assert data["period_days"] == 30
        # Total includes our 5 logs plus the permission check log from RBAC
        assert data["total_count"] >= 5

        # Verify action stats
        assert len(data["action_stats"]) > 0
        success_stat = next(
            s
            for s in data["action_stats"]
            if s["action"] == "permission_check" and s["status"] == "success"
        )
        # Should have at least 3 (may have more from RBAC)
        assert success_stat["count"] >= 3

    def test_get_user_audit_logs(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        """Test getting audit logs for a specific user."""
        # Create logs for the user
        for i in range(3):
            crud_audit_log.log_action(
                db_session,
                account_id=audit_user.account_id,
                user_id=audit_user.id,
                action=f"user_action_{i}",
                status="success",
            )

        response = audit_client.get(f"/api/v1/audit-logs/user/{audit_user.id}?days=30")
        assert response.status_code == 200
        data = response.json()

        # Filter out automatic permission check logs from RBAC
        user_action_logs = [
            log for log in data["logs"] if log["action"].startswith("user_action_")
        ]
        assert len(user_action_logs) == 3
        assert all(log["user_id"] == str(audit_user.id) for log in data["logs"])

    def test_get_permission_denials(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        """Test getting permission denial events."""
        # Create permission checks
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="permission_check",
            status="success",
        )
        for _ in range(2):
            crud_audit_log.log_action(
                db_session,
                account_id=audit_user.account_id,
                user_id=audit_user.id,
                action="permission_check",
                status="denied",
            )

        response = audit_client.get("/api/v1/audit-logs/denials?days=7")
        assert response.status_code == 200
        data = response.json()

        assert len(data["logs"]) == 2
        assert all(log["status"] == "denied" for log in data["logs"])
        assert all(log["action"] == "permission_check" for log in data["logs"])

    def test_list_audit_logs_requires_permission(self, audit_client: TestClient):
        """The core account authorizer can deny the audit surface."""
        from preloop.plugins import account_hooks

        previous = account_hooks.get_authorizer()
        account_hooks.register_authorizer(
            lambda ctx, action, resource: account_hooks.Decision("deny")
        )
        try:
            assert audit_client.get("/api/v1/audit-logs").status_code == 403
        finally:
            account_hooks.register_authorizer(previous)

    def test_account_isolation_in_audit_logs(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        """Test that users can only see audit logs from their own account."""
        from preloop.models.crud import crud_account

        # Create another account
        other_account_data = {"organization_name": "Other Org", "is_active": True}
        other_account = crud_account.create(db_session, obj_in=other_account_data)

        # Create logs for both accounts
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="my_account_action",
            status="success",
        )
        crud_audit_log.log_action(
            db_session,
            account_id=other_account.id,
            user_id=audit_user.id,
            action="other_account_action",
            status="success",
        )

        response = audit_client.get("/api/v1/audit-logs")
        assert response.status_code == 200
        data = response.json()

        # Should only see logs from own account (plus the permission check log from RBAC)
        # Filter to get only our test logs
        my_account_logs = [
            log for log in data["logs"] if log["action"] == "my_account_action"
        ]
        other_account_logs = [
            log for log in data["logs"] if log["action"] == "other_account_action"
        ]

        assert len(my_account_logs) == 1
        assert len(other_account_logs) == 0
        assert my_account_logs[0]["action"] == "my_account_action"

    def test_audit_log_response_format(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        """Test that audit log response has correct format."""
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="test_action",
            resource_type="issue",
            resource_id="123",
            status="success",
            ip_address="192.168.1.1",
            user_agent="Mozilla/5.0",
            details={"key": "value"},
        )

        response = audit_client.get("/api/v1/audit-logs")
        assert response.status_code == 200
        data = response.json()

        # Find the test_action log (filter out permission_check logs from RBAC)
        test_log = next(
            (log for log in data["logs"] if log["action"] == "test_action"), None
        )
        assert test_log is not None, "test_action log not found"

        log = test_log
        assert "id" in log
        assert "account_id" in log
        assert "user_id" in log
        assert "action" in log
        assert "resource_type" in log
        assert "resource_id" in log
        assert "status" in log
        assert "ip_address" in log
        assert "user_agent" in log
        assert "details" in log
        assert "timestamp" in log

        assert log["action"] == "test_action"
        assert log["resource_type"] == "issue"
        assert log["resource_id"] == "123"
        assert log["status"] == "success"
        assert log["ip_address"] == "192.168.1.1"
        assert log["details"]["key"] == "value"


def endpoints_core_matches_any() -> bool:
    import inspect

    return (
        "outcome_match"
        in inspect.signature(crud_audit_log.get_grouped_by_correlation).parameters
    )


class TestAuditOverviewEndpoint:
    """The Overview's audit figures and exceptions, in one call."""

    def _tool_call(self, db_session: Session, user: User, cid: str, status: str):
        crud_audit_log.log_action(
            db_session,
            account_id=user.account_id,
            user_id=user.id,
            action="tool_call",
            resource_type="tool",
            resource_id="bash",
            status=status,
            details={"tool_name": "bash", "correlation_id": cid},
        )

    def test_overview_matches_the_grouped_figures(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        self._tool_call(db_session, audit_user, "c-ok", "success")
        self._tool_call(db_session, audit_user, "c-bad", "failed")
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="model_gateway_request",
            resource_type="model",
            resource_id="m",
            status="budget_denied",
            details={"model": "m-1"},
        )

        overview = audit_client.get("/api/v1/audit-logs/overview").json()

        def grouped_total(query: str) -> int:
            return audit_client.get(f"/api/v1/audit-logs/grouped?{query}").json()[
                "total"
            ]

        assert overview["tool_calls_total"] == grouped_total(
            "event_type=tool_call&limit=1"
        )
        assert overview["tool_calls_total"] == 2
        assert overview["tool_calls_failed"] == 1
        assert overview["events_total"] >= 3

    def test_exceptions_list_either_outcome(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        """The card means failed OR budget denied (#1604), newest first."""
        self._tool_call(db_session, audit_user, "c-bad", "failed")
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="model_gateway_request",
            resource_type="model",
            resource_id="m",
            status="budget_denied",
            details={"model": "m-1"},
        )
        self._tool_call(db_session, audit_user, "c-ok", "success")

        response = audit_client.get("/api/v1/audit-logs/overview?exceptions_limit=5")
        assert response.status_code == 200
        exceptions = response.json()["exceptions"]
        outcomes = [group["outcome"] for group in exceptions["groups"]]
        assert sorted(outcomes) == ["budget_denied", "failed"]
        assert exceptions["total"] == 2
        timestamps = [g["primary_event"]["timestamp"] for g in exceptions["groups"]]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_a_chain_with_both_outcomes_is_listed_and_counted_once(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        """A failed tool call later denied for budget is one exception."""
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="tool_call",
            resource_type="tool",
            resource_id="bash",
            status="failed",
            details={"tool_name": "bash", "correlation_id": "c-both"},
        )
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="policy_deny",
            resource_type="policy",
            resource_id="bash",
            status="budget_denied",
            details={"decision": "budget_denied", "correlation_id": "c-both"},
        )

        exceptions = audit_client.get("/api/v1/audit-logs/overview").json()[
            "exceptions"
        ]

        both = [
            group
            for group in exceptions["groups"]
            if group["correlation_id"] == "c-both"
        ]
        assert len(both) == 1
        assert exceptions["total"] == len(exceptions["groups"])

    def test_exceptions_are_failures_and_denials_with_their_own_outcome(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        """An allowed call that then failed reads "failed", not "allow"."""
        if not endpoints_core_matches_any():
            pytest.skip("core predates outcome_match")
        for action, status, details in (
            ("tool_call", "failed", {"correlation_id": "c-f"}),
            ("policy_allow", "allow", {"correlation_id": "c-f", "decision": "allow"}),
            ("tool_call", "executed", {"correlation_id": "c-ok"}),
            ("policy_allow", "allow", {"correlation_id": "c-ok", "decision": "allow"}),
            ("policy_deny", "deny", {"correlation_id": "c-d", "decision": "deny"}),
        ):
            crud_audit_log.log_action(
                db_session,
                account_id=audit_user.account_id,
                user_id=audit_user.id,
                action=action,
                resource_type="tool",
                resource_id="update_pr",
                status=status,
                details={"tool_name": "update_pr", **details},
            )

        exceptions = audit_client.get("/api/v1/audit-logs/overview").json()[
            "exceptions"
        ]

        by_cid = {g["correlation_id"]: g["outcome"] for g in exceptions["groups"]}
        assert by_cid == {"c-f": "failed", "c-d": "deny"}
        assert exceptions["total"] == 2

        from datetime import datetime, timedelta, timezone

        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        later = audit_client.get(
            "/api/v1/audit-logs/overview", params={"start_date": future}
        ).json()["exceptions"]
        assert later == {**later, "groups": [], "total": 0}

    def test_grouped_any_outcome_is_an_or(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        if not endpoints_core_matches_any():
            pytest.skip("core predates outcome_match")
        self._tool_call(db_session, audit_user, "c-bad", "failed")
        crud_audit_log.log_action(
            db_session,
            account_id=audit_user.account_id,
            user_id=audit_user.id,
            action="model_gateway_request",
            resource_type="model",
            resource_id="m",
            status="budget_denied",
            details={"model": "m-1"},
        )
        query = "outcome=failed&outcome=budget_denied"
        both = audit_client.get(f"/api/v1/audit-logs/grouped?{query}").json()
        either = audit_client.get(
            f"/api/v1/audit-logs/grouped?{query}&outcome_match=any"
        ).json()
        assert both["total"] == 0
        assert sorted(g["outcome"] for g in either["groups"]) == [
            "budget_denied",
            "failed",
        ]
        bad = audit_client.get(f"/api/v1/audit-logs/grouped?{query}&outcome_match=x")
        assert bad.status_code == 422

    def test_events_total_counts_only_the_window(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        from datetime import datetime, timedelta, timezone

        self._tool_call(db_session, audit_user, "c-now", "success")
        start = datetime.now(timezone.utc) - timedelta(minutes=5)
        everything = audit_client.get("/api/v1/audit-logs/overview").json()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        windowed = audit_client.get(
            "/api/v1/audit-logs/overview", params={"start_date": future}
        ).json()
        recent = audit_client.get(
            "/api/v1/audit-logs/overview", params={"start_date": start.isoformat()}
        ).json()

        assert windowed["events_total"] == 0
        assert recent["events_total"] >= 1
        assert everything["events_total"] >= recent["events_total"]
        assert windowed["events_total_capped"] is False

    def test_events_total_stops_at_the_count_cap(
        self,
        audit_client: TestClient,
        db_session: Session,
        audit_user: User,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Three groups with the cap at two report two and say so (#1592).

        The assertion is the capped total, not a timer: the read passes
        ``count_limit`` and stops instead of counting the whole trail.
        """
        import inspect

        if (
            "count_limit"
            not in inspect.signature(
                crud_audit_log.get_grouped_by_correlation
            ).parameters
        ):
            pytest.skip("core predates count_limit")
        try:
            from preloop.api.endpoints import audit as audit_endpoints
        except ImportError:
            from preloop.api.endpoints import audit as audit_endpoints

        monkeypatch.setattr(audit_endpoints, "OVERVIEW_COUNT_LIMIT", 2)
        for index in range(3):
            self._tool_call(db_session, audit_user, f"c-cap-{index}", "success")

        overview = audit_client.get("/api/v1/audit-logs/overview").json()
        grouped = audit_client.get(
            "/api/v1/audit-logs/grouped",
            params={"limit": 1, "count_limit": 2},
        ).json()

        assert overview["events_total"] == 2
        assert overview["events_total_capped"] is True
        assert (grouped["total"], grouped["total_capped"]) == (2, True)

    def test_tool_call_figures_follow_the_window(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        """Every figure covers start_date onwards, like events_total (#1592)."""
        from datetime import datetime, timedelta, timezone

        from preloop.models.models.audit_log import AuditLog

        self._tool_call(db_session, audit_user, "c-new-ok", "success")
        self._tool_call(db_session, audit_user, "c-new-bad", "failed")
        self._tool_call(db_session, audit_user, "c-old-bad", "failed")
        old = (
            db_session.query(AuditLog)
            .filter(AuditLog.details["correlation_id"].astext == "c-old-bad")
            .one()
        )
        old.timestamp = datetime.now(timezone.utc) - timedelta(days=40)
        db_session.flush()
        start = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

        windowed = audit_client.get(
            "/api/v1/audit-logs/overview", params={"start_date": start}
        ).json()
        grouped = audit_client.get(
            "/api/v1/audit-logs/grouped",
            params={"event_type": "tool_call", "limit": 1, "start_date": start},
        ).json()

        assert windowed["tool_calls_total"] == grouped["total"] == 2
        assert windowed["tool_calls_failed"] == 1

    def test_tool_calls_total_matches_grouped_with_a_shared_correlation(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        """Grouped by tool call, every tool_call row is its own group.

        Two calls sharing a correlation_id are two rows on the Audit page,
        so a row count is the same figure.
        """
        self._tool_call(db_session, audit_user, "c-shared", "success")
        self._tool_call(db_session, audit_user, "c-shared", "failed")

        overview = audit_client.get("/api/v1/audit-logs/overview").json()
        grouped = audit_client.get(
            "/api/v1/audit-logs/grouped",
            params={"event_type": "tool_call", "limit": 1},
        ).json()

        assert overview["tool_calls_total"] == grouped["total"] == 2


class TestGroupedCountLimit:
    """The Audit page asks for a capped total (#1592)."""

    def test_capped_total_on_the_real_core(
        self, audit_client: TestClient, db_session: Session, audit_user: User
    ):
        if (
            "count_limit"
            not in __import__("inspect")
            .signature(crud_audit_log.get_grouped_by_correlation)
            .parameters
        ):
            pytest.skip("core predates count_limit")
        for index in range(3):
            crud_audit_log.log_action(
                db_session,
                account_id=audit_user.account_id,
                user_id=audit_user.id,
                action="tool_call",
                resource_type="tool",
                resource_id="bash",
                status="success",
                details={"correlation_id": f"c-{index}"},
            )
        query = "/api/v1/audit-logs/grouped?event_type=tool_call&limit=1"
        capped = audit_client.get(f"{query}&count_limit=2").json()
        exact = audit_client.get(f"{query}&count_limit=3").json()
        plain = audit_client.get(query).json()
        assert (capped["total"], capped["total_capped"]) == (2, True)
        assert (exact["total"], exact["total_capped"]) == (3, False)
        assert (plain["total"], plain["total_capped"]) == (3, False)
        assert len(capped["groups"]) == 1
        bad = audit_client.get(f"{query}&count_limit=0")
        assert bad.status_code == 422


def test_focused_entry_is_account_scoped(
    audit_client: TestClient, db_session: Session, audit_user: User
) -> None:
    """Deep links fetch the precise entry and hide foreign ids."""
    from preloop.models.crud import crud_account

    entry = crud_audit_log.log_action(
        db_session,
        account_id=audit_user.account_id,
        user_id=audit_user.id,
        action="focused_example",
        status="success",
    )
    response = audit_client.get(f"/api/v1/audit-logs/{entry.id}")
    assert response.status_code == 200
    assert response.json()["id"] == str(entry.id)
    other = crud_account.create(db_session, obj_in={"organization_name": "Other"})
    foreign = crud_audit_log.log_action(
        db_session, account_id=other.id, action="foreign_example", status="success"
    )
    assert audit_client.get(f"/api/v1/audit-logs/{foreign.id}").status_code == 404
