"""The approval workflow's own webhook_url keeps working, now signed."""

import uuid
import pytest
from sqlalchemy import select

from preloop.models.models.tool_configuration import ApprovalWorkflow
from preloop.models.models.webhook_endpoint import (
    SOURCE_ACCOUNT,
    SOURCE_APPROVAL_WORKFLOW,
    WebhookDelivery,
    WebhookEndpoint,
)
from preloop.services.event_webhooks import approval_shim, outbox
from preloop.services.event_webhooks.events import EVENT_APPROVAL_CREATED
from preloop.services.event_webhooks.signing import SECRET_PREFIX
from preloop.utils.encryption import decrypt_value


class FakeAsync:
    """Minimal async facade over the sync test session."""

    def __init__(self, session):
        self._session = session

    def add(self, obj):
        self._session.add(obj)

    async def execute(self, statement, *args, **kwargs):
        return self._session.execute(statement, *args, **kwargs)

    async def flush(self):
        self._session.flush()


def _workflow(db_session, account_id, config):
    """A real ApprovalWorkflow row: the shim writes back to its config."""
    workflow = ApprovalWorkflow(
        account_id=account_id,
        name="Deploy approvals",
        approval_type="webhook",
        approval_config=config,
    )
    db_session.add(workflow)
    db_session.flush()
    return workflow


def _shim_rows(db_session):
    return (
        db_session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.source == SOURCE_APPROVAL_WORKFLOW
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_the_legacy_url_creates_a_hidden_endpoint(db_session, account):
    workflow = _workflow(
        db_session, account.id, {"webhook_url": "https://chat.example.com/hook"}
    )

    endpoint = await approval_shim.sync_shim_endpoint_async(
        FakeAsync(db_session), workflow
    )

    assert endpoint is not None
    assert endpoint.url == "https://chat.example.com/hook"
    assert endpoint.source == SOURCE_APPROVAL_WORKFLOW
    assert endpoint.approval_workflow_id == workflow.id
    assert endpoint.active is True
    assert len(_shim_rows(db_session)) == 1


@pytest.mark.asyncio
async def test_a_secret_is_generated_and_written_back_to_the_config(
    db_session, account
):
    workflow = _workflow(
        db_session, account.id, {"webhook_url": "https://chat.example.com/hook"}
    )

    endpoint = await approval_shim.sync_shim_endpoint_async(
        FakeAsync(db_session), workflow
    )

    secret = workflow.approval_config["webhook_secret"]
    assert secret.startswith(SECRET_PREFIX)
    assert decrypt_value(endpoint.secret_encrypted) == secret
    assert endpoint.secret_hint and secret.endswith(endpoint.secret_hint)


@pytest.mark.asyncio
async def test_an_operator_supplied_secret_is_respected(db_session, account):
    workflow = _workflow(
        db_session,
        account.id,
        {"webhook_url": "https://chat.example.com/hook", "webhook_secret": "chosen"},
    )

    endpoint = await approval_shim.sync_shim_endpoint_async(
        FakeAsync(db_session), workflow
    )

    assert workflow.approval_config["webhook_secret"] == "chosen"
    assert decrypt_value(endpoint.secret_encrypted) == "chosen"


@pytest.mark.asyncio
async def test_syncing_twice_reuses_the_same_row_and_secret(db_session, account):
    workflow = _workflow(
        db_session, account.id, {"webhook_url": "https://chat.example.com/hook"}
    )
    db = FakeAsync(db_session)

    first = await approval_shim.sync_shim_endpoint_async(db, workflow)
    secret = workflow.approval_config["webhook_secret"]
    second = await approval_shim.sync_shim_endpoint_async(db, workflow)

    assert first.id == second.id
    assert workflow.approval_config["webhook_secret"] == secret
    assert len(_shim_rows(db_session)) == 1


@pytest.mark.asyncio
async def test_changing_the_url_repoints_the_row_and_closes_the_breaker(
    db_session, account
):
    workflow = _workflow(
        db_session, account.id, {"webhook_url": "https://old.example.com/hook"}
    )
    db = FakeAsync(db_session)
    endpoint = await approval_shim.sync_shim_endpoint_async(db, workflow)
    endpoint.consecutive_failures = 9
    endpoint.circuit_opened_at = outbox._utcnow()
    db_session.flush()

    workflow.approval_config["webhook_url"] = "https://new.example.com/hook"
    updated = await approval_shim.sync_shim_endpoint_async(db, workflow)

    assert updated.id == endpoint.id
    assert updated.url == "https://new.example.com/hook"
    assert updated.consecutive_failures == 0
    assert updated.circuit_opened_at is None


@pytest.mark.asyncio
async def test_clearing_the_url_deactivates_the_row(db_session, account):
    workflow = _workflow(
        db_session, account.id, {"webhook_url": "https://chat.example.com/hook"}
    )
    db = FakeAsync(db_session)
    endpoint = await approval_shim.sync_shim_endpoint_async(db, workflow)

    workflow.approval_config["webhook_url"] = "   "
    assert await approval_shim.sync_shim_endpoint_async(db, workflow) is None

    db_session.refresh(endpoint)
    assert endpoint.active is False


@pytest.mark.asyncio
async def test_no_config_creates_nothing(db_session, account):
    workflow = _workflow(db_session, account.id, None)

    assert (
        await approval_shim.sync_shim_endpoint_async(FakeAsync(db_session), workflow)
        is None
    )
    assert _shim_rows(db_session) == []


@pytest.mark.asyncio
async def test_the_legacy_body_is_posted_verbatim(db_session, account):
    workflow = _workflow(
        db_session, account.id, {"webhook_url": "https://chat.example.com/hook"}
    )
    db = FakeAsync(db_session)
    endpoint = await approval_shim.sync_shim_endpoint_async(db, workflow)
    request_id = uuid.uuid4()
    legacy_body = {
        "type": "approval_request",
        "request_id": str(request_id),
        "tool_name": "deploy",
        "actions": {"review": "https://app.example.com/console/approval/x"},
    }

    result = await outbox.enqueue_raw_delivery_async(
        db,
        endpoint=endpoint,
        event_type=EVENT_APPROVAL_CREATED,
        payload=legacy_body,
        natural_key=f"approval_workflow_webhook:{request_id}",
        subject_id=request_id,
    )
    db_session.flush()

    row = db_session.get(WebhookDelivery, result.delivery_ids[0])
    # No v1 envelope wrapping: receivers parse exactly what they parsed before.
    assert row.payload == legacy_body
    assert "version" not in row.payload
    assert row.subject_id == request_id


@pytest.mark.asyncio
async def test_the_same_approval_is_only_queued_once(db_session, account):
    workflow = _workflow(
        db_session, account.id, {"webhook_url": "https://chat.example.com/hook"}
    )
    db = FakeAsync(db_session)
    endpoint = await approval_shim.sync_shim_endpoint_async(db, workflow)
    key = f"approval_workflow_webhook:{uuid.uuid4()}"

    first = await outbox.enqueue_raw_delivery_async(
        db,
        endpoint=endpoint,
        event_type=EVENT_APPROVAL_CREATED,
        payload={},
        natural_key=key,
    )
    second = await outbox.enqueue_raw_delivery_async(
        db,
        endpoint=endpoint,
        event_type=EVENT_APPROVAL_CREATED,
        payload={},
        natural_key=key,
    )

    assert len(first.delivery_ids) == 1
    assert second.delivery_ids == []


@pytest.mark.asyncio
async def test_shim_rows_never_receive_v1_events(db_session, account, make_endpoint):
    workflow = _workflow(
        db_session, account.id, {"webhook_url": "https://chat.example.com/hook"}
    )
    await approval_shim.sync_shim_endpoint_async(FakeAsync(db_session), workflow)
    account_endpoint, _ = make_endpoint(source=SOURCE_ACCOUNT)

    result = outbox.enqueue_event(
        db_session,
        account_id=account.id,
        event_type=EVENT_APPROVAL_CREATED,
        data={"approval_request_id": "1"},
    )

    assert result.endpoints_matched == 1
    row = db_session.get(WebhookDelivery, result.delivery_ids[0])
    assert row.endpoint_id == account_endpoint.id
