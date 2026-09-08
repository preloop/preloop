"""Webhook DB waits and NATS waits must not amplify each other."""

import asyncio
import threading
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from preloop.api.endpoints import webhooks
from preloop.sync.scanner.core import TrackerClient
from preloop.models.crud import crud_issue_embedding
from preloop.sync import tasks


@pytest.mark.asyncio
async def test_rejected_webhook_releases_read_transaction_before_notification(
    monkeypatch: Any,
) -> None:
    engine = create_engine(
        "sqlite://",
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        connect_args={"check_same_thread": False},
    )

    def missing_organization(db: Session, **kwargs: Any) -> None:
        db.connection()
        assert engine.pool.checkedout() == 1

    published: list[str] = []

    async def publish(*args: Any, **kwargs: Any) -> object:
        assert engine.pool.checkedout() == 0
        published.append(args[0])
        return object()

    monkeypatch.setattr(
        webhooks.crud_organization, "get_with_tracker", missing_organization
    )
    request = MagicMock(body=AsyncMock(return_value=b"{}"), headers={})
    with Session(engine) as db:
        with pytest.raises(HTTPException) as error:
            await webhooks.receive_webhook(
                "gitlab", "missing", request, db, MagicMock(publish_task=publish)
            )
        assert error.value.status_code == 404
    assert engine.pool.checkedout() == 0
    assert published == ["notify_admins"]
    engine.dispose()


@pytest.mark.asyncio
async def test_webhook_pool_checkout_keeps_loop_responsive(monkeypatch: Any) -> None:
    entered = threading.Event()
    release = threading.Event()

    def wait_for_pool(*args: Any, **kwargs: Any) -> None:
        entered.set()
        release.wait(2)

    monkeypatch.setattr(webhooks.crud_organization, "get_with_tracker", wait_for_pool)
    request = MagicMock(body=AsyncMock(return_value=b"{}"), headers={})
    with Session(create_engine("sqlite://")) as db:
        task = asyncio.create_task(
            webhooks.receive_webhook(
                "gitlab", "missing", request, db, MagicMock(publish_task=AsyncMock())
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.sleep(0)
            assert not task.done(), "webhook checkout blocked the event loop"
        finally:
            release.set()
            with pytest.raises(HTTPException) as error:
                await task
            assert error.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledged", [True, False])
@pytest.mark.parametrize("inline_error", [True, False])
async def test_event_forwarding_and_timestamp_require_ack(
    monkeypatch: Any,
    acknowledged: bool,
    inline_error: bool,
) -> None:
    engine = create_engine(
        "sqlite://",
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        connect_args={"check_same_thread": False},
    )
    tracker = SimpleNamespace(
        id="tracker", is_active=True, subscribed_events=["Issue Hook", "Job Hook"]
    )
    organization = SimpleNamespace(
        id="organization", tracker=tracker, webhook_secret="secret"
    )

    def resolve(db: Session, **kwargs: Any) -> Any:
        db.connection()
        return organization

    monkeypatch.setattr(webhooks.crud_organization, "get_with_tracker", resolve)
    timestamp = MagicMock()
    monkeypatch.setattr(webhooks.crud_organization, "touch_webhook", timestamp)
    # A job notification should never construct/authenticate a tracker client.
    constructor = MagicMock(side_effect=AssertionError("unnecessary tracker auth"))
    monkeypatch.setattr(webhooks, "TrackerClient", constructor)
    published: list[str] = []

    async def publish(name: str, **kwargs: Any) -> Any:
        assert engine.pool.checkedout() == 0
        published.append(name)
        return object() if acknowledged else None

    request = MagicMock(
        body=AsyncMock(return_value=b"{}"),
        headers={
            "X-Gitlab-Token": "secret",
            "X-Gitlab-Event": "Issue Hook" if inline_error else "Job Hook",
        },
    )
    with Session(engine) as db:
        if acknowledged:
            result = await webhooks.receive_webhook(
                "gitlab", "organization", request, db, MagicMock(publish_task=publish)
            )
            assert result["status"] == (
                "partial_success" if inline_error else "success"
            )
        else:
            with pytest.raises(HTTPException) as error:
                await webhooks.receive_webhook(
                    "gitlab",
                    "organization",
                    request,
                    db,
                    MagicMock(publish_task=publish),
                )
            assert error.value.status_code == 503
    assert published == ["process_webhook_event"]
    assert timestamp.call_count == int(acknowledged and not inline_error)
    constructor.assert_not_called()
    assert engine.pool.checkedout() == 0
    engine.dispose()


@pytest.mark.parametrize("provider", ["gitlab", "jira", "github"])
def test_payload_transform_client_never_authenticates(
    provider: str, monkeypatch: Any
) -> None:
    gitlab = MagicMock(side_effect=AssertionError("GitLab network initialization"))
    jira = MagicMock(side_effect=AssertionError("Jira network initialization"))
    monkeypatch.setattr("preloop.sync.trackers.gitlab.gitlab.Gitlab", gitlab)
    monkeypatch.setattr("preloop.sync.trackers.jira.JIRA", jira)
    tracker = SimpleNamespace(
        id="tracker",
        tracker_type=provider,
        connection_details={},
        url="https://tracker.invalid",
    )
    # No resolved_api_key or OAuth attributes are supplied: transforms must
    # neither load credentials nor call the provider's authentication endpoint.
    client = TrackerClient(tracker, initialize_client=False)
    result = client.client.transform_comment(
        {
            "id": 1,
            "body": "comment",
            "note": "comment",
            "created_at": "2026-09-08T00:00:00Z",
            "updated_at": "2026-09-08T00:00:00Z",
        },
        "issue",
    )
    assert result["body"] == "comment"
    gitlab.assert_not_called()
    jira.assert_not_called()


@pytest.mark.asyncio
async def test_issue_webhook_queues_embedding_work_without_waiting_for_provider(
    monkeypatch: Any,
) -> None:
    engine = create_engine(
        "sqlite://",
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        connect_args={"check_same_thread": False},
    )
    issue_id = uuid4()
    organization = SimpleNamespace(
        id=uuid4(),
        webhook_secret="secret",
        tracker=SimpleNamespace(
            id=uuid4(), is_active=True, subscribed_events=["Issue Hook"]
        ),
    )

    def resolve(db: Session, **kwargs: Any) -> Any:
        db.connection()
        return organization

    monkeypatch.setattr(webhooks.crud_organization, "get_with_tracker", resolve)
    monkeypatch.setattr(webhooks.crud_organization, "touch_webhook", MagicMock())
    monkeypatch.setattr(
        webhooks.crud_project,
        "get_by_identifier",
        MagicMock(return_value=SimpleNamespace(id=uuid4(), slug="repo")),
    )
    monkeypatch.setattr(
        webhooks.crud_issue, "get_by_external_id", MagicMock(return_value=None)
    )
    monkeypatch.setattr(
        webhooks.crud_issue,
        "create",
        MagicMock(return_value=SimpleNamespace(id=issue_id)),
    )
    monkeypatch.setattr(
        webhooks,
        "TrackerClient",
        MagicMock(
            return_value=SimpleNamespace(
                client=SimpleNamespace(
                    transform_issue=lambda payload, project: {"external_id": "2"}
                )
            )
        ),
    )
    provider = MagicMock(side_effect=AssertionError("provider must run only in worker"))
    monkeypatch.setattr(crud_issue_embedding, "create_embeddings", provider)
    publications: list[dict[str, Any]] = []

    async def publish(name: str, **kwargs: Any) -> object:
        assert engine.pool.checkedout() == 0
        assert name == "process_webhook_event"
        publications.append(kwargs)
        return object()

    request = MagicMock(
        body=AsyncMock(
            return_value=json.dumps(
                {
                    "project": {"id": 1},
                    "object_attributes": {"id": 2, "iid": 3, "title": "issue"},
                }
            ).encode()
        ),
        headers={"X-Gitlab-Token": "secret", "X-Gitlab-Event": "Issue Hook"},
    )
    with Session(engine) as db:
        result = await webhooks.receive_webhook(
            "gitlab", str(organization.id), request, db, MagicMock(publish_task=publish)
        )
    assert result["status"] == "success"
    assert publications[0]["embedding_requests"] == [
        {"issue_id": issue_id, "force_update": True}
    ]
    provider.assert_not_called()
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("embedding_fails", [False, True])
async def test_worker_generates_embeddings_before_flow_without_holding_flow_session(
    monkeypatch: Any,
    embedding_fails: bool,
) -> None:
    order: list[str] = []
    flow_db = MagicMock()

    def get_session() -> Any:
        order.append("open_flow_db")
        yield flow_db

    def generate(db: Any, request: dict[str, Any]) -> None:
        assert not order
        order.append("embedding")
        if embedding_fails:
            raise RuntimeError("synthetic provider failure")

    def run_owned(operation: Any) -> Any:
        with Session() as owned:
            return operation(owned)

    async def process(event: dict[str, Any]) -> None:
        order.append("flow")

    monkeypatch.setattr(tasks, "get_db_session", get_session)
    monkeypatch.setattr(tasks, "_generate_webhook_embeddings", generate)
    monkeypatch.setattr(tasks, "run_db_sync", run_owned)
    monkeypatch.setattr(
        tasks.crud_tracker,
        "get",
        MagicMock(
            return_value=SimpleNamespace(
                id="tracker",
                tracker_type="gitlab",
                account_id="account",
            )
        ),
    )
    monkeypatch.setattr(
        "preloop.services.flow_trigger_service.FlowTriggerService",
        MagicMock(return_value=SimpleNamespace(process_event=process)),
    )
    await tasks.process_webhook_event(
        "tracker",
        "Job Hook",
        {},
        embedding_requests=[{"issue_id": "issue"}],
    )
    assert order == ["embedding", "open_flow_db", "flow"]
    flow_db.close.assert_called_once()
