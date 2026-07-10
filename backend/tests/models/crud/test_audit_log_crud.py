"""Tests for audit log CRUD operations."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from preloop.models.crud import crud_audit_log, crud_account, crud_user


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
