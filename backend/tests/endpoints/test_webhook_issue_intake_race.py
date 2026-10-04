"""Concurrent GitHub ``issues`` deliveries converge on one issue row (#1194).

``gh issue create --label a --label b --label c`` makes GitHub send an
``opened`` and three ``labeled`` deliveries within one second. Each ran the
webhook's get-then-create in its own transaction, so every delivery inserted
a row. These tests post signed deliveries concurrently through the real
endpoint against Postgres and assert one row survives.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.api.app import create_app
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_organization,
    crud_project,
    crud_tracker,
)
from preloop.models.crud.base import CRUDBase
from preloop.models.db.session import get_db_session
from preloop.sync.services.event_bus import get_task_publisher
from preloop.sync.trackers.github import GitHubTracker

SECRET = "local-test-webhook-secret"
REPOSITORY_ID = 424242
DELIVERIES = 4


def _committed_tracker(engine: Any) -> Iterator[SimpleNamespace]:
    suffix = uuid4().hex[:8]
    with Session(engine) as db:
        account = crud_account.create(
            db, obj_in={"organization_name": f"Example {suffix}", "is_active": True}
        )
        tracker = crud_tracker.create(
            db,
            obj_in={
                "name": "Example GitHub",
                "tracker_type": "github",
                "account_id": account.id,
                "api_key": "fake-local-only",
            },
        )
        org = crud_organization.create(
            db,
            obj_in={
                "name": "example",
                "identifier": f"example-{suffix}",
                "tracker_id": tracker.id,
                "webhook_secret": SECRET,
            },
        )
        project = crud_project.create(
            db,
            obj_in={
                "name": "project",
                "identifier": str(REPOSITORY_ID),
                "slug": "example/project",
                "organization_id": org.id,
            },
        )
        rig = SimpleNamespace(
            account_id=account.id, org_id=org.id, project_id=project.id
        )
    try:
        yield rig
    finally:
        with Session(engine) as cleanup:
            CRUDBase(models.Account).delete(cleanup, id=rig.account_id)


def _delivery(action: str, labels: list[str]) -> bytes:
    return json.dumps(
        {
            "action": action,
            "issue": {
                "id": 9000001,
                "number": 7,
                "title": "Saved filters disappear after restart",
                "body": "Steps to reproduce are in the description.",
                "state": "open",
                "labels": [{"name": name} for name in labels],
                "created_at": "2026-10-03T10:00:00Z",
                "updated_at": "2026-10-03T10:00:01Z",
                "html_url": "https://github.com/example/project/issues/7",
            },
            "repository": {"id": REPOSITORY_ID, "full_name": "example/project"},
        }
    ).encode()


@pytest.mark.asyncio
async def test_concurrent_issue_deliveries_store_one_row(db_engine: Any) -> None:
    """Opened plus three labeled deliveries racing leave exactly one row."""
    rig_iter = _committed_tracker(db_engine)
    rig = next(rig_iter)
    try:
        with (
            patch("preloop.api.app.connect_nats", new_callable=AsyncMock),
            patch("preloop.api.app.close_nats", new_callable=AsyncMock),
        ):
            app = create_app()
        publisher = AsyncMock()
        publisher.publish_task.return_value = object()

        def session_per_request() -> Iterator[Session]:
            with Session(db_engine) as db:
                yield db

        app.dependency_overrides[get_db_session] = session_per_request
        app.dependency_overrides[get_task_publisher] = lambda: publisher

        # Line every delivery up just before it looks for an existing row, so
        # the get-then-create window is hit on every run, not by luck.
        barrier = threading.Barrier(DELIVERIES, timeout=10)
        original = GitHubTracker.transform_issue

        def aligned_transform(self: Any, *args: Any, **kwargs: Any) -> Any:
            barrier.wait()
            return original(self, *args, **kwargs)

        labels: list[str] = []
        bodies = [_delivery("opened", [])]
        for name in ["bug", "frontend", "backend"]:
            labels.append(name)
            bodies.append(_delivery("labeled", list(labels)))

        async def post(client: httpx.AsyncClient, body: bytes) -> httpx.Response:
            signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
            return await client.post(
                f"/api/v1/private/webhooks/github/{rig.org_id}",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": f"sha256={signature}",
                },
            )

        with patch.object(GitHubTracker, "transform_issue", aligned_transform):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                responses = await asyncio.wait_for(
                    asyncio.gather(*(post(client, body) for body in bodies)), 30
                )

        assert [r.status_code for r in responses] == [200] * DELIVERIES
        assert all(r.json()["status"] == "success" for r in responses), [
            r.json() for r in responses
        ]
        with Session(db_engine) as db:
            rows = db.scalars(
                select(models.Issue).where(models.Issue.project_id == rig.project_id)
            ).all()
        assert len(rows) == 1
        assert rows[0].key == "example/project#7"
        assert rows[0].external_id == "9000001"
        # One event per delivery reaches the worker, all naming the same row.
        events = [
            call
            for call in publisher.publish_task.await_args_list
            if call.args[0] == "process_webhook_event"
        ]
        assert len(events) == DELIVERIES
    finally:
        next(rig_iter, None)
