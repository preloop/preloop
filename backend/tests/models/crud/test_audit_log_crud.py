"""Tests for audit log CRUD operations."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.models.crud import crud_audit_log, crud_account, crud_user
from preloop.models.crud.audit_log import _narrow_audit_rows
from preloop.models.models.audit_log import AuditLog


@pytest.fixture
def test_account(db_session: Session):
    """Create a test account."""
    account_data = {
        "organization_name": "Audit Test Org",
        "is_active": True,
    }
    return crud_account.create(db_session, obj_in=account_data)


@pytest.fixture
def test_user_for_audit(db_session: Session, test_account):
    """Create a test user for audit logging."""
    user_data = {
        "account_id": test_account.id,
        "email": "audituser@example.com",
        "username": "audituser",
        "full_name": "Audit User",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    return crud_user.create(db_session, obj_in=user_data)


class TestAuditLogCRUD:
    """Test audit log CRUD operations."""

    def test_log_action_with_string_account_id(
        self, db_session: Session, test_account, test_user_for_audit
    ):
        """Test logging an action with string account ID."""
        log = crud_audit_log.log_action(
            db_session,
            account_id=test_account.id,
            user_id=test_user_for_audit.id,
            action="permission_check",
            resource_type="issue",
            resource_id="123",
            status="success",
            ip_address="192.168.1.1",
            user_agent="Mozilla/5.0",
            details={"permission": "view_issues", "granted": True},
        )

        assert log.id is not None
        assert log.account_id == test_account.id
        assert log.user_id == test_user_for_audit.id
        assert log.action == "permission_check"
        assert log.resource_type == "issue"
        assert log.resource_id == "123"
        assert log.status == "success"
        assert log.ip_address == "192.168.1.1"
        assert log.user_agent == "Mozilla/5.0"
        assert log.details["permission"] == "view_issues"
        assert log.details["granted"] is True

    def test_log_action_uses_audit_event_id_as_row_id(
        self, db_session: Session, test_account, test_user_for_audit
    ):
        """A policy hit and its audit row share the id carried in details."""
        event_id = uuid4()
        log = crud_audit_log.log_action(
            db_session,
            account_id=test_account.id,
            user_id=test_user_for_audit.id,
            action="policy_deny",
            resource_type="policy",
            resource_id="model.request",
            status="deny",
            details={"audit_event_id": str(event_id), "rule_id": "deny-secret"},
        )
        assert log.id == event_id
        assert log.details["audit_event_id"] == str(event_id)
        assert log.timestamp is not None

    def test_log_action_with_uuid_account_id(
        self, db_session: Session, test_account, test_user_for_audit
    ):
        """Test logging an action with UUID account ID."""
        # test_account.id is already a UUID object
        account_uuid = test_account.id

        log = crud_audit_log.log_action(
            db_session,
            account_id=account_uuid,
            user_id=test_user_for_audit.id,
            action="role_assigned",
            status="success",
        )

        assert log.account_id == test_account.id  # Should be stored as UUID
        assert log.action == "role_assigned"

    def test_log_action_without_user(self, db_session: Session, test_account):
        """Test logging a system action without a user."""
        log = crud_audit_log.log_action(
            db_session,
            account_id=test_account.id,
            user_id=None,
            action="system_maintenance",
            status="success",
        )

        assert log.user_id is None
        assert log.action == "system_maintenance"

    def test_get_by_account(
        self, db_session: Session, test_account, test_user_for_audit
    ):
        """Test retrieving audit logs by account."""
        # Create multiple logs
        for i in range(5):
            crud_audit_log.log_action(
                db_session,
                account_id=test_account.id,
                user_id=test_user_for_audit.id,
                action=f"action_{i}",
                status="success",
            )

        logs = crud_audit_log.get_by_account(
            db_session, account_id=test_account.id, skip=0, limit=10
        )

        assert len(logs) == 5
        # Should be ordered by timestamp descending
        assert logs[0].action == "action_4"
        assert logs[4].action == "action_0"

    def test_get_by_account_with_filters(
        self, db_session: Session, test_account, test_user_for_audit
    ):
        """Test retrieving audit logs with filters."""
        # Create logs with different actions and statuses
        crud_audit_log.log_action(
            db_session,
            account_id=test_account.id,
            user_id=test_user_for_audit.id,
            action="permission_check",
            status="success",
        )
        crud_audit_log.log_action(
            db_session,
            account_id=test_account.id,
            user_id=test_user_for_audit.id,
            action="permission_check",
            status="denied",
        )
        crud_audit_log.log_action(
            db_session,
            account_id=test_account.id,
            user_id=test_user_for_audit.id,
            action="role_assigned",
            status="success",
        )

        # Filter by action
        logs = crud_audit_log.get_by_account(
            db_session, account_id=test_account.id, action="permission_check"
        )
        assert len(logs) == 2

        # Filter by status
        logs = crud_audit_log.get_by_account(
            db_session, account_id=test_account.id, status="denied"
        )
        assert len(logs) == 1
        assert logs[0].status == "denied"

        # Filter by both
        logs = crud_audit_log.get_by_account(
            db_session,
            account_id=test_account.id,
            action="permission_check",
            status="success",
        )
        assert len(logs) == 1

    def test_get_by_account_filters_by_resource_id(
        self, db_session: Session, test_account, test_user_for_audit
    ):
        """A resource id narrows the account's rows to one resource."""
        for resource_id in ("flow-1", "flow-2", "flow-2"):
            crud_audit_log.log_action(
                db_session,
                account_id=test_account.id,
                user_id=test_user_for_audit.id,
                action="flow_failure_streak_alert",
                resource_type="flow",
                resource_id=resource_id,
                status="success",
            )

        logs = crud_audit_log.get_by_account(
            db_session,
            account_id=test_account.id,
            action="flow_failure_streak_alert",
            resource_type="flow",
            resource_id="flow-2",
        )
        assert len(logs) == 2
        assert all(log.resource_id == "flow-2" for log in logs)

    def test_get_by_account_with_date_filters(
        self, db_session: Session, test_account, test_user_for_audit
    ):
        """Test retrieving audit logs with date filters."""
        now = datetime.now(timezone.utc)

        # Create a log
        log = crud_audit_log.log_action(
            db_session,
            account_id=test_account.id,
            user_id=test_user_for_audit.id,
            action="test_action",
            status="success",
        )

        assert log.id is not None

        # Filter by start date (should include)
        logs = crud_audit_log.get_by_account(
            db_session,
            account_id=test_account.id,
            start_date=now - timedelta(hours=1),
        )
        assert len(logs) == 1

        # Filter by start date (should exclude)
        logs = crud_audit_log.get_by_account(
            db_session, account_id=test_account.id, start_date=now + timedelta(hours=1)
        )
        assert len(logs) == 0

    def test_get_by_user(self, db_session: Session, test_account, test_user_for_audit):
        """Test retrieving audit logs by user."""
        # Create logs for the user
        for i in range(3):
            crud_audit_log.log_action(
                db_session,
                account_id=test_account.id,
                user_id=test_user_for_audit.id,
                action=f"user_action_{i}",
                status="success",
            )

        logs = crud_audit_log.get_by_user(
            db_session,
            user_id=test_user_for_audit.id,
            account_id=test_account.id,
            days=30,
        )

        assert len(logs) == 3

    def test_get_permission_denials(
        self, db_session: Session, test_account, test_user_for_audit
    ):
        """Test retrieving permission denial events."""
        # Create some permission checks
        crud_audit_log.log_action(
            db_session,
            account_id=test_account.id,
            user_id=test_user_for_audit.id,
            action="permission_check",
            status="success",
        )
        crud_audit_log.log_action(
            db_session,
            account_id=test_account.id,
            user_id=test_user_for_audit.id,
            action="permission_check",
            status="denied",
        )
        crud_audit_log.log_action(
            db_session,
            account_id=test_account.id,
            user_id=test_user_for_audit.id,
            action="permission_check",
            status="denied",
        )

        denials = crud_audit_log.get_permission_denials(
            db_session, account_id=test_account.id, days=7
        )

        assert len(denials) == 2
        assert all(d.status == "denied" for d in denials)
        assert all(d.action == "permission_check" for d in denials)

    def test_get_action_stats(
        self, db_session: Session, test_account, test_user_for_audit
    ):
        """Test getting action statistics."""
        # Create various logs
        for _ in range(3):
            crud_audit_log.log_action(
                db_session,
                account_id=test_account.id,
                user_id=test_user_for_audit.id,
                action="permission_check",
                status="success",
            )
        for _ in range(2):
            crud_audit_log.log_action(
                db_session,
                account_id=test_account.id,
                user_id=test_user_for_audit.id,
                action="permission_check",
                status="denied",
            )
        crud_audit_log.log_action(
            db_session,
            account_id=test_account.id,
            user_id=test_user_for_audit.id,
            action="role_assigned",
            status="success",
        )

        stats = crud_audit_log.get_action_stats(
            db_session, account_id=test_account.id, days=30
        )

        # Should have 3 distinct action/status combinations
        assert len(stats) == 3

        # Find the permission_check success stat
        perm_success = next(
            s
            for s in stats
            if s["action"] == "permission_check" and s["status"] == "success"
        )
        assert perm_success["count"] == 3

        # Find the permission_check denied stat
        perm_denied = next(
            s
            for s in stats
            if s["action"] == "permission_check" and s["status"] == "denied"
        )
        assert perm_denied["count"] == 2

    def test_get_user_activity(
        self, db_session: Session, test_account, test_user_for_audit
    ):
        """Test getting user activity statistics."""
        # Create logs for the user
        for i in range(5):
            crud_audit_log.log_action(
                db_session,
                account_id=test_account.id,
                user_id=test_user_for_audit.id,
                action=f"action_{i}",
                status="success",
            )

        activity = crud_audit_log.get_user_activity(
            db_session, account_id=test_account.id, days=30, limit=10
        )

        assert len(activity) == 1
        assert activity[0]["username"] == test_user_for_audit.username
        assert activity[0]["email"] == test_user_for_audit.email
        assert activity[0]["action_count"] == 5

    def test_count_by_account(
        self, db_session: Session, test_account, test_user_for_audit
    ):
        """Test counting audit logs by account."""
        # Create some logs
        for _i in range(10):
            crud_audit_log.log_action(
                db_session,
                account_id=test_account.id,
                user_id=test_user_for_audit.id,
                action="test_action",
                status="success",
            )

        count = crud_audit_log.count_by_account(db_session, account_id=test_account.id)
        assert count == 10

        # Test with filters
        crud_audit_log.log_action(
            db_session,
            account_id=test_account.id,
            user_id=test_user_for_audit.id,
            action="different_action",
            status="denied",
        )

        count = crud_audit_log.count_by_account(
            db_session, account_id=test_account.id, action="different_action"
        )
        assert count == 1

    def test_consent_ref_filters_list_count_and_timeline(
        self, db_session: Session, test_account, test_user_for_audit
    ):
        """``details.grant.consent_ref`` matches one account's exact reference.

        A different reference, a row with no grant, and the same reference
        on another account must all stay out of the three readers.
        """
        other = crud_account.create(
            db_session,
            obj_in={"organization_name": "Other consent org", "is_active": True},
        )
        match = crud_audit_log.log_action(
            db_session,
            account_id=test_account.id,
            user_id=test_user_for_audit.id,
            action="tool_call",
            resource_type="tool",
            resource_id="search",
            status="success",
            details={
                "correlation_id": "c-match",
                "grant": {"consent_ref": "consent-a"},
            },
        )
        crud_audit_log.log_action(
            db_session,
            account_id=test_account.id,
            user_id=test_user_for_audit.id,
            action="tool_call",
            resource_type="tool",
            resource_id="search",
            status="success",
            details={
                "correlation_id": "c-other-ref",
                "grant": {"consent_ref": "consent-b"},
            },
        )
        crud_audit_log.log_action(
            db_session,
            account_id=test_account.id,
            user_id=test_user_for_audit.id,
            action="tool_call",
            resource_type="tool",
            resource_id="search",
            status="success",
            details={"correlation_id": "c-no-grant"},
        )
        crud_audit_log.log_action(
            db_session,
            account_id=other.id,
            user_id=test_user_for_audit.id,
            action="tool_call",
            resource_type="tool",
            resource_id="search",
            status="success",
            details={
                "correlation_id": "c-foreign",
                "grant": {"consent_ref": "consent-a"},
            },
        )

        listed = crud_audit_log.get_by_account(
            db_session, account_id=test_account.id, consent_ref="consent-a"
        )
        assert [row.id for row in listed] == [match.id]

        assert (
            crud_audit_log.count_by_account(
                db_session, account_id=test_account.id, consent_ref="consent-a"
            )
            == 1
        )

        groups, total = crud_audit_log.get_grouped_by_correlation(
            db_session, account_id=test_account.id, consent_ref="consent-a"
        )
        assert total == 1
        assert len(groups) == 1
        assert groups[0]["correlation_id"] == "c-match"
        assert groups[0]["primary_event"].id == match.id

    def test_account_isolation(self, db_session: Session, test_user_for_audit):
        """Test that audit logs are properly isolated by account."""
        # Create two accounts
        account1_data = {"organization_name": "Account 1", "is_active": True}
        account1 = crud_account.create(db_session, obj_in=account1_data)

        account2_data = {"organization_name": "Account 2", "is_active": True}
        account2 = crud_account.create(db_session, obj_in=account2_data)

        # Create logs for each account
        crud_audit_log.log_action(
            db_session,
            account_id=account1.id,
            user_id=test_user_for_audit.id,
            action="account1_action",
            status="success",
        )
        crud_audit_log.log_action(
            db_session,
            account_id=account2.id,
            user_id=test_user_for_audit.id,
            action="account2_action",
            status="success",
        )

        # Verify isolation
        logs1 = crud_audit_log.get_by_account(db_session, account_id=account1.id)
        assert len(logs1) == 1
        assert logs1[0].action == "account1_action"

        logs2 = crud_audit_log.get_by_account(db_session, account_id=account2.id)
        assert len(logs2) == 1
        assert logs2[0].action == "account2_action"

    def test_get_cli_activity_stats_empty(self, db_session: Session, test_account):
        """Empty window returns zeroed stats without version rows."""
        stats = crud_audit_log.get_cli_activity_stats(
            db_session, account_id=test_account.id, days=30
        )
        assert stats == {
            "cli_checkins_total": 0,
            "cli_active_unique_ips": 0,
            "cli_active_last_24h": 0,
            "cli_last_seen_at": None,
            "top_cli_versions": [],
        }

    def test_get_cli_activity_stats_aggregates(
        self, db_session: Session, test_account, test_user_for_audit
    ):
        """Scalar + version aggregations preserve prior return shape/semantics."""
        now = datetime.now(timezone.utc)
        recent = now - timedelta(hours=1)
        older = now - timedelta(days=2)

        for ip, version, ts in (
            ("1.1.1.1", "0.10.0", recent),
            ("1.1.1.1", "0.10.0", recent),
            ("2.2.2.2", "0.9.0", older),
            ("3.3.3.3", "0.10.0", older),
        ):
            log = crud_audit_log.log_action(
                db_session,
                account_id=test_account.id,
                user_id=test_user_for_audit.id,
                action="cli_activity",
                resource_type="cli",
                resource_id="version_check",
                status="success",
                ip_address=ip,
                details={"cli_version": version},
            )
            log.timestamp = ts.replace(tzinfo=None)
        db_session.commit()

        stats = crud_audit_log.get_cli_activity_stats(
            db_session, account_id=test_account.id, days=30
        )

        assert stats["cli_checkins_total"] == 4
        assert stats["cli_active_unique_ips"] == 3
        assert stats["cli_active_last_24h"] == 1
        assert stats["cli_last_seen_at"] is not None
        assert stats["top_cli_versions"][0] == {"version": "0.10.0", "count": 3}
        assert {"version": "0.9.0", "count": 1} in stats["top_cli_versions"]


class TestGroupedStandalonePolicyDecisions:
    """Policy decisions without a tool_call row anchor their own group (#1136)."""

    @staticmethod
    def _log(db, account, action, tool, cid, status="success", **extra):
        details = {"correlation_id": cid, "tool_name": tool, **extra}
        if action.startswith("policy_"):
            details["decision"] = status
        return crud_audit_log.log_action(
            db,
            account_id=account.id,
            action=action,
            resource_type="policy" if action.startswith("policy_") else "tool",
            resource_id=tool,
            status=status,
            details=details,
        )

    @pytest.fixture
    def events(self, db_session: Session, test_account):
        # Allowed call: tool_call plus its policy_allow sub-event.
        self._log(db_session, test_account, "tool_call", "read_file", "c-allow")
        self._log(
            db_session, test_account, "policy_allow", "read_file", "c-allow", "allow"
        )
        # Denied call: policy_deny only, no tool_call row.
        deny = self._log(
            db_session, test_account, "policy_deny", "delete_repo", "c-deny", "deny"
        )
        # require_approval that never executed: no tool_call row.
        approval = self._log(
            db_session,
            test_account,
            "policy_require_approval",
            "send_email",
            "c-approval",
            "require_approval",
        )
        self._log(
            db_session,
            test_account,
            "approval_created",
            "send_email",
            "c-approval",
            approval_id="a-1",
        )
        # Deny that still has a tool_call row stays a sub-event.
        self._log(db_session, test_account, "tool_call", "rm", "c-both", "denied")
        self._log(db_session, test_account, "policy_deny", "rm", "c-both", "deny")
        return deny, approval

    def test_standalone_deny_is_a_primary_event(
        self, db_session: Session, test_account, events
    ):
        deny, approval = events
        groups, total = crud_audit_log.get_grouped_by_correlation(
            db_session, account_id=test_account.id
        )
        primaries = {(g["primary_event"].action, g["correlation_id"]) for g in groups}
        assert ("policy_deny", "c-deny") in primaries
        assert ("policy_require_approval", "c-approval") in primaries
        # Correlated decisions remain sub-events of their tool_call.
        assert ("policy_deny", "c-both") not in primaries
        assert ("policy_allow", "c-allow") not in primaries
        assert total == len(groups) == 4

        by_cid = {g["correlation_id"]: g for g in groups}
        assert by_cid["c-deny"]["outcome"] == "deny"
        assert by_cid["c-deny"]["sub_events"] == []
        assert [s.action for s in by_cid["c-approval"]["sub_events"]] == [
            "approval_created"
        ]
        assert [s.action for s in by_cid["c-both"]["sub_events"]] == ["policy_deny"]

    def test_total_adds_both_branches_on_a_short_page(
        self, db_session: Session, test_account, events
    ):
        # The total is counted per branch (#1592); a one-row page must still
        # report every primary event, standalone decisions included.
        groups, total = crud_audit_log.get_grouped_by_correlation(
            db_session, account_id=test_account.id, limit=1
        )
        assert len(groups) == 1
        assert total == 4

    def test_count_limit_caps_the_total_and_says_there_is_more(
        self, db_session: Session, test_account, events
    ):
        # The exact total reads every matching row; a capped one stops past
        # the cap (#1592). Above the cap it reports cap + 1: "more than".
        def total(**kwargs):
            return crud_audit_log.get_grouped_by_correlation(
                db_session, account_id=test_account.id, limit=1, **kwargs
            )[1]

        assert total() == 4
        assert total(count_limit=10) == 4
        assert total(count_limit=4) == 4
        assert total(count_limit=3) == 4
        assert total(count_limit=1) == 2
        assert total(count_limit=0) == 1
        assert total(count_limit=1, event_type_filter=["tool_call"]) == 2
        with pytest.raises(ValueError):
            total(count_limit=-1)

    def test_count_limit_leaves_window_totals_exact(
        self, db_session: Session, test_account, events
    ):
        groups, total = crud_audit_log.get_grouped_by_correlation(
            db_session,
            account_id=test_account.id,
            outcome_filter=["deny"],
            count_limit=1,
        )
        assert total == len(groups) == 2

    def _statements(self, db_session: Session, **kwargs) -> tuple[list[str], int]:
        from sqlalchemy import event as sa_event

        statements: list[str] = []

        def _capture(conn, cursor, statement, *args):
            statements.append(statement)

        bind = db_session.get_bind()
        sa_event.listen(bind, "before_cursor_execute", _capture)
        try:
            _, total = crud_audit_log.get_grouped_by_correlation(
                db_session, account_id=kwargs.pop("account_id"), **kwargs
            )
        finally:
            sa_event.remove(bind, "before_cursor_execute", _capture)
        return statements, total

    def test_capped_total_stops_counting_once_past_the_cap(
        self, db_session: Session, test_account, events
    ):
        # The primary branch is an index-only count. When it alone passes
        # the cap, the anti-join branches (standalone decisions, orphans)
        # cannot change the capped total, and on prod they were the cold
        # tail of the Audit page: 0.87 s and 0.42 s of a 2.2 s call (#1693).
        statements, total = self._statements(
            db_session, account_id=test_account.id, limit=1, count_limit=1
        )
        counts = [s for s in statements if "count(" in s.lower()]
        assert total == 2
        assert len(counts) == 1
        assert "EXISTS" not in counts[0]

        # Under the cap every branch is still counted, exactly.
        statements, total = self._statements(
            db_session, account_id=test_account.id, limit=1, count_limit=10
        )
        assert total == 4
        assert len([s for s in statements if "count(" in s.lower()]) == 4

    def test_grouped_reads_run_without_jit_and_restore_it(
        self, db_session: Session, test_account, events
    ):
        # The anti-join probes are estimated at millions of cost units, so
        # Postgres JIT-compiled each statement: 150-270 ms of LLVM per query
        # on prod, more on a fresh backend, for 20-100 ms of execution
        # (#1693). The grouped read turns JIT off for its own statements.
        from sqlalchemy import text as sa_text

        db_session.execute(sa_text("SET jit = on"))
        statements, _ = self._statements(
            db_session, account_id=test_account.id, count_limit=10
        )
        assert any("set_config('jit'" in s for s in statements)
        assert db_session.execute(sa_text("SHOW jit")).scalar() == "on"

    def test_tool_call_probe_is_not_hashed(
        self, db_session: Session, test_account, events
    ):
        # A hashed subplan would read every tool_call's details (#1592).
        # ``OFFSET 0`` keeps the EXISTS a per-row index probe.
        from sqlalchemy import event as sa_event

        statements: list[str] = []

        def _capture(conn, cursor, statement, *args):
            statements.append(statement)

        bind = db_session.get_bind()
        sa_event.listen(bind, "before_cursor_execute", _capture)
        try:
            crud_audit_log.get_grouped_by_correlation(
                db_session, account_id=test_account.id
            )
        finally:
            sa_event.remove(bind, "before_cursor_execute", _capture)
        probes = [s for s in statements if "EXISTS" in s]
        assert probes
        assert all("OFFSET" in s for s in probes)

    def test_approval_chain_orphans_are_counted_through_nested_probes(
        self, db_session: Session, test_account, events
    ):
        # approval_approved carries the approval_id but no correlation_id:
        # chained to an anchored approval_created it is a sub-event, with no
        # such approval it is an orphan and anchors its own group.
        self._log(
            db_session,
            test_account,
            "approval_approved",
            "send_email",
            None,
            approval_id="a-1",
        )
        self._log(
            db_session,
            test_account,
            "approval_approved",
            "send_email",
            None,
            approval_id="a-unknown",
        )
        from sqlalchemy import event as sa_event

        statements: list[str] = []

        def _capture(conn, cursor, statement, *args):
            statements.append(" ".join(statement.split()))

        bind = db_session.get_bind()
        sa_event.listen(bind, "before_cursor_execute", _capture)
        try:
            groups, total = crud_audit_log.get_grouped_by_correlation(
                db_session, account_id=test_account.id
            )
        finally:
            sa_event.remove(bind, "before_cursor_execute", _capture)

        orphans = [
            g["primary_event"].details.get("approval_id")
            for g in groups
            if g["primary_event"].action == "approval_approved"
        ]
        assert orphans == ["a-unknown"]
        assert total == len(groups) == 5
        # As one join the approval probe was driven from a sequential scan of
        # every approval_created row, per candidate: past the 30 s statement
        # timeout on prod (#1693). Each probe is its own EXISTS now.
        probes = [s for s in statements if "EXISTS" in s]
        assert probes
        assert not [s for s in probes if " JOIN audit_log AS " in s]

    def test_other_event_families_are_listed_by_name_not_by_exclusion(
        self, db_session: Session, test_account, events
    ):
        # Actions outside the grouped families still anchor their own group.
        # They are matched as an IN list of the account's actions: a NOT IN
        # walked every row in range of the account index (#1693).
        self._log(db_session, test_account, "runner.inventory_changed", "r", None)
        from sqlalchemy import event as sa_event

        statements: list[str] = []

        def _capture(conn, cursor, statement, *args):
            statements.append(" ".join(statement.split()))

        bind = db_session.get_bind()
        sa_event.listen(bind, "before_cursor_execute", _capture)
        try:
            groups, total = crud_audit_log.get_grouped_by_correlation(
                db_session, account_id=test_account.id, count_limit=100
            )
        finally:
            sa_event.remove(bind, "before_cursor_execute", _capture)

        actions = [g["primary_event"].action for g in groups]
        assert "runner.inventory_changed" in actions
        assert total == len(groups) == 5
        assert not [s for s in statements if "audit_log.action NOT IN" in s]

    def test_no_other_event_families_counts_nothing_for_that_branch(
        self, db_session: Session, test_account, events
    ):
        groups, total = crud_audit_log.get_grouped_by_correlation(
            db_session, account_id=test_account.id
        )
        assert total == len(groups) == 4

    def test_sub_events_are_probed_by_correlation_only(
        self, db_session: Session, test_account, events
    ):
        # With an action list or ORDER BY timestamp in the sub-event read the
        # planner walked the timestamp or action index across the account
        # (18 s, then the statement timeout on prod, #1693). Only the id probe
        # goes to SQL; the action filter and the order are applied in Python.
        from sqlalchemy import event as sa_event

        statements: list[str] = []

        def _capture(conn, cursor, statement, *args):
            statements.append(" ".join(statement.split()))

        bind = db_session.get_bind()
        sa_event.listen(bind, "before_cursor_execute", _capture)
        try:
            groups, _ = crud_audit_log.get_grouped_by_correlation(
                db_session, account_id=test_account.id
            )
        finally:
            sa_event.remove(bind, "before_cursor_execute", _capture)

        sub_reads = [
            s
            for s in statements
            if "(audit_log.details ->> " in s
            and ") IN (" in s
            and "count(" not in s
            and "EXISTS" not in s
        ]
        assert sub_reads
        for sql in sub_reads:
            assert "audit_log.action IN" not in sql
            assert "ORDER BY" not in sql
        by_cid = {g["correlation_id"]: g for g in groups}
        assert [s.action for s in by_cid["c-both"]["sub_events"]] == ["policy_deny"]

    def test_tool_name_filter_matches_standalone_deny(
        self, db_session: Session, test_account, events
    ):
        groups, total = crud_audit_log.get_grouped_by_correlation(
            db_session, account_id=test_account.id, tool_name_filter="delete"
        )
        assert total == 1
        assert groups[0]["primary_event"].action == "policy_deny"
        assert groups[0]["primary_event"].resource_id == "delete_repo"

    def test_standalone_outcome_comes_from_the_decision(
        self, db_session: Session, test_account
    ):
        row = self._log(
            db_session, test_account, "policy_deny", "drop_db", "c-x", "denied"
        )
        row.details = {**row.details, "decision": "deny"}
        db_session.flush()
        groups, _ = crud_audit_log.get_grouped_by_correlation(
            db_session, account_id=test_account.id, outcome_filter=["deny"]
        )
        assert [g["correlation_id"] for g in groups] == ["c-x"]
        assert groups[0]["outcome"] == "deny"

    def test_failed_call_after_allow_shows_failed(
        self, db_session: Session, test_account
    ):
        # A call the policy allowed and that then failed used to read "allow"
        # while an outcome=failed filter matched it: the Overview listed
        # "tool call / update_pull_request / allow" as an exception.
        self._log(db_session, test_account, "tool_call", "update_pr", "c-f", "failed")
        self._log(db_session, test_account, "policy_allow", "update_pr", "c-f", "allow")
        for outcome in ("failed", "allow"):
            groups, _ = crud_audit_log.get_grouped_by_correlation(
                db_session, account_id=test_account.id, outcome_filter=[outcome]
            )
            assert [g["correlation_id"] for g in groups] == ["c-f"]
            assert groups[0]["outcome"] == "failed"

    def test_outcome_match_any_is_an_or(
        self, db_session: Session, test_account, events
    ):
        self._log(db_session, test_account, "tool_call", "update_pr", "c-f", "failed")
        self._log(db_session, test_account, "policy_allow", "update_pr", "c-f", "allow")
        wanted = ["failed", "deny", "budget_denied"]
        groups, total = crud_audit_log.get_grouped_by_correlation(
            db_session,
            account_id=test_account.id,
            outcome_filter=wanted,
            outcome_match="any",
        )
        assert {g["correlation_id"] for g in groups} == {"c-f", "c-deny", "c-both"}
        assert total == 3
        assert all(g["outcome"] in wanted for g in groups)
        # The default stays AND: no chain carries all three.
        groups, total = crud_audit_log.get_grouped_by_correlation(
            db_session, account_id=test_account.id, outcome_filter=wanted
        )
        assert (groups, total) == ([], 0)

    def test_outcome_match_rejects_unknown_mode(
        self, db_session: Session, test_account
    ):
        with pytest.raises(ValueError):
            crud_audit_log.get_grouped_by_correlation(
                db_session, account_id=test_account.id, outcome_match="some"
            )

    def test_outcome_filter_deny_includes_standalone(
        self, db_session: Session, test_account, events
    ):
        groups, _ = crud_audit_log.get_grouped_by_correlation(
            db_session, account_id=test_account.id, outcome_filter=["deny"]
        )
        assert {g["correlation_id"] for g in groups} == {"c-deny", "c-both"}

    def test_groups_are_built_narrow_and_return_full_rows(
        self, db_session: Session, test_account, events
    ):
        """Only the returned page is loaded in full (#1592).

        Grouping reads four ``details`` keys, so the 5000-event outcome
        window is fetched without the rest of ``details``. The page that is
        returned must still carry complete ORM rows, sub-events included.
        """
        statements: list[str] = []

        def record(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        engine = db_session.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            groups, _ = crud_audit_log.get_grouped_by_correlation(
                db_session, account_id=test_account.id, outcome_filter=["deny"]
            )
        finally:
            event.remove(engine, "before_cursor_execute", record)

        by_cid = {g["correlation_id"]: g for g in groups}
        both = by_cid["c-both"]
        assert isinstance(both["primary_event"], AuditLog)
        assert both["primary_event"].details["tool_name"] == "rm"
        assert all(isinstance(sub, AuditLog) for sub in both["sub_events"])
        assert both["sub_events"][0].details["tool_name"] == "rm"
        # Only the final hydration selects the details column itself.
        full_row_reads = [
            s for s in statements if "audit_log.details AS audit_log_details" in s
        ]
        assert len(full_row_reads) == 1
        assert "audit_log.id IN" in full_row_reads[0]

    def test_cost_filter_uses_estimated_cost_on_the_narrow_path(
        self, db_session: Session, test_account
    ):
        """min_cost/max_cost read numeric estimated_cost from the narrow row."""
        self._log(
            db_session,
            test_account,
            "tool_call",
            "read_file",
            "c-cheap",
            estimated_cost=0.25,
        )
        self._log(
            db_session,
            test_account,
            "tool_call",
            "write_file",
            "c-mid",
            estimated_cost=1.5,
        )
        self._log(
            db_session,
            test_account,
            "tool_call",
            "deploy",
            "c-high",
            estimated_cost=8.0,
        )

        groups, total = crud_audit_log.get_grouped_by_correlation(
            db_session,
            account_id=test_account.id,
            min_cost=1.0,
            max_cost=2.0,
        )
        assert total == 1
        assert [g["correlation_id"] for g in groups] == ["c-mid"]
        primary = groups[0]["primary_event"]
        assert isinstance(primary, AuditLog)
        assert primary.details["estimated_cost"] == 1.5
        assert primary.details["tool_name"] == "write_file"

        cheap, cheap_total = crud_audit_log.get_grouped_by_correlation(
            db_session, account_id=test_account.id, max_cost=1.0
        )
        assert cheap_total == 1
        assert cheap[0]["correlation_id"] == "c-cheap"
        assert isinstance(cheap[0]["primary_event"], AuditLog)
        assert cheap[0]["primary_event"].details["estimated_cost"] == 0.25

        high, high_total = crud_audit_log.get_grouped_by_correlation(
            db_session, account_id=test_account.id, min_cost=5.0
        )
        assert high_total == 1
        assert high[0]["correlation_id"] == "c-high"
        assert isinstance(high[0]["primary_event"], AuditLog)
        assert high[0]["primary_event"].details["estimated_cost"] == 8.0

    def test_narrow_and_full_details_share_the_group_outcome(
        self, db_session: Session, test_account
    ):
        """Extra details keys do not change the outcome taken from decision.

        Grouping reads only the projected keys. A row that also stores
        ``tool_name`` still takes its outcome from ``decision`` (else status),
        and the narrow projection drops ``tool_name`` while keeping
        ``correlation_id`` and numeric ``estimated_cost``.
        """
        row = self._log(
            db_session,
            test_account,
            "policy_deny",
            "drop_table",
            "c-narrow",
            "denied",
            estimated_cost=2.5,
        )
        row.details = {**row.details, "decision": "deny"}
        db_session.flush()

        groups, _ = crud_audit_log.get_grouped_by_correlation(
            db_session, account_id=test_account.id
        )
        full_outcome = row.details.get("decision") or row.status
        assert [g["correlation_id"] for g in groups] == ["c-narrow"]
        assert groups[0]["outcome"] == full_outcome == "deny"
        assert groups[0]["primary_event"].status == "denied"
        assert groups[0]["primary_event"].details["tool_name"] == "drop_table"

        narrow = _narrow_audit_rows(
            db_session.query(AuditLog).filter(AuditLog.id == row.id)
        )
        assert len(narrow) == 1
        details = narrow[0].details or {}
        assert "tool_name" not in details
        assert details["correlation_id"] == "c-narrow"
        assert details["decision"] == "deny"
        assert details["estimated_cost"] == 2.5
        assert isinstance(details["estimated_cost"], (int, float))


def test_grouped_total_branches_are_disjoint():
    """The grouped total sums one count per branch (#1592); no action may be in both."""
    from preloop.models.crud.audit_log import (
        PRIMARY_ACTIONS,
        STANDALONE_POLICY_ACTIONS,
    )

    assert not set(PRIMARY_ACTIONS) & set(STANDALONE_POLICY_ACTIONS)


class TestGroupedTotalDegradesOnTimeout:
    """A total the database cancels must not take the page down (#1693)."""

    @staticmethod
    def _rows(db_session: Session, account, count: int) -> None:
        for index in range(count):
            crud_audit_log.log_action(
                db_session,
                account_id=account.id,
                action="tool_call",
                resource_type="tool",
                resource_id=f"t-{index}",
                status="success",
                details={"correlation_id": f"c-{index}"},
            )

    @staticmethod
    def _time_out(monkeypatch, *, code_attribute: str = "pgcode"):
        """Make every count raise the driver's statement-timeout error.

        psycopg2 (the sync engine's driver for ``postgresql://``) exposes the
        SQLSTATE as ``pgcode``, psycopg3 as ``sqlstate``; both must degrade.
        """
        from types import SimpleNamespace

        from sqlalchemy.exc import OperationalError

        from preloop.models.crud import audit_log as audit_log_module

        def _cancelled(*args, **kwargs):
            orig = SimpleNamespace(**{code_attribute: "57014"})
            raise OperationalError("SELECT count(*)", {}, orig)

        monkeypatch.setattr(audit_log_module, "_count_rows", _cancelled)

    @pytest.mark.parametrize("code_attribute", ["pgcode", "sqlstate"])
    def test_capped_total_reads_more_than_the_cap(
        self, db_session: Session, test_account, monkeypatch, code_attribute
    ):
        self._rows(db_session, test_account, 3)
        self._time_out(monkeypatch, code_attribute=code_attribute)
        groups, total = crud_audit_log.get_grouped_by_correlation(
            db_session, account_id=test_account.id, limit=2, count_limit=100
        )
        assert len(groups) == 2
        assert total == 101

    def test_uncapped_total_is_what_the_page_proves(
        self, db_session: Session, test_account, monkeypatch
    ):
        self._rows(db_session, test_account, 3)
        self._time_out(monkeypatch)
        groups, total = crud_audit_log.get_grouped_by_correlation(
            db_session, account_id=test_account.id, limit=2
        )
        assert len(groups) == 2
        # What the page proves, nothing more: a lower bound.
        assert total == 2
        groups, total = crud_audit_log.get_grouped_by_correlation(
            db_session, account_id=test_account.id, limit=5
        )
        assert len(groups) == 3
        assert total == 3

    def test_other_database_errors_still_raise(
        self, db_session: Session, test_account, monkeypatch
    ):
        from sqlalchemy.exc import OperationalError

        from preloop.models.crud import audit_log as audit_log_module

        def _broken(*args, **kwargs):
            raise OperationalError("SELECT count(*)", {}, Exception("gone"))

        monkeypatch.setattr(audit_log_module, "_count_rows", _broken)
        with pytest.raises(OperationalError):
            crud_audit_log.get_grouped_by_correlation(
                db_session, account_id=test_account.id
            )
