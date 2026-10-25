"""Tests for new audit service methods: tool execution, policy decision, and async variants."""

import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_audit_log, crud_user

from preloop.services.audit.service import AuditService

_SECRET = "audit-secret-token-9f3c"
_DISCONNECT_PHRASES = (
    "ssl connection has been closed",
    "connection reset by peer",
    "server closed the connection unexpectedly",
    "connection already closed",
)


def _pool_timeout() -> PoolTimeoutError:
    return PoolTimeoutError(f"QueuePool checkout timed out token={_SECRET}")


def _disconnected(message: str, *, invalidated: bool) -> OperationalError:
    return OperationalError(
        f"INSERT INTO audit_log (secret) VALUES ('{_SECRET}')",
        {"token": _SECRET},
        Exception(message),
        connection_invalidated=invalidated,
    )


def _deadlock(*, statement: str | None = None) -> OperationalError:
    return OperationalError(
        statement or "UPDATE audit_log SET status = %(status)s",
        {"status": "approved", "token": _SECRET},
        Exception("deadlock detected"),
        connection_invalidated=False,
    )


@pytest.fixture
def audit_service():
    """Create an AuditService instance."""
    return AuditService()


@pytest.fixture
def test_account(db_session: Session):
    """Create a test account."""
    account_data = {"organization_name": "Audit Service Test Org", "is_active": True}
    return crud_account.create(db_session, obj_in=account_data)


@pytest.fixture
def test_audit_user(db_session: Session, test_account):
    """Create a test user for audit tests."""
    user_data = {
        "account_id": test_account.id,
        "email": "audit-svc-test@example.com",
        "username": "auditsvctest",
        "full_name": "Audit Service Test",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    return crud_user.create(db_session, obj_in=user_data)


class TestLogToolCall:
    """Tests for AuditService.log_tool_call."""

    def test_log_tool_call_basic(
        self, audit_service, db_session, test_account, test_audit_user
    ):
        """Test basic tool call logging."""
        audit_service.log_tool_call(
            db=db_session,
            account_id=test_account.id,
            user_id=test_audit_user.id,
            tool_name="bash",
            tool_args={"command": "ls -la"},
            result="executed",
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        tool_logs = [l for l in logs if l.action == "tool_call"]
        assert len(tool_logs) == 1
        assert tool_logs[0].resource_type == "tool"
        assert tool_logs[0].resource_id == "bash"
        assert tool_logs[0].status == "executed"
        assert tool_logs[0].details["tool_name"] == "bash"
        assert tool_logs[0].details["tool_args"]["command"] == "ls -la"

    def test_log_tool_call_with_duration(
        self, audit_service, db_session, test_account, test_audit_user
    ):
        """Test tool call logging with duration."""
        audit_service.log_tool_call(
            db=db_session,
            account_id=test_account.id,
            user_id=test_audit_user.id,
            tool_name="search",
            tool_args={"query": "auth bug"},
            result="success",
            duration_ms=150,
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        tool_logs = [l for l in logs if l.action == "tool_call"]
        assert len(tool_logs) == 1
        assert tool_logs[0].details["duration_ms"] == 150

    def test_log_tool_call_with_policy_decision(
        self, audit_service, db_session, test_account, test_audit_user
    ):
        """Test tool call logging with policy decision metadata."""
        audit_service.log_tool_call(
            db=db_session,
            account_id=test_account.id,
            user_id=test_audit_user.id,
            tool_name="bash",
            tool_args={"command": "echo hi"},
            result="executed",
            policy_decision="allow",
            rule_matched="default allow rule",
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        tool_logs = [l for l in logs if l.action == "tool_call"]
        assert len(tool_logs) == 1
        assert tool_logs[0].details["policy_decision"] == "allow"
        assert tool_logs[0].details["rule_matched"] == "default allow rule"

    def test_log_tool_call_disabled(self, db_session, test_account, test_audit_user):
        """Test that logging is skipped when service is disabled."""
        service = AuditService()
        service.enabled = False

        service.log_tool_call(
            db=db_session,
            account_id=test_account.id,
            user_id=test_audit_user.id,
            tool_name="bash",
            tool_args={},
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        tool_logs = [l for l in logs if l.action == "tool_call"]
        assert len(tool_logs) == 0

    def test_log_tool_call_with_runtime_and_api_key_metadata(
        self, audit_service, db_session, test_account, test_audit_user
    ):
        """Test tool call logging captures session and API token attribution."""
        audit_service.log_tool_call(
            db=db_session,
            account_id=test_account.id,
            user_id=test_audit_user.id,
            tool_name="bash",
            tool_args={"command": "echo hi"},
            result="executed",
            correlation_id="corr-123",
            runtime_session_id="runtime-session-1",
            runtime_principal_type="claude_code",
            runtime_principal_id="workspace-42",
            runtime_principal_name="Claude Workspace",
            api_key_id="api-key-1",
            api_key_name="Claude Workspace Token",
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        tool_logs = [l for l in logs if l.action == "tool_call"]
        assert len(tool_logs) == 1
        assert tool_logs[0].details["correlation_id"] == "corr-123"
        assert tool_logs[0].details["runtime_session_id"] == "runtime-session-1"
        assert tool_logs[0].details["runtime_principal_name"] == "Claude Workspace"
        assert tool_logs[0].details["api_key_id"] == "api-key-1"
        assert tool_logs[0].details["api_key_name"] == "Claude Workspace Token"


class TestLogPolicyDecision:
    """Tests for AuditService.log_policy_decision."""

    def test_log_policy_decision_allow(
        self, audit_service, db_session, test_account, test_audit_user
    ):
        """Test logging an allow decision."""
        audit_service.log_policy_decision(
            db=db_session,
            account_id=test_account.id,
            user_id=test_audit_user.id,
            tool_name="get_issue",
            action="allow",
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        policy_logs = [l for l in logs if l.action == "policy_allow"]
        assert len(policy_logs) == 1
        assert policy_logs[0].status == "allow"
        assert policy_logs[0].details["decision"] == "allow"

    def test_log_policy_decision_deny(
        self, audit_service, db_session, test_account, test_audit_user
    ):
        """Test logging a deny decision."""
        audit_service.log_policy_decision(
            db=db_session,
            account_id=test_account.id,
            user_id=test_audit_user.id,
            tool_name="bash",
            action="deny",
            rule_description="Blocked dangerous command",
            tool_args={"command": "rm -rf /"},
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        policy_logs = [l for l in logs if l.action == "policy_deny"]
        assert len(policy_logs) == 1
        assert policy_logs[0].details["rule_description"] == "Blocked dangerous command"
        assert policy_logs[0].details["tool_args"]["command"] == "rm -rf /"

    def test_log_policy_decision_require_approval(
        self, audit_service, db_session, test_account, test_audit_user
    ):
        """Test logging a require_approval decision."""
        exec_id = uuid4()
        audit_service.log_policy_decision(
            db=db_session,
            account_id=test_account.id,
            user_id=test_audit_user.id,
            tool_name="deploy",
            action="require_approval",
            execution_id=exec_id,
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        policy_logs = [l for l in logs if l.action == "policy_require_approval"]
        assert len(policy_logs) == 1
        assert policy_logs[0].details["execution_id"] == str(exec_id)

    def test_log_policy_decision_disabled(
        self, db_session, test_account, test_audit_user
    ):
        """Test that logging is skipped when service is disabled."""
        service = AuditService()
        service.enabled = False

        service.log_policy_decision(
            db=db_session,
            account_id=test_account.id,
            user_id=test_audit_user.id,
            tool_name="bash",
            action="deny",
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        policy_logs = [l for l in logs if l.action.startswith("policy_")]
        assert len(policy_logs) == 0

    def test_log_policy_decision_with_extra_details(
        self, audit_service, db_session, test_account, test_audit_user
    ):
        """Test policy decisions retain correlation and API token metadata."""
        audit_service.log_policy_decision(
            db=db_session,
            account_id=test_account.id,
            user_id=test_audit_user.id,
            tool_name="search",
            action="allow",
            correlation_id="corr-456",
            extra_details={
                "runtime_session_id": "runtime-session-1",
                "api_key_name": "Automation Token",
            },
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        policy_logs = [l for l in logs if l.action == "policy_allow"]
        assert len(policy_logs) == 1
        assert policy_logs[0].details["correlation_id"] == "corr-456"
        assert policy_logs[0].details["runtime_session_id"] == "runtime-session-1"
        assert policy_logs[0].details["api_key_name"] == "Automation Token"


class TestLogToolCallAsync:
    """Tests for AuditService.log_tool_call_async."""

    def test_log_tool_call_async_basic(
        self, audit_service, db_session, test_account, test_audit_user
    ):
        """Test async tool call logging."""
        account_id = test_account.id
        user_id = test_audit_user.id

        def db_factory():
            return db_session

        audit_service.log_tool_call_async(
            db_factory=db_factory,
            account_id=account_id,
            tool_name="create_issue",
            tool_args={"title": "New bug"},
            result="executed",
            user_id=user_id,
        )

        # Async fires a task — but without a running event loop in tests,
        # it falls back to a debug log. Just verify no exception is raised.

    def test_log_tool_call_async_with_execution_id(
        self, audit_service, db_session, test_account, test_audit_user
    ):
        """Test async logging accepts execution_id."""
        account_id = test_account.id
        user_id = test_audit_user.id
        exec_id = uuid4()

        def db_factory():
            return db_session

        audit_service.log_tool_call_async(
            db_factory=db_factory,
            account_id=account_id,
            tool_name="bash",
            tool_args={},
            user_id=user_id,
            execution_id=exec_id,
        )

        # No exception means the method accepted the params correctly.

    def test_log_tool_call_async_disabled(
        self, db_session, test_account, test_audit_user
    ):
        """Test that async logging is skipped when disabled."""
        account_id = test_account.id
        user_id = test_audit_user.id
        service = AuditService()
        service.enabled = False

        service.log_tool_call_async(
            db_factory=lambda: db_session,
            account_id=account_id,
            tool_name="bash",
            tool_args={},
            user_id=user_id,
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=account_id)
        tool_logs = [l for l in logs if l.action == "tool_call"]
        assert len(tool_logs) == 0


class TestLogPolicyDecisionAsync:
    """Tests for AuditService.log_policy_decision_async."""

    def test_log_policy_decision_async_basic(
        self, audit_service, db_session, test_account, monkeypatch
    ):
        """Sync redaction workers still admit a policy audit with an owned session."""
        from preloop.services.audit import service as audit_module

        submitted = []
        monkeypatch.setattr(
            audit_module,
            "schedule_audit_write",
            lambda *args, **kwargs: submitted.append((args, kwargs)),
        )
        audit_service.log_policy_decision_async(
            db_factory=lambda: db_session,
            account_id=test_account.id,
            tool_name="bash",
            action="deny",
            rule_description="Blocked",
            condition_matched="args.command.contains('rm')",
        )
        args, fields = submitted[0]
        assert fields.pop("allow_without_loop") is True
        assert fields.pop("priority") is True
        args[1](db_session, **fields)
        rows = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        assert [row.action for row in rows] == ["policy_deny"]


class TestLogApprovalLifecycle:
    """Tests for AuditService.log_approval_lifecycle."""

    def test_log_approval_created(
        self, audit_service, db_session, test_account, test_audit_user
    ):
        """Test logging approval creation."""
        approval_id = uuid4()
        audit_service.log_approval_lifecycle(
            db=db_session,
            account_id=test_account.id,
            approval_id=approval_id,
            event="created",
            tool_name="deploy",
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        approval_logs = [l for l in logs if l.action == "approval_created"]
        assert len(approval_logs) == 1
        assert approval_logs[0].status == "created"
        assert approval_logs[0].details["tool_name"] == "deploy"

    def test_log_approval_denied(
        self, audit_service, db_session, test_account, test_audit_user
    ):
        """Test logging approval denial."""
        approval_id = uuid4()
        audit_service.log_approval_lifecycle(
            db=db_session,
            account_id=test_account.id,
            approval_id=approval_id,
            event="denied",
            approver_id=test_audit_user.id,
            reason="Too risky",
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        approval_logs = [l for l in logs if l.action == "approval_denied"]
        assert len(approval_logs) == 1
        assert approval_logs[0].status == "denied"
        assert approval_logs[0].details["reason"] == "Too risky"

    def test_log_runtime_session_event(
        self, audit_service, db_session, test_account, test_audit_user
    ):
        """Test runtime session lifecycle events are logged."""
        audit_service.log_runtime_session_event(
            db=db_session,
            account_id=test_account.id,
            runtime_session_id="runtime-session-1",
            event="created",
            session_source_type="claude_code",
            session_source_id="workspace-42",
            session_reference="claude-session-42",
            runtime_principal_type="claude_code",
            runtime_principal_id="workspace-42",
            runtime_principal_name="Claude Workspace",
            api_key_name="Claude Workspace Token",
            user_id=test_audit_user.id,
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        session_logs = [l for l in logs if l.action == "runtime_session_created"]
        assert len(session_logs) == 1
        assert session_logs[0].resource_type == "runtime_session"
        assert session_logs[0].details["session_source_type"] == "claude_code"
        assert session_logs[0].details["session_reference"] == "claude-session-42"
        assert session_logs[0].details["api_key_name"] == "Claude Workspace Token"


class TestLogApprovalNotification:
    """Tests for AuditService.log_approval_notification (per-channel fan-out)."""

    def test_log_email_notification_with_recipients(
        self, audit_service, db_session, test_account, test_audit_user
    ):
        """Email fan-out audit row should record channel + recipient list + counts."""
        approval_id = uuid4()
        recipients = [test_audit_user.id, uuid4()]

        audit_service.log_approval_notification(
            db=db_session,
            account_id=test_account.id,
            approval_id=approval_id,
            channel="email",
            status="sent",
            tool_name="deploy",
            recipient_user_ids=recipients,
            sent_count=2,
            failed_count=0,
            skipped_count=0,
            correlation_id="corr-abc-123",
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        notif_logs = [l for l in logs if l.action == "approval_notification_sent"]
        assert len(notif_logs) == 1
        log = notif_logs[0]
        assert log.status == "sent"
        assert log.resource_type == "approval"
        assert log.resource_id == str(approval_id)
        assert log.details["channel"] == "email"
        assert log.details["sent_count"] == 2
        assert log.details["failed_count"] == 0
        assert log.details["recipient_count"] == 2
        assert log.details["correlation_id"] == "corr-abc-123"
        assert {str(r) for r in recipients}.issubset(
            set(log.details["recipient_user_ids"])
        )

    def test_log_failed_webhook_notification(
        self, audit_service, db_session, test_account
    ):
        """Webhook failure should record the error string."""
        approval_id = uuid4()
        audit_service.log_approval_notification(
            db=db_session,
            account_id=test_account.id,
            approval_id=approval_id,
            channel="slack",
            status="failed",
            tool_name="pay",
            error="HTTP 500",
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        notif_logs = [l for l in logs if l.action == "approval_notification_sent"]
        assert len(notif_logs) == 1
        assert notif_logs[0].status == "failed"
        assert notif_logs[0].details["error"] == "HTTP 500"
        assert notif_logs[0].details["channel"] == "slack"


class TestLogApprovalToolExecuted:
    """Tests for AuditService.log_approval_tool_executed (post-approval execution)."""

    def test_log_successful_execution(self, audit_service, db_session, test_account):
        """Successful post-approval execution should record duration + preview."""
        approval_id = uuid4()
        audit_service.log_approval_tool_executed(
            db=db_session,
            account_id=test_account.id,
            approval_id=approval_id,
            tool_name="deploy",
            status="executed",
            duration_ms=1234,
            result_preview="Deployment ok",
            correlation_id="corr-xyz",
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        exec_logs = [l for l in logs if l.action == "approval_tool_executed"]
        assert len(exec_logs) == 1
        log = exec_logs[0]
        assert log.status == "executed"
        assert log.details["duration_ms"] == 1234
        assert "result_preview" not in log.details
        assert log.details["result_preview_length"] == len("Deployment ok")
        assert len(log.details["result_preview_sha256"]) == 64
        assert log.details["tool_name"] == "deploy"
        assert log.details["approval_id"] == str(approval_id)
        assert log.details["correlation_id"] == "corr-xyz"

    def test_log_failed_execution_truncates_long_error(
        self, audit_service, db_session, test_account
    ):
        """Long errors should be truncated to keep the audit payload manageable."""
        approval_id = uuid4()
        long_err = "x" * 5000
        audit_service.log_approval_tool_executed(
            db=db_session,
            account_id=test_account.id,
            approval_id=approval_id,
            tool_name="pay",
            status="failed",
            error=long_err,
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        exec_logs = [l for l in logs if l.action == "approval_tool_executed"]
        assert len(exec_logs) == 1
        assert exec_logs[0].status == "failed"
        assert len(exec_logs[0].details["error"]) == 200


class TestLogApprovalLifecycleAsync:
    """Tests for AuditService.log_approval_lifecycle_async."""

    def test_log_approval_lifecycle_async(
        self, audit_service, db_session, test_account, test_audit_user
    ):
        """Test async approval lifecycle logging."""
        account_id = test_account.id
        user_id = test_audit_user.id
        approval_id = uuid4()

        audit_service.log_approval_lifecycle_async(
            db_factory=lambda: db_session,
            account_id=account_id,
            approval_id=approval_id,
            event="approved",
            tool_name="deploy",
            approver_id=user_id,
            reason="Looks good",
        )

        # Async fires a task — without a running event loop in tests,
        # it falls back to a debug log. Just verify no exception is raised.


class TestLogModelGatewayRequest:
    """Tests for AuditService.log_model_gateway_request."""

    def test_log_model_gateway_request_budget_denied(
        self, audit_service, db_session, test_account, test_audit_user
    ):
        """Budget denials should be logged as standalone gateway audit events."""
        audit_service.log_model_gateway_request(
            db=db_session,
            account_id=test_account.id,
            user_id=test_audit_user.id,
            api_usage_id="usage-1",
            endpoint="/openai/v1/responses",
            endpoint_kind="responses",
            status_code=403,
            outcome="budget_denied",
            requested_model="openai/gpt-5",
            model_alias="openai/gpt-5",
            provider_name="openai",
            gateway_provider="preloop",
            auth_subject_type="api_key",
            runtime_session_id="runtime-session-1",
            api_key_id="api-key-1",
            api_key_name="Runtime Token",
            error_detail="Model gateway budget exceeded: account monthly limit reached",
            error_type="budget_limit_exceeded",
            budget={"hard_limit_exceeded": True},
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        gateway_logs = [l for l in logs if l.action == "model_gateway_request"]
        assert len(gateway_logs) == 1
        assert gateway_logs[0].resource_type == "model_gateway"
        assert gateway_logs[0].resource_id == "openai/gpt-5"
        assert gateway_logs[0].status == "budget_denied"
        assert gateway_logs[0].details["status_code"] == 403
        assert gateway_logs[0].details["api_key_name"] == "Runtime Token"
        assert gateway_logs[0].details["error_type"] == "budget_limit_exceeded"

    def test_log_model_gateway_request_failure(
        self, audit_service, db_session, test_account, test_audit_user
    ):
        """Gateway failures should preserve provider and request attribution."""
        audit_service.log_model_gateway_request(
            db=db_session,
            account_id=test_account.id,
            user_id=test_audit_user.id,
            api_usage_id="usage-2",
            endpoint="/openai/v1/chat/completions",
            endpoint_kind="chat_completions",
            status_code=429,
            outcome="failed",
            requested_model="openai/gpt-5",
            model_alias="openai/gpt-5",
            provider_name="openai",
            gateway_provider="preloop",
            auth_subject_type="api_key",
            flow_execution_id="flow-exec-1",
            runtime_principal_name="Nightly Review Flow",
            error_detail="Rate limit exceeded",
            error_type="rate_limit_error",
        )

        logs = crud_audit_log.get_by_account(db_session, account_id=test_account.id)
        gateway_logs = [l for l in logs if l.action == "model_gateway_request"]
        assert len(gateway_logs) == 1
        assert gateway_logs[0].status == "failed"
        assert gateway_logs[0].details["endpoint_kind"] == "chat_completions"
        assert gateway_logs[0].details["flow_execution_id"] == "flow-exec-1"
        assert (
            gateway_logs[0].details["runtime_principal_name"] == "Nightly Review Flow"
        )
        assert gateway_logs[0].details["error_detail"] == "Rate limit exceeded"


def _raise_lifecycle(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> AuditService:
    def fail(**kwargs: object) -> None:
        raise exc

    monkeypatch.setattr(
        "preloop.services.audit.service.crud_audit_log.log_action", fail
    )
    return AuditService()


class TestApprovalLifecycleFailureClassification:
    """Expected pool and disconnect failures stay warnings. Other faults do not."""

    @pytest.mark.parametrize(
        ("exc", "fragment"),
        [
            (_pool_timeout(), "pool timeout"),
            (_disconnected("boom", invalidated=True), "disconnected"),
            *[
                (
                    _disconnected(phrase, invalidated=False),
                    "disconnected",
                )
                for phrase in _DISCONNECT_PHRASES
            ],
        ],
    )
    def test_expected_infrastructure_failures_warn_without_traceback(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
        exc: BaseException,
        fragment: str,
    ) -> None:
        """Checkout timeouts and disconnects must not alert or leak exception text."""
        caplog.set_level(logging.DEBUG, logger="preloop.services.audit.service")
        service = _raise_lifecycle(monkeypatch, exc)

        service.log_approval_lifecycle(
            db=SimpleNamespace(),
            account_id="account",
            approval_id="approval",
            event="approved",
        )

        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and record.name == "preloop.services.audit.service"
        ]
        errors = [
            record for record in caplog.records if record.levelno >= logging.ERROR
        ]
        assert len(warnings) == 1
        assert fragment in warnings[0].getMessage()
        assert warnings[0].exc_info is None
        assert errors == []
        assert _SECRET not in caplog.text
        assert "Traceback" not in caplog.text

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError(f"programming fault {_SECRET}"),
            TimeoutError(f"builtin timeout {_SECRET}"),
            IntegrityError(
                "INSERT INTO audit_log (id) VALUES (%(id)s)",
                {"id": _SECRET},
                Exception("duplicate key value violates unique constraint"),
            ),
            _deadlock(),
            _deadlock(statement="SELECT 'ssl connection has been closed' AS note"),
        ],
    )
    def test_other_failures_keep_error_diagnostics(
        self,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
        exc: BaseException,
    ) -> None:
        """Programming faults and ordinary operational errors stay diagnosable."""
        caplog.set_level(logging.DEBUG, logger="preloop.services.audit.service")
        service = _raise_lifecycle(monkeypatch, exc)

        service.log_approval_lifecycle(
            db=SimpleNamespace(),
            account_id="account",
            approval_id="approval",
            event="approved",
        )

        errors = [
            record
            for record in caplog.records
            if record.levelno >= logging.ERROR
            and record.name == "preloop.services.audit.service"
        ]
        assert len(errors) == 1
        assert errors[0].levelno == logging.ERROR
        assert errors[0].exc_info is not None
        assert "Failed to log approval lifecycle" in errors[0].getMessage()
        assert not any(
            record.levelno == logging.WARNING and "disconnected" in record.getMessage()
            for record in caplog.records
        )
