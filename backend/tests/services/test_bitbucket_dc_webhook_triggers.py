"""Replay safety and trigger semantics for Bitbucket Data Center deliveries.

Events are built from the recorded 10.2 fixtures exactly as
``sync.tasks.process_webhook_event`` builds them, then run through the real
``FlowTriggerService`` against PostgreSQL.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.sync.event_normalizer import extract_filter_fields, normalize_event_type
from preloop.utils.bitbucket_dc import parse_instance_url
from preloop.utils.bitbucket_dc_webhooks import normalize_delivery
from tests.services.test_webhook_delivery_idempotency import (
    executions_for,
    without_in_process_run,
)

INSTANCE = parse_instance_url(
    "https://bitbucket.example.com/bitbucket", allow_path=True
)
FIXTURES: Dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "bitbucket_dc"
        / "webhooks_10.2.json"
    ).read_text()
)["events"]


def dc_event(
    key: str,
    *,
    account_id: Any,
    tracker_id: Any,
    delivery_id: Optional[str] = "req-1",
    mutate: Any = None,
    self_user_slug: Optional[str] = None,
) -> Dict[str, Any]:
    raw = copy.deepcopy(FIXTURES[key])
    if mutate:
        mutate(raw)
    delivery = normalize_delivery(
        key,
        raw,
        instance=INSTANCE,
        bound_repository_id=42,
        bound_project_key="PRJ",
        self_user_slug=self_user_slug,
    )
    assert delivery.accepted, delivery.reason
    payload = delivery.payload
    event = {
        "source": "bitbucket_dc",
        "tracker_id": str(tracker_id),
        "type": normalize_event_type("bitbucket_dc", key, payload),
        "payload": {**payload, **extract_filter_fields("bitbucket_dc", key, payload)},
        "account_id": str(account_id),
    }
    if delivery_id:
        event["delivery_id"] = f"bitbucket_dc:{tracker_id}:{delivery_id}"
    return event


def set_head(raw: Dict[str, Any], sha: str, version: int, previous: str) -> None:
    raw["pullRequest"]["fromRef"]["latestCommit"] = sha
    raw["pullRequest"]["version"] = version
    raw["previousFromHash"] = previous


@pytest.fixture
def flow(db_session: Session, test_user, test_tracker) -> models.Flow:
    row = models.Flow(
        name="DC reviewer",
        prompt_template="review",
        agent_type="codex",
        agent_config={},
        account_id=test_user.account_id,
        trigger_event_source=str(test_tracker.id),
        trigger_event_types=[
            "pull_request_opened",
            "pull_request_updated",
            "comment_created",
        ],
        webhook_config={"supersede_on_update": True},
        is_enabled=True,
    )
    db_session.add(row)
    db_session.flush()
    return row


async def deliver(service: FlowTriggerService, flow: models.Flow, event: dict) -> None:
    with (
        patch(
            "preloop.services.flow_trigger_service.crud_flow.get_by_trigger",
            return_value=[flow],
        ),
        patch(
            "preloop.services.flow_trigger_service.get_nats_client",
            new=AsyncMock(return_value=None),
        ),
        without_in_process_run(),
    ):
        await service.process_event(copy.deepcopy(event))


@pytest.mark.asyncio
async def test_duplicate_delivery_creates_exactly_one_execution(
    db_session: Session, flow, test_user, test_tracker
) -> None:
    service = FlowTriggerService(db_session)
    event = dc_event(
        "pr:opened", account_id=test_user.account_id, tracker_id=test_tracker.id
    )
    for _ in range(3):
        await deliver(service, flow, event)
    rows = executions_for(db_session, flow.id)
    assert len(rows) == 1
    assert rows[0].webhook_delivery_key == (
        f"delivery:bitbucket_dc:{test_tracker.id}:req-1"
    )
    assert rows[0].trigger_event_details["payload"]["pull_request"]["number"] == 101


@pytest.mark.asyncio
async def test_handler_failure_is_retried_without_a_duplicate(
    db_session: Session, flow, test_user, test_tracker
) -> None:
    service = FlowTriggerService(db_session)
    event = dc_event(
        "pr:opened", account_id=test_user.account_id, tracker_id=test_tracker.id
    )
    with patch.object(
        FlowTriggerService,
        "_start_flow_execution",
        new=AsyncMock(side_effect=RuntimeError("worker crashed")),
    ):
        await deliver(service, flow, event)
    assert executions_for(db_session, flow.id) == []
    await deliver(service, flow, event)
    await deliver(service, flow, event)
    assert len(executions_for(db_session, flow.id)) == 1


@pytest.mark.asyncio
async def test_out_of_order_head_does_not_supersede_the_newer_run(
    db_session: Session, flow, test_user, test_tracker
) -> None:
    service = FlowTriggerService(db_session)
    older, newer = "a" * 40, "b" * 40
    new_event = dc_event(
        "pr:from_ref_updated",
        account_id=test_user.account_id,
        tracker_id=test_tracker.id,
        delivery_id="req-new",
        mutate=lambda raw: set_head(raw, newer, 6, older),
    )
    late_event = dc_event(
        "pr:from_ref_updated",
        account_id=test_user.account_id,
        tracker_id=test_tracker.id,
        delivery_id="req-old",
        mutate=lambda raw: set_head(raw, older, 5, "c" * 40),
    )
    await deliver(service, flow, new_event)
    await deliver(service, flow, late_event)
    rows = executions_for(db_session, flow.id)
    assert len(rows) == 1
    assert rows[0].trigger_event_details["payload"]["pull_request"]["head"]["sha"] == (
        newer
    )
    assert rows[0].status != "STOPPED"


@pytest.mark.asyncio
async def test_late_older_head_after_the_newer_run_finished_is_skipped(
    db_session: Session, flow, test_user, test_tracker
) -> None:
    service = FlowTriggerService(db_session)
    older, newer = "a" * 40, "b" * 40
    await deliver(
        service,
        flow,
        dc_event(
            "pr:from_ref_updated",
            account_id=test_user.account_id,
            tracker_id=test_tracker.id,
            delivery_id="req-new",
            mutate=lambda raw: set_head(raw, newer, 6, older),
        ),
    )
    finished = executions_for(db_session, flow.id)
    assert len(finished) == 1
    finished[0].status = "SUCCEEDED"
    db_session.flush()
    await deliver(
        service,
        flow,
        dc_event(
            "pr:from_ref_updated",
            account_id=test_user.account_id,
            tracker_id=test_tracker.id,
            delivery_id="req-old",
            mutate=lambda raw: set_head(raw, older, 5, "c" * 40),
        ),
    )
    assert [e.id for e in executions_for(db_session, flow.id)] == [finished[0].id]


@pytest.mark.asyncio
async def test_new_head_in_order_supersedes_the_older_run(
    db_session: Session, flow, test_user, test_tracker
) -> None:
    service = FlowTriggerService(db_session)
    first, second = "a" * 40, "b" * 40
    await deliver(
        service,
        flow,
        dc_event(
            "pr:from_ref_updated",
            account_id=test_user.account_id,
            tracker_id=test_tracker.id,
            delivery_id="req-1",
            mutate=lambda raw: set_head(raw, first, 5, "c" * 40),
        ),
    )
    with patch(
        "preloop.services.flow_trigger_service.FlowTriggerService._stop_for_pull_request",
        new=AsyncMock(return_value=True),
    ) as stop:
        await deliver(
            service,
            flow,
            dc_event(
                "pr:from_ref_updated",
                account_id=test_user.account_id,
                tracker_id=test_tracker.id,
                delivery_id="req-2",
                mutate=lambda raw: set_head(raw, second, 6, first),
            ),
        )
    assert stop.await_count == 1
    assert stop.await_args.kwargs["object_key"] == "bitbucket_dc:42:pr:101"


@pytest.mark.asyncio
async def test_self_generated_comment_does_not_trigger(
    db_session: Session, flow, test_user, test_tracker
) -> None:
    service = FlowTriggerService(db_session)
    event = dc_event(
        "pr:comment:added",
        account_id=test_user.account_id,
        tracker_id=test_tracker.id,
        self_user_slug="rev",
    )
    await deliver(service, flow, event)
    assert executions_for(db_session, flow.id) == []
    human = dc_event(
        "pr:comment:added",
        account_id=test_user.account_id,
        tracker_id=test_tracker.id,
        self_user_slug="preloop-bot",
        delivery_id="req-2",
    )
    await deliver(service, flow, human)
    assert len(executions_for(db_session, flow.id)) == 1


@pytest.mark.asyncio
async def test_feedback_maps_to_the_existing_thread_not_a_new_run(
    db_session: Session, flow, test_user, test_tracker
) -> None:
    flow.agent_config = {"feedback": {"enabled": True}}
    db_session.flush()
    origin = models.FlowExecution(
        flow_id=flow.id,
        status="SUCCEEDED",
        trigger_event_details={},
    )
    db_session.add(origin)
    db_session.flush()
    now = datetime.now(UTC).replace(tzinfo=None)
    thread = models.FlowThread(
        id=uuid.uuid4(),
        account_id=test_user.account_id,
        flow_id=flow.id,
        tracker_id=test_tracker.id,
        repository_id="42",
        pr_number="101",
        pr_url=(
            "https://bitbucket.example.com/bitbucket/projects/PRJ/repos/my-repo"
            "/pull-requests/101"
        ),
        provider="bitbucket_dc",
        branch="feature/retry",
        context={},
        policy={},
        cursor={},
        state="waiting",
        latest_execution_id=origin.id,
        due_at=now + timedelta(hours=1),
        expires_at=now + timedelta(days=7),
    )
    db_session.add(thread)
    db_session.flush()

    service = FlowTriggerService(db_session)
    event = dc_event(
        "pr:comment:added", account_id=test_user.account_id, tracker_id=test_tracker.id
    )
    await deliver(service, flow, event)
    await deliver(service, flow, event)

    feedback = (
        db_session.query(models.FlowFeedback)
        .filter(models.FlowFeedback.thread_id == thread.id)
        .all()
    )
    assert len(feedback) == 1
    assert feedback[0].delivery_id == f"bitbucket_dc:{test_tracker.id}:req-1"
    assert [e.id for e in executions_for(db_session, flow.id)] == [origin.id]


@pytest.mark.asyncio
async def test_simultaneous_deliveries_create_exactly_one_execution(
    db_engine, test_user
) -> None:
    """Two workers race the same delivery on separate committed connections."""
    setup = Session(bind=db_engine)
    flow_id = None
    account_id = None
    tracker_id = None
    try:
        from preloop.models.crud import crud_account

        account = crud_account.create(
            setup, obj_in={"organization_name": "DC race", "is_active": True}
        )
        setup.flush()
        account_id = account.id
        tracker = models.Tracker(
            name="DC",
            tracker_type="bitbucket_dc",
            url="https://bitbucket.example.com/bitbucket",
            account_id=account_id,
            is_active=True,
        )
        setup.add(tracker)
        setup.flush()
        tracker_id = tracker.id
        row = models.Flow(
            name="DC race flow",
            prompt_template="review",
            agent_type="codex",
            agent_config={},
            account_id=account_id,
            trigger_event_source=str(tracker_id),
            trigger_event_types=["pull_request_opened"],
            is_enabled=True,
        )
        setup.add(row)
        setup.commit()
        flow_id = row.id
        event = dc_event("pr:opened", account_id=account_id, tracker_id=tracker_id)
        barrier = asyncio.Barrier(2)

        async def worker() -> None:
            session = Session(bind=db_engine)
            try:
                service = FlowTriggerService(session)
                flow_row = session.get(models.Flow, flow_id)
                await barrier.wait()
                await deliver(service, flow_row, event)
            finally:
                session.close()

        await asyncio.gather(worker(), worker())
        check = Session(bind=db_engine)
        try:
            assert len(executions_for(check, flow_id)) == 1
        finally:
            check.close()
    finally:
        cleanup = Session(bind=db_engine)
        try:
            if flow_id is not None:
                cleanup.execute(
                    text("DELETE FROM flow_execution WHERE flow_id = :id"),
                    {"id": flow_id},
                )
                cleanup.execute(
                    text("DELETE FROM flow WHERE id = :id"), {"id": flow_id}
                )
            if tracker_id is not None:
                cleanup.execute(
                    text("DELETE FROM tracker WHERE id = :id"), {"id": tracker_id}
                )
            if account_id is not None:
                cleanup.execute(
                    text("DELETE FROM account WHERE id = :id"), {"id": account_id}
                )
            cleanup.commit()
        finally:
            cleanup.close()
        setup.close()
