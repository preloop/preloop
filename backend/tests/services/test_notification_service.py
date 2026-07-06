"""Tests for NotificationService channels, partitioning and approver expansion.

External services (email, httpx/webhooks, APNs) are mocked so the tests are
hermetic. Push-channel behaviour is covered separately in
test_notification_service_push.py; this module covers the remaining surface.
"""

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_team,
    crud_user,
    notification_preferences,
)

from preloop.services.notification_service import NotificationService

pytestmark = pytest.mark.asyncio


@pytest.fixture
def account(db_session):
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Org", "is_active": True}
    )
    db_session.commit()
    return account


def make_user(
    db_session, account, email, username, enable_email=False, enable_push=False
):
    user = crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "email": email,
            "username": username,
            "full_name": username,
            "is_active": True,
        },
    )
    db_session.commit()
    prefs = notification_preferences.get_or_create(db_session, user.id)
    prefs.enable_email = enable_email
    prefs.enable_mobile_push = enable_push
    if enable_push:
        prefs.add_device_token("ios", "a" * 64)
    db_session.commit()
    return user


@pytest.fixture
def approval_request(account):
    return models.ApprovalRequest(
        id=uuid.uuid4(),
        account_id=str(account.id),
        tool_configuration_id=uuid.uuid4(),
        approval_workflow_id=uuid.uuid4(),
        tool_name="create_issue",
        tool_args={"title": "T", "api_key": "secret"},
        agent_reasoning="reasoning",
        status="pending",
        approval_token="tok123",
        requested_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(minutes=5),
    )


def make_workflow(**overrides):
    base = {
        "approver_user_ids": None,
        "approver_team_ids": None,
        "escalation_user_ids": None,
        "escalation_team_ids": None,
        "channel_configs": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestPartitionUsersByChannel:
    async def test_partition_each_bucket(self, db_session, account):
        push_user = make_user(db_session, account, "p@e.com", "p", enable_push=True)
        email_user = make_user(db_session, account, "e@e.com", "e", enable_email=True)
        both_user = make_user(
            db_session, account, "b@e.com", "b", enable_email=True, enable_push=True
        )
        none_user = make_user(db_session, account, "n@e.com", "n")

        svc = NotificationService(db_session)
        push_only, email_only, both = svc._partition_users_by_channel(
            [push_user.id, email_user.id, both_user.id, none_user.id]
        )
        assert push_only == [push_user.id]
        assert email_only == [email_user.id]
        assert both == [both_user.id]

    async def test_user_without_prefs_skipped(self, db_session, account):
        # User id that has no notification preferences row
        svc = NotificationService(db_session)
        push_only, email_only, both = svc._partition_users_by_channel([uuid.uuid4()])
        assert push_only == [] and email_only == [] and both == []

    async def test_push_enabled_without_token_is_not_push(self, db_session, account):
        user = make_user(db_session, account, "x@e.com", "x", enable_email=True)
        prefs = notification_preferences.get_by_user(db_session, user.id)
        prefs.enable_mobile_push = True  # enabled but no ios token registered
        db_session.commit()

        svc = NotificationService(db_session)
        push_only, email_only, both = svc._partition_users_by_channel([user.id])
        # No ios token -> treated as email-only
        assert email_only == [user.id]
        assert push_only == [] and both == []


class TestApproverExpansion:
    async def test_direct_user_ids(self, db_session):
        svc = NotificationService(db_session)
        u1, u2 = uuid.uuid4(), uuid.uuid4()
        wf = make_workflow(approver_user_ids=[u1, u2])
        ids = await svc._get_approver_user_ids(wf)
        assert set(ids) == {u1, u2}

    async def test_team_expansion(self, db_session, account):
        member = make_user(db_session, account, "m@e.com", "m")
        team = crud_team.create(
            db_session, obj_in={"account_id": account.id, "name": "Team A"}
        )
        db_session.commit()
        crud_team.add_member(db_session, team_id=team.id, user_id=member.id)

        svc = NotificationService(db_session)
        wf = make_workflow(approver_team_ids=[team.id])
        ids = await svc._get_approver_user_ids(wf)
        assert member.id in ids

    async def test_empty_when_nothing_configured(self, db_session):
        svc = NotificationService(db_session)
        ids = await svc._get_approver_user_ids(make_workflow())
        assert ids == []

    async def test_escalation_user_ids(self, db_session):
        svc = NotificationService(db_session)
        u1 = uuid.uuid4()
        ids = await svc._get_escalation_user_ids(
            make_workflow(escalation_user_ids=[u1])
        )
        assert ids == [u1]

    async def test_escalation_team_expansion(self, db_session, account):
        member = make_user(db_session, account, "esc@e.com", "esc")
        team = crud_team.create(
            db_session, obj_in={"account_id": account.id, "name": "Esc Team"}
        )
        db_session.commit()
        crud_team.add_member(db_session, team_id=team.id, user_id=member.id)

        svc = NotificationService(db_session)
        ids = await svc._get_escalation_user_ids(
            make_workflow(escalation_team_ids=[team.id])
        )
        assert member.id in ids


class TestSendEmailNotifications:
    async def test_no_eligible_users(self, db_session, account, approval_request):
        user = make_user(
            db_session, account, "noemail@e.com", "noem", enable_email=False
        )
        svc = NotificationService(db_session)
        result = await svc._send_email_notifications(approval_request, [user.id])
        assert result["sent"] == 0
        assert result["skipped"] == 1

    async def test_sends_to_eligible_user(self, db_session, account, approval_request):
        user = make_user(db_session, account, "ok@e.com", "ok", enable_email=True)
        svc = NotificationService(db_session)
        with patch(
            "preloop.utils.email.send_approval_request_email", new=AsyncMock()
        ) as mock_send:
            result = await svc._send_email_notifications(approval_request, [user.id])
        assert result["success"] is True
        assert result["sent"] == 1
        mock_send.assert_awaited_once()

    async def test_escalation_uses_escalation_email(
        self, db_session, account, approval_request
    ):
        user = make_user(db_session, account, "esc2@e.com", "esc2", enable_email=True)
        svc = NotificationService(db_session)
        with patch(
            "preloop.utils.email.send_escalation_email", new=MagicMock()
        ) as mock_esc:
            result = await svc._send_email_notifications(
                approval_request, [user.id], is_escalation=True
            )
        assert result["is_escalation"] is True
        assert result["sent"] == 1
        mock_esc.assert_called_once()

    async def test_send_failure_counts(self, db_session, account, approval_request):
        user = make_user(db_session, account, "fail@e.com", "fail", enable_email=True)
        svc = NotificationService(db_session)
        with patch(
            "preloop.utils.email.send_approval_request_email",
            new=AsyncMock(side_effect=RuntimeError("smtp down")),
        ):
            result = await svc._send_email_notifications(approval_request, [user.id])
        assert result["success"] is False
        assert result["failed"] == 1


class TestWebhookChannels:
    async def _patched_client(self, raise_for_status=None):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(side_effect=raise_for_status)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mock_client)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm, mock_client

    async def test_slack_missing_url(self, db_session, approval_request):
        svc = NotificationService(db_session)
        wf = make_workflow(channel_configs={"slack": {}})
        result = await svc._send_slack_notification(approval_request, wf)
        assert result["success"] is False
        assert "webhook" in result["error"].lower()

    async def test_slack_success(self, db_session, approval_request):
        svc = NotificationService(db_session)
        wf = make_workflow(
            channel_configs={"slack": {"webhook_url": "https://hooks.slack/x"}}
        )
        cm, client = await self._patched_client()
        with patch("httpx.AsyncClient", return_value=cm):
            result = await svc._send_slack_notification(approval_request, wf)
        assert result == {"success": True, "channel": "slack"}
        client.post.assert_awaited_once()

    async def test_mattermost_success(self, db_session, approval_request):
        svc = NotificationService(db_session)
        wf = make_workflow(
            channel_configs={"mattermost": {"webhook_url": "https://mm/x"}}
        )
        cm, client = await self._patched_client()
        with patch("httpx.AsyncClient", return_value=cm):
            result = await svc._send_mattermost_notification(approval_request, wf)
        assert result == {"success": True, "channel": "mattermost"}

    async def test_webhook_missing_url(self, db_session, approval_request):
        svc = NotificationService(db_session)
        wf = make_workflow(channel_configs={"webhook": {}})
        result = await svc._send_webhook_notification(approval_request, wf)
        assert result["success"] is False

    async def test_webhook_success_with_custom_headers(
        self, db_session, approval_request
    ):
        svc = NotificationService(db_session)
        wf = make_workflow(
            channel_configs={
                "webhook": {
                    "url": "https://hook/x",
                    "headers": {"X-Token": "abc"},
                }
            }
        )
        cm, client = await self._patched_client()
        with patch("httpx.AsyncClient", return_value=cm):
            result = await svc._send_webhook_notification(approval_request, wf)
        assert result["success"] is True
        # Custom header merged into request
        sent_headers = client.post.await_args.kwargs["headers"]
        assert sent_headers["X-Token"] == "abc"
        # Sensitive tool_args should be redacted in payload
        sent_json = client.post.await_args.kwargs["json"]
        assert sent_json["tool_args"]["api_key"] != "secret"

    async def test_webhook_http_error(self, db_session, approval_request):
        svc = NotificationService(db_session)
        wf = make_workflow(channel_configs={"webhook": {"url": "https://hook/x"}})
        cm, client = await self._patched_client(raise_for_status=RuntimeError("500"))
        with patch("httpx.AsyncClient", return_value=cm):
            result = await svc._send_webhook_notification(approval_request, wf)
        assert result["success"] is False
        assert "500" in result["error"]


class TestBuildChatWebhookPayload:
    async def test_payload_structure(self, db_session, approval_request):
        svc = NotificationService(db_session)
        payload = svc._build_chat_webhook_payload(approval_request)
        assert "text" in payload
        assert payload["attachments"][0]["title_link"].endswith(
            f"?token={approval_request.approval_token}"
        )
        assert "create_issue" in payload["text"]
        # Reasoning present -> a field is inserted at the front
        assert payload["attachments"][0]["fields"][0]["title"] == "Agent Reasoning"

    async def test_payload_redacts_args(self, db_session, approval_request):
        svc = NotificationService(db_session)
        payload = svc._build_chat_webhook_payload(approval_request)
        # The raw secret should not leak into the chat text
        assert "secret" not in payload["text"]


class TestNotifyEscalation:
    async def test_no_escalation_approvers(self, db_session, approval_request):
        svc = NotificationService(db_session)
        wf = make_workflow()
        result = await svc.notify_escalation(approval_request, wf)
        assert result == {"error": "No escalation approvers configured"}


class TestDelayedEmail:
    async def test_skips_when_not_pending(self, db_session, account, approval_request):
        svc = NotificationService(db_session)
        # No matching ApprovalRequest row exists in the DB -> current is None
        send_mock = AsyncMock()
        with patch.object(svc, "_send_email_notifications", new=send_mock):
            await svc._send_delayed_email(
                request_id=approval_request.id,
                approval_request=approval_request,
                user_ids=[uuid.uuid4()],
                delay_seconds=0,
            )
        send_mock.assert_not_awaited()
