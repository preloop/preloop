"""The documented ``channel_configs.webhook`` form dispatches, signed.

Regression tests for a workflow configured as the docs and policy YAML
describe (``channel_configs.webhook.url``) never sending anything, for the
signing secret being unreachable, and for a null or fragment ``summary``.
"""

import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from preloop.models.models.tool_configuration import ApprovalWorkflow
from preloop.models.models.webhook_endpoint import (
    SOURCE_APPROVAL_WORKFLOW,
    WebhookEndpoint,
)
from preloop.models.schemas.tool_configuration import ApprovalWorkflowResponse
from preloop.services import approval_summary
from preloop.services.approval_service import ApprovalService
from preloop.services.event_webhooks import approval_shim, outbox
from preloop.utils.encryption import decrypt_value

HOOK = "https://receiver.example.com/approvals"


def _wf(**kwargs):
    base = {"approval_type": "slack", "approval_config": None, "channel_configs": None}
    base.update(kwargs)
    return SimpleNamespace(**base)


class TestResolveWebhookTarget:
    def test_documented_channel_configs_webhook_url(self):
        workflow = _wf(channel_configs={"webhook": {"url": HOOK}})
        assert approval_shim.resolve_webhook_target(workflow) == ("webhook", HOOK)

    def test_policy_yaml_default_approval_type_does_not_matter(self):
        """Policy apply stores approval_type as the mode, never 'webhook'."""
        workflow = _wf(
            approval_type="standard", channel_configs={"webhook": {"url": HOOK}}
        )
        assert approval_shim.resolve_webhook_target(workflow) == ("webhook", HOOK)

    def test_slack_channel_config_uses_webhook_url_key(self):
        workflow = _wf(
            channel_configs={"slack": {"webhook_url": "https://hooks.example/x"}}
        )
        assert approval_shim.resolve_webhook_target(workflow) == (
            "slack",
            "https://hooks.example/x",
        )

    def test_legacy_approval_config_keeps_working(self):
        workflow = _wf(
            approval_type="mattermost", approval_config={"webhook_url": HOOK}
        )
        assert approval_shim.resolve_webhook_target(workflow) == ("mattermost", HOOK)

    def test_nothing_configured(self):
        assert approval_shim.resolve_webhook_target(_wf()) is None
        assert (
            approval_shim.resolve_webhook_target(
                _wf(channel_configs={"webhook": {"url": "  "}})
            )
            is None
        )


def _request(summary=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tool_name="get_weekly_summary",
        tool_args={"week": 0, "owner": "did:web:sandbox.example.org:u:abc123"},
        summary=summary,
        agent_reasoning=None,
        status="pending",
        requested_at=datetime(2026, 10, 1, 12, 0, 0),
        expires_at=None,
        approval_token="tok",
    )


def _capture(monkeypatch):
    captured = {}

    async def fake_sync(db, workflow):
        captured["workflow"] = workflow
        return MagicMock()

    async def fake_enqueue(db, **kwargs):
        captured.update(kwargs)
        return outbox.EnqueueResult(
            event_id=uuid.uuid4(),
            delivery_ids=[uuid.uuid4()],
            endpoints_matched=1,
            skipped_queue_full=0,
        )

    monkeypatch.setattr(approval_shim, "sync_shim_endpoint_async", fake_sync)
    monkeypatch.setattr(outbox, "enqueue_raw_delivery_async", fake_enqueue)
    return captured


@pytest.mark.asyncio
async def test_documented_webhook_config_queues_the_generic_payload(monkeypatch):
    captured = _capture(monkeypatch)
    service = ApprovalService(AsyncMock(), "https://app.example.com")
    workflow = _wf(channel_configs={"webhook": {"url": HOOK}})

    assert await service.post_webhook_notification(_request(), workflow) is True

    payload = captured["payload"]
    assert payload["type"] == "approval_request"
    assert "decision" in payload


@pytest.mark.asyncio
async def test_send_notifications_dispatches_the_documented_webhook(monkeypatch):
    service = ApprovalService(AsyncMock(), "https://app.example.com")
    workflow = _wf(approval_type="standard", channel_configs={"webhook": {"url": HOOK}})
    monkeypatch.setattr(service, "_resolve_bypass", AsyncMock(return_value=None))
    monkeypatch.setattr(
        service, "_get_all_approver_user_ids", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        service,
        "_partition_approvers_for_stagger",
        AsyncMock(
            return_value={
                k: []
                for k in (
                    "push_capable",
                    "email_only",
                    "both_stagger",
                    "both_immediate",
                    "all_email",
                )
            }
        ),
    )
    monkeypatch.setattr(
        service, "_send_push_notification", AsyncMock(return_value={"success": True})
    )
    monkeypatch.setattr(
        service, "_send_email_notification", AsyncMock(return_value={"success": True})
    )
    monkeypatch.setattr(service, "_audit_notification_result", AsyncMock())
    post = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "post_webhook_notification", post)
    request = _request()
    request.requested_at = datetime.utcnow()
    request.account_id = uuid.uuid4()
    request.managed_agent_id = None

    results = await service.send_notifications(request, workflow)

    post.assert_awaited_once()
    assert results["webhook"] == {"success": True}


@pytest.mark.asyncio
async def test_webhook_summary_is_never_null(monkeypatch):
    captured = _capture(monkeypatch)
    service = ApprovalService(AsyncMock(), "https://app.example.com")
    workflow = _wf(channel_configs={"webhook": {"url": HOOK}})

    await service.post_webhook_notification(_request(summary=None), workflow)

    summary = captured["payload"]["summary"]
    assert summary
    assert summary.startswith("Allow get_weekly_summary")
    assert "did:web:sandbox.example.org:u:abc123" in summary


def _completion(text, finish_reason="stop"):
    choice = SimpleNamespace(
        message=SimpleNamespace(content=text, reasoning_content=None),
        finish_reason=finish_reason,
    )
    return SimpleNamespace(choices=[choice])


def _call(monkeypatch, response):
    import litellm

    monkeypatch.setattr(litellm, "completion", lambda **kwargs: response)
    monkeypatch.setattr(
        approval_summary, "check_reasoning_model_empty_content", lambda r: None
    )
    monkeypatch.setattr(
        approval_summary,
        "build_aux_kwargs",
        lambda m, c, call_site_kwargs: dict(call_site_kwargs),
    )
    monkeypatch.setattr(approval_summary, "_to_litellm_model", lambda m: "test/model")
    return approval_summary._call_summary_model(
        MagicMock(),
        {},
        tool_name="get_weekly_summary",
        tool_args={"owner": "did:web:sandbox.example.org:u:abc"},
        agent_reasoning=None,
        managed_agent_name=None,
    )


def test_summary_cut_off_at_max_tokens_is_rejected(monkeypatch):
    with pytest.raises(ValueError, match="truncated"):
        _call(monkeypatch, _completion("sandbox.examp", finish_reason="length"))


def test_summary_fragment_is_rejected(monkeypatch):
    with pytest.raises(ValueError, match="fragment"):
        _call(monkeypatch, _completion("sandbox.examp"))


def test_one_word_and_unspaced_summaries_are_kept(monkeypatch):
    assert _call(monkeypatch, _completion("Deploy?")) == "Deploy?"
    assert _call(monkeypatch, _completion("週次サマリーを許可しますか")) == (
        "週次サマリーを許可しますか"
    )


def test_summary_sentence_is_kept(monkeypatch):
    text = "Allow the agent to read the weekly summary for this owner?"
    assert _call(monkeypatch, _completion(text)) == text


def test_fallback_summary_redacts_and_skips_markers():
    summary = approval_summary.fallback_approval_summary(
        "deploy", {"env": "prod", "api_key": "sk-123", "_preloop_marker": 1}
    )
    assert summary.startswith("Allow deploy with env=prod")
    assert "sk-123" not in summary
    assert "_preloop_marker" not in summary


class _FakeAsync:
    def __init__(self, session):
        self._session = session

    def add(self, obj):
        self._session.add(obj)

    async def execute(self, statement, *args, **kwargs):
        return self._session.execute(statement, *args, **kwargs)

    async def flush(self):
        self._session.flush()


@pytest.mark.asyncio
async def test_channel_configs_webhook_creates_the_signed_endpoint(
    db_session, test_user
):
    workflow = ApprovalWorkflow(
        account_id=test_user.account_id,
        name="Consent",
        approval_type="slack",
        channel_configs={"webhook": {"url": HOOK}},
    )
    db_session.add(workflow)
    db_session.flush()

    endpoint = await approval_shim.sync_shim_endpoint_async(
        _FakeAsync(db_session), workflow
    )

    assert endpoint is not None and endpoint.url == HOOK
    secret = workflow.approval_config["webhook_secret"]
    assert decrypt_value(endpoint.secret_encrypted) == secret


@pytest.mark.asyncio
async def test_rotated_secret_reaches_the_endpoint(db_session, test_user):
    workflow = ApprovalWorkflow(
        account_id=test_user.account_id,
        name="Consent rotate",
        approval_type="slack",
        channel_configs={"webhook": {"url": HOOK}},
    )
    db_session.add(workflow)
    db_session.flush()
    await approval_shim.sync_shim_endpoint_async(_FakeAsync(db_session), workflow)

    new_secret = approval_shim.rotate_webhook_secret(workflow)
    approval_shim.sync_shim_endpoint(db_session, workflow)

    endpoint = (
        db_session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.source == SOURCE_APPROVAL_WORKFLOW,
                WebhookEndpoint.approval_workflow_id == workflow.id,
            )
        )
        .scalars()
        .one()
    )
    assert decrypt_value(endpoint.secret_encrypted) == new_secret


def test_workflow_response_hides_the_stored_secret():
    now = datetime(2026, 10, 1)
    response = ApprovalWorkflowResponse(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        name="wf",
        approval_type="slack",
        is_default=False,
        approval_mode="standard",
        ai_confidence_threshold=0.8,
        ai_fallback_behavior="escalate",
        approval_config={"webhook_url": HOOK, "webhook_secret": "whsec_abcdWXYZ"},
        created_at=now,
        updated_at=now,
    )
    dumped = response.model_dump()
    assert "whsec_abcdWXYZ" not in str(dumped)
    assert dumped["webhook_secret"] is None
    assert dumped["webhook_secret_hint"] == "WXYZ"
    assert dumped["approval_config"] == {"webhook_url": HOOK}
