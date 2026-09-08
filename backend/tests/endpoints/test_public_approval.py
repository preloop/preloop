"""Tests for public approval API endpoints (token-based, no auth required)."""

import logging
import uuid
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from preloop.api.endpoints.public_approval import DECISION_FAILED_DETAIL
from preloop.models.crud import crud_approval_workflow
from preloop.models.models.approval_request import ApprovalRequest
from preloop.models.models.tool_configuration import ToolConfiguration
from preloop.models.schemas.tool_configuration import ApprovalWorkflowCreate


class TestPublicApprovalGetData:
    """Test GET /approval/{request_id}/data endpoint."""

    def test_get_approval_data_success(self, client: TestClient, db_session, test_user):
        """Test GET approval data with valid token returns request details."""
        # Create approval workflow
        workflow = crud_approval_workflow.create(
            db_session,
            obj_in=ApprovalWorkflowCreate(name="Test Workflow", approval_type="manual"),
            account_id=str(test_user.account_id),
        )
        db_session.flush()

        # Create tool configuration
        tool_config = ToolConfiguration(
            tool_name="test_tool",
            tool_source="builtin",
            account_id=test_user.account_id,
            approval_workflow_id=workflow.id,
        )
        db_session.add(tool_config)
        db_session.flush()

        # Create approval request with known token
        approval_token = "test-token-12345"
        approval_request = ApprovalRequest(
            account_id=test_user.account_id,
            tool_configuration_id=tool_config.id,
            approval_workflow_id=workflow.id,
            execution_id="exec-1",
            tool_name="test_tool",
            tool_args={"arg1": "value1"},
            agent_reasoning="Test reasoning",
            status="pending",
            requested_at=datetime.now(UTC),
            approval_token=approval_token,
        )
        db_session.add(approval_request)
        db_session.flush()

        response = client.get(
            f"/approval/{approval_request.id}/data",
            params={"token": approval_token},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == str(approval_request.id)
        assert data["tool_name"] == "test_tool"
        assert data["tool_args"] == {"arg1": "value1"}
        assert data["agent_reasoning"] == "Test reasoning"
        assert data["status"] == "pending"
        assert "requested_at" in data

    def test_get_approval_data_invalid_token(
        self, client: TestClient, db_session, test_user
    ):
        """Test GET approval data with invalid token returns 404."""
        request_id = uuid.uuid4()
        response = client.get(
            f"/approval/{request_id}/data",
            params={"token": "invalid-token"},
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_get_approval_data_missing_token(self, client: TestClient):
        """Test GET approval data without token returns 422."""
        request_id = uuid.uuid4()
        response = client.get(f"/approval/{request_id}/data")
        assert response.status_code == 422


class TestPublicApprovalDecide:
    """Test POST /approval/{request_id}/decide endpoint."""

    def test_decide_approval_invalid_token(self, client: TestClient):
        """Test POST decide with invalid token returns 404."""
        request_id = uuid.uuid4()
        response = client.post(
            f"/approval/{request_id}/decide",
            params={"token": "invalid-token"},
            json={"action": "approve", "comment": None},
        )
        assert response.status_code == 404

    def test_decide_approval_invalid_action(
        self, client: TestClient, db_session, test_user
    ):
        """Test POST decide with invalid action returns 400."""
        # Create minimal approval request
        workflow = crud_approval_workflow.create(
            db_session,
            obj_in=ApprovalWorkflowCreate(name="Test WF", approval_type="manual"),
            account_id=str(test_user.account_id),
        )
        db_session.flush()

        tool_config = ToolConfiguration(
            tool_name="test_tool",
            tool_source="builtin",
            account_id=test_user.account_id,
            approval_workflow_id=workflow.id,
        )
        db_session.add(tool_config)
        db_session.flush()

        approval_token = "decide-test-token"
        approval_request = ApprovalRequest(
            account_id=test_user.account_id,
            tool_configuration_id=tool_config.id,
            approval_workflow_id=workflow.id,
            execution_id="exec-1",
            tool_name="test_tool",
            tool_args={},
            status="pending",
            requested_at=datetime.now(UTC),
            approval_token=approval_token,
        )
        db_session.add(approval_request)
        db_session.flush()

        with patch(
            "preloop.api.endpoints.public_approval.get_async_db_session"
        ) as mock_get_session:
            mock_session = AsyncMock()
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with patch(
                "preloop.api.endpoints.public_approval.ApprovalService"
            ) as mock_service_cls:
                mock_service = AsyncMock()
                mock_service_cls.return_value = mock_service

                response = client.post(
                    f"/approval/{approval_request.id}/decide",
                    params={"token": approval_token},
                    json={"action": "invalid_action", "comment": None},
                )
                assert response.status_code == 400
                assert "invalid" in response.json()["detail"].lower()

    def test_decide_approval_success(self, client: TestClient, db_session, test_user):
        """Test POST decide with approve action succeeds."""
        workflow = crud_approval_workflow.create(
            db_session,
            obj_in=ApprovalWorkflowCreate(name="Test WF", approval_type="manual"),
            account_id=str(test_user.account_id),
        )
        db_session.flush()

        tool_config = ToolConfiguration(
            tool_name="test_tool",
            tool_source="builtin",
            account_id=test_user.account_id,
            approval_workflow_id=workflow.id,
        )
        db_session.add(tool_config)
        db_session.flush()

        approval_token = "decide-approve-token"
        approval_request = ApprovalRequest(
            account_id=test_user.account_id,
            tool_configuration_id=tool_config.id,
            approval_workflow_id=workflow.id,
            execution_id="exec-1",
            tool_name="test_tool",
            tool_args={},
            status="pending",
            requested_at=datetime.now(UTC),
            approval_token=approval_token,
        )
        db_session.add(approval_request)
        db_session.flush()

        from preloop.models.models.approval_event import ApprovalEvent

        db_session.add(
            ApprovalEvent(
                approval_request_id=approval_request.id,
                account_id=test_user.account_id,
                event_type="notification_sent",
                detail="Notification via email to jane@example.com (sent)",
            )
        )
        db_session.flush()

        updated_request = MagicMock()
        updated_request.id = approval_request.id
        updated_request.tool_name = "test_tool"
        updated_request.tool_args = {}
        updated_request.agent_reasoning = None
        updated_request.status = "approved"
        updated_request.requested_at = approval_request.requested_at
        updated_request.expires_at = None
        updated_request.resolved_at = datetime.now(UTC)

        with patch(
            "preloop.api.endpoints.public_approval.get_async_db_session"
        ) as mock_get_session:
            mock_session = AsyncMock()
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with patch(
                "preloop.api.endpoints.public_approval.ApprovalService"
            ) as mock_service_cls:
                mock_service = AsyncMock()
                mock_service.approve_request = AsyncMock(return_value=updated_request)
                mock_service_cls.return_value = mock_service

                response = client.post(
                    f"/approval/{approval_request.id}/decide",
                    params={"token": approval_token},
                    json={"action": "approve", "comment": "Looks good"},
                )
                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "approved"
                assert data["id"] == str(approval_request.id)
                assert data["resolved_at"] is not None
                assert isinstance(data["history"], list)
                assert data["history"], "decide must return the existing timeline"
                assert "jane@example.com" not in str(data)
                notified = next(
                    e for e in data["history"] if e["event_type"] == "notification_sent"
                )
                assert "1 recipient" in notified["detail"]


class TestPublicApprovalDecideDoesNotLeakInternals:
    """The decision endpoint authenticates with a link token only.

    Nothing in a response body may vary with the internal cause of a failure,
    and nothing the caller sends may be reflected back. Operators read the
    cause in the logs.
    """

    def _pending_request(self, db_session, test_user, token: str) -> ApprovalRequest:
        workflow = crud_approval_workflow.create(
            db_session,
            obj_in=ApprovalWorkflowCreate(name="Test WF", approval_type="manual"),
            account_id=str(test_user.account_id),
        )
        db_session.flush()

        tool_config = ToolConfiguration(
            tool_name="test_tool",
            tool_source="builtin",
            account_id=test_user.account_id,
            approval_workflow_id=workflow.id,
        )
        db_session.add(tool_config)
        db_session.flush()

        approval_request = ApprovalRequest(
            account_id=test_user.account_id,
            tool_configuration_id=tool_config.id,
            approval_workflow_id=workflow.id,
            execution_id="exec-1",
            tool_name="test_tool",
            tool_args={},
            status="pending",
            requested_at=datetime.now(UTC),
            approval_token=token,
        )
        db_session.add(approval_request)
        db_session.flush()
        return approval_request

    def test_invalid_action_is_not_reflected_back(
        self, client: TestClient, db_session, test_user
    ):
        """The submitted action must not appear in the response body."""
        approval_request = self._pending_request(
            db_session, test_user, "no-reflect-token"
        )
        payload = "<img src=x onerror=alert(1)>"

        response = client.post(
            f"/approval/{approval_request.id}/decide",
            params={"token": "no-reflect-token"},
            json={"action": payload, "comment": None},
        )

        assert response.status_code == 400
        assert payload not in response.text
        assert response.json()["detail"] == (
            "Invalid action. Expected 'approve' or 'decline'."
        )

    def test_service_exception_yields_a_fixed_sentence(
        self, client: TestClient, db_session, test_user
    ):
        """An internal failure returns one fixed sentence, and logs the rest."""
        approval_request = self._pending_request(db_session, test_user, "boom-token")
        secret = (
            'relation "approval_request" does not exist at '
            "/app/preloop/services/approval_service.py line 981"
        )

        # The module logger does not propagate to the root under the JSON
        # formatter, so caplog sees nothing. Attach a handler directly.
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Capture()
        module_logger = logging.getLogger("preloop.api.endpoints.public_approval")
        module_logger.addHandler(handler)
        try:
            with patch(
                "preloop.api.endpoints.public_approval.get_async_db_session"
            ) as mock_get_session:
                mock_session = AsyncMock()
                mock_get_session.return_value.__aenter__.return_value = mock_session

                with patch(
                    "preloop.api.endpoints.public_approval.ApprovalService"
                ) as mock_service_cls:
                    mock_service = AsyncMock()
                    mock_service.approve_request = AsyncMock(
                        side_effect=RuntimeError(secret)
                    )
                    mock_service_cls.return_value = mock_service

                    response = client.post(
                        f"/approval/{approval_request.id}/decide",
                        params={"token": "boom-token"},
                        json={"action": "approve", "comment": None},
                    )
        finally:
            module_logger.removeHandler(handler)

        assert response.status_code == 500
        assert response.json()["detail"] == DECISION_FAILED_DETAIL
        assert secret not in response.text
        assert "approval_service.py" not in response.text

        logged = [record for record in records if record.exc_info]
        assert logged, "the failure must be logged with its traceback"
        assert str(logged[-1].exc_info[1]) == secret

    def test_missing_updated_request_yields_the_same_sentence(
        self, client: TestClient, db_session, test_user
    ):
        """A None from the service is indistinguishable from any other failure."""
        approval_request = self._pending_request(db_session, test_user, "none-token")

        with patch(
            "preloop.api.endpoints.public_approval.get_async_db_session"
        ) as mock_get_session:
            mock_session = AsyncMock()
            mock_get_session.return_value.__aenter__.return_value = mock_session

            with patch(
                "preloop.api.endpoints.public_approval.ApprovalService"
            ) as mock_service_cls:
                mock_service = AsyncMock()
                mock_service.approve_request = AsyncMock(return_value=None)
                mock_service_cls.return_value = mock_service

                response = client.post(
                    f"/approval/{approval_request.id}/decide",
                    params={"token": "none-token"},
                    json={"action": "approve", "comment": None},
                )

        assert response.status_code == 500
        assert response.json()["detail"] == DECISION_FAILED_DETAIL


WAIVER_SCHEMA = {
    "type": "object",
    "properties": {
        "waived": {
            "type": "array",
            "title": "Findings you accept",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "enum": ["CVE-1", "CVE-2"]},
                    "reason": {"type": "string", "minLength": 3},
                },
                "required": ["id", "reason"],
            },
        },
        "author": {"type": "string", "x-autofill": "author"},
    },
    "required": [],
}

WAIVER_ITEMS = [
    {"id": "CVE-1", "title": "openssl 1.1.1", "severity": "critical"},
    {"id": "CVE-2", "title": "curl 7.50", "severity": "high"},
]


def _question_args():
    """tool_args for an ask_user that wants a filled-in form back."""
    return {
        "is_question": True,
        "question": "Which findings do you accept?",
        "options": [],
        "allow_free_text": True,
        "items": WAIVER_ITEMS,
        "input_schema": WAIVER_SCHEMA,
    }


class TestPublicApprovalForm:
    """The token link is a different door into the same decision, so it gets
    the same form and the same validation, not a looser one."""

    def _seed(self, db_session, test_user, token):
        workflow = crud_approval_workflow.create(
            db_session,
            obj_in=ApprovalWorkflowCreate(name="Form WF", approval_type="manual"),
            account_id=str(test_user.account_id),
        )
        db_session.flush()
        tool_config = ToolConfiguration(
            tool_name="ask_user",
            tool_source="builtin",
            account_id=test_user.account_id,
            approval_workflow_id=workflow.id,
        )
        db_session.add(tool_config)
        db_session.flush()
        approval_request = ApprovalRequest(
            account_id=test_user.account_id,
            tool_configuration_id=tool_config.id,
            approval_workflow_id=workflow.id,
            execution_id="exec-form",
            tool_name="ask_user",
            tool_args=_question_args(),
            status="pending",
            requested_at=datetime.now(UTC),
            approval_token=token,
        )
        db_session.add(approval_request)
        db_session.flush()
        return approval_request

    def test_get_data_carries_the_form(self, client: TestClient, db_session, test_user):
        request = self._seed(db_session, test_user, "form-token-get")
        response = client.get(
            f"/approval/{request.id}/data", params={"token": "form-token-get"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["question"] == "Which findings do you accept?"
        assert [item["id"] for item in data["question_items"]] == ["CVE-1", "CVE-2"]
        assert data["question_schema"]["properties"]["waived"]["type"] == "array"

    def test_approve_stores_the_validated_answer(
        self, client: TestClient, db_session, test_user
    ):
        request = self._seed(db_session, test_user, "form-token-ok")
        updated = MagicMock()
        updated.id = request.id
        updated.tool_name = "ask_user"
        updated.tool_args = _question_args()
        updated.agent_reasoning = None
        updated.status = "approved"
        updated.requested_at = request.requested_at
        updated.expires_at = None
        updated.resolved_at = datetime.now(UTC)

        with patch(
            "preloop.api.endpoints.public_approval.get_async_db_session"
        ) as mock_get_session:
            mock_get_session.return_value.__aenter__.return_value = AsyncMock()
            with patch(
                "preloop.api.endpoints.public_approval.ApprovalService"
            ) as mock_service_cls:
                mock_service = AsyncMock()
                mock_service.approve_request = AsyncMock(return_value=updated)
                mock_service_cls.return_value = mock_service
                response = client.post(
                    f"/approval/{request.id}/decide",
                    params={"token": "form-token-ok"},
                    json={
                        "action": "approve",
                        "answer": {
                            "waived": [{"id": "CVE-1", "reason": "Not reachable"}]
                        },
                    },
                )
        assert response.status_code == 200
        stored = mock_service.approve_request.await_args.kwargs["structured_answer"]
        assert stored["waived"] == [{"id": "CVE-1", "reason": "Not reachable"}]
        # A token proves someone was sent the link, not who they are, so the
        # autofilled author stays empty rather than being invented.
        assert stored.get("author") in (None, "")

    def test_bad_answer_is_refused_with_field_paths(
        self, client: TestClient, db_session, test_user
    ):
        request = self._seed(db_session, test_user, "form-token-bad")
        response = client.post(
            f"/approval/{request.id}/decide",
            params={"token": "form-token-bad"},
            json={"action": "approve", "answer": {"waived": [{"id": "CVE-9"}]}},
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        paths = {error["path"] for error in detail["errors"]}
        assert any(path.startswith("waived[0]") for path in paths)


class TestPublicApprovalTemplate:
    """The token page draws the form itself (no Lit bundle out here), so the
    template has to carry the renderer, the validation, and the payload."""

    @staticmethod
    def _template():
        from pathlib import Path

        import preloop

        path = Path(preloop.__file__).parent / "templates" / "approval.html"
        return path.read_text()

    def test_template_renders_the_schema(self):
        template = self._template()
        for marker in (
            "function renderAnswerForm(",
            "function renderRowTable(",
            "function renderIdChecklist(",
            "answerSchema = approvalData.question_schema",
            "answerItems = approvalData.question_items",
        ):
            assert marker in template, f"missing: {marker}"

    def test_template_validates_before_posting(self):
        template = self._template()
        assert "function validateAnswer(" in template
        assert "body.answer = answerPayload();" in template
        assert "showAnswerErrors(detail.errors)" in template

    def test_template_never_posts_autofilled_fields(self):
        template = self._template()
        payload = template.split("function answerPayload(")[1].split("return payload")[
            0
        ]
        assert "if (properties[name]['x-autofill']) continue;" in payload


class TestPublicApprovalPage:
    """GET /approval/{id} is the public HTML page, only with a token."""

    def test_bare_path_redirects_to_console(self, client: TestClient):
        request_id = uuid.uuid4()
        response = client.get(f"/approval/{request_id}", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == f"/console/approval/{request_id}"

    def test_empty_token_redirects_to_console(self, client: TestClient):
        request_id = uuid.uuid4()
        response = client.get(
            f"/approval/{request_id}",
            params={"token": "  "},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"] == f"/console/approval/{request_id}"

    def test_token_query_serves_public_html(self, client: TestClient):
        request_id = uuid.uuid4()
        response = client.get(
            f"/approval/{request_id}",
            params={"token": "email-token"},
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")
        assert b"token" in response.content or b"Approval" in response.content

    def test_token_query_still_serves_html_for_non_uuid(self, client: TestClient):
        """Email/Slack `?token=` must keep serving the public page."""
        response = client.get(
            "/approval/not-a-uuid",
            params={"token": "email-token"},
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")

    def test_non_uuid_request_id_does_not_redirect(self, client: TestClient):
        """Reject unvalidated path segments so Location cannot be attacker-controlled."""
        response = client.get("/approval/not-a-uuid", follow_redirects=False)
        assert response.status_code == 404
        assert "location" not in response.headers

    def test_open_redirect_payload_does_not_302(self, client: TestClient):
        response = client.get(
            "/approval/https:%2F%2Fevil.example",
            follow_redirects=False,
        )
        assert response.status_code == 404
        location = response.headers.get("location", "")
        assert location == ""
        assert "evil.example" not in location


class TestPublicApprovalExpiredAndHistory:
    """Expired requests must stay viewable and expose their timeline (#335)."""

    def _make_request(
        self,
        db_session,
        test_user,
        *,
        status: str,
        token: str,
    ):
        from preloop.models.models.approval_event import ApprovalEvent

        workflow = crud_approval_workflow.create(
            db_session,
            obj_in=ApprovalWorkflowCreate(name="WF " + status, approval_type="manual"),
            account_id=str(test_user.account_id),
        )
        db_session.flush()

        tool_config = ToolConfiguration(
            tool_name="test_tool",
            tool_source="builtin",
            account_id=test_user.account_id,
            approval_workflow_id=workflow.id,
        )
        db_session.add(tool_config)
        db_session.flush()

        approval_request = ApprovalRequest(
            account_id=test_user.account_id,
            tool_configuration_id=tool_config.id,
            approval_workflow_id=workflow.id,
            tool_name="test_tool",
            tool_args={"arg1": "value1"},
            status=status,
            requested_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) - timedelta(minutes=1)
            if status == "expired"
            else datetime.now(UTC) + timedelta(minutes=5),
            resolved_at=datetime.now(UTC) if status != "pending" else None,
            approval_token=token,
        )
        db_session.add(approval_request)
        db_session.flush()

        db_session.add(
            ApprovalEvent(
                approval_request_id=approval_request.id,
                account_id=test_user.account_id,
                event_type="approval_requested",
                detail="Approval requested for tool 'test_tool'",
            )
        )
        db_session.add(
            ApprovalEvent(
                approval_request_id=approval_request.id,
                account_id=test_user.account_id,
                event_type="notification_sent",
                detail="Notification via email to jane@example.com (sent)",
            )
        )
        db_session.add(
            ApprovalEvent(
                approval_request_id=approval_request.id,
                account_id=test_user.account_id,
                event_type="expired",
                detail="Expired: no response within the approval window",
            )
        )
        db_session.flush()
        return approval_request

    def test_get_approval_data_returns_expired_request(
        self, client: TestClient, db_session, test_user
    ):
        """An expired request is retrievable with its token — not an error."""
        approval_request = self._make_request(
            db_session, test_user, status="expired", token="expired-token-1"
        )

        response = client.get(
            f"/approval/{approval_request.id}/data",
            params={"token": "expired-token-1"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "expired"
        assert data["resolved_at"] is not None

    def test_get_approval_data_includes_history(
        self, client: TestClient, db_session, test_user
    ):
        """The public payload carries the timeline (without actor identities)."""
        approval_request = self._make_request(
            db_session, test_user, status="expired", token="expired-token-2"
        )

        response = client.get(
            f"/approval/{approval_request.id}/data",
            params={"token": "expired-token-2"},
        )
        assert response.status_code == 200
        history = response.json()["history"]
        types = [event["event_type"] for event in history]
        assert "approval_requested" in types
        assert "expired" in types
        assert all("actor_id" not in event for event in history)
        payload = response.json()
        assert "jane@example.com" not in str(payload)
        notified = next(e for e in history if e["event_type"] == "notification_sent")
        assert "email" in notified["detail"]
        assert "1 recipient" in notified["detail"]
        assert "@" not in notified["detail"]

    def test_get_approval_data_records_viewed_event(
        self, client: TestClient, db_session, test_user
    ):
        """Opening the link lands a single anonymous `viewed` entry."""
        approval_request = self._make_request(
            db_session, test_user, status="pending", token="viewed-token-1"
        )

        response = client.get(
            f"/approval/{approval_request.id}/data",
            params={"token": "viewed-token-1"},
        )
        assert response.status_code == 200
        assert "viewed" in [event["event_type"] for event in response.json()["history"]]

        # Second load is deduped: still exactly one viewed entry.
        client.get(
            f"/approval/{approval_request.id}/data",
            params={"token": "viewed-token-1"},
        )
        response = client.get(
            f"/approval/{approval_request.id}/data",
            params={"token": "viewed-token-1"},
        )
        viewed_count = sum(
            1 for event in response.json()["history"] if event["event_type"] == "viewed"
        )
        assert viewed_count == 1


class TestPublicEventDetailRedaction:
    """ApprovalEventPublic must never serialize recipient emails."""

    def test_notification_sent_becomes_a_recipient_count(self):
        from preloop.models.schemas.approval_request import public_event_detail

        redacted = public_event_detail(
            "notification_sent",
            "Notification via email to jane@example.com (sent)",
        )
        assert redacted == "Notification via email to 1 recipient (sent)"
        assert "@" not in redacted

    def test_notification_sent_counts_truncated_lists(self):
        from preloop.models.schemas.approval_request import public_event_detail

        redacted = public_event_detail(
            "notification_sent",
            "Notification via email to a@x.com, b@x.com, c@x.com, "
            "d@x.com, e@x.com (+2 more) (sent)",
        )
        assert redacted == "Notification via email to 7 recipients (sent)"

    def test_notification_sent_without_recipients_unchanged_shape(self):
        from preloop.models.schemas.approval_request import public_event_detail

        redacted = public_event_detail(
            "notification_sent", "Notification via slack (failed)"
        )
        assert redacted == "Notification via slack (failed)"

    def test_other_events_still_strip_emails(self):
        from preloop.models.schemas.approval_request import public_event_detail

        redacted = public_event_detail(
            "expired", "Timed out; last pinged jane@example.com"
        )
        assert "jane@example.com" not in redacted
        assert "[redacted]" in redacted

    def test_vote_received_drops_actor_uuid(self):
        from preloop.models.schemas.approval_request import public_event_detail

        voter = "123e4567-e89b-12d3-a456-426614174000"
        redacted = public_event_detail(
            "vote_received",
            f"Approved by {voter} (1/2) via console",
        )
        assert voter not in redacted
        assert redacted == "Approved by an approver (1/2) via console"

    def test_vote_received_drops_email_voter(self):
        from preloop.models.schemas.approval_request import public_event_detail

        redacted = public_event_detail(
            "vote_received", "Approved by jane@example.com via console"
        )
        assert "jane@example.com" not in redacted
        assert redacted == "Approved by an approver via console"

    def test_vote_received_drops_anonymous_label(self):
        from preloop.models.schemas.approval_request import public_event_detail

        redacted = public_event_detail(
            "vote_received", "Declined by anonymous (1 decline(s))"
        )
        assert "anonymous" not in redacted
        assert redacted == "Declined by an approver (1 decline(s))"

    def test_approval_event_public_redacts_vote_actor_id(self):
        from datetime import datetime, timezone

        from preloop.models.schemas.approval_request import ApprovalEventPublic

        voter = "123e4567-e89b-12d3-a456-426614174000"
        event = ApprovalEventPublic(
            event_type="vote_received",
            detail=f"Approved by {voter} (1/2) via console",
            comment=None,
            timestamp=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        assert voter not in event.detail
        assert event.detail == "Approved by an approver (1/2) via console"
