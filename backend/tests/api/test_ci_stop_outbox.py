"""Real restricted stop outbox, scoped to the execution's principal."""

from typing import Any
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.schemas.ci_subscription import CiSubscriptionCreate
from tests.api.test_ci_execution import review_binding
import pytest
from tests.api.test_ci_principal import ci_resources as create_ci_resources, provision


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    return create_ci_resources.__wrapped__(db_session)


def test_stop_enqueues_only_owned_subscription_once(
    db_session: Session, ci_resources: tuple[Any, ...]
) -> None:
    _, _, token = provision(db_session, ci_resources)
    _, _, other_token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    other = crud.crud_ci_principal.authenticate(db_session, token=other_token)
    endpoint, _ = crud.crud_ci_subscription.create(
        db_session,
        context=context,
        payload=CiSubscriptionCreate(url="https://example.com/own"),
    )
    crud.crud_ci_subscription.create(
        db_session,
        context=other,
        payload=CiSubscriptionCreate(url="https://example.com/other"),
    )
    execution = crud.crud_ci_execution.create(
        db_session, context=context, binding=review_binding(context), event={}
    )
    assert crud.crud_flow_execution.mark_stopped(
        db_session,
        execution_id=execution.id,
        error_message="budget expired",
        commit=False,
    )
    rows = db_session.query(models.WebhookDelivery).all()
    assert len(rows) == 1
    assert rows[0].endpoint_id == endpoint.id
    assert rows[0].payload["data"]["status"] == "STOPPED"
    assert rows[0].payload["data"]["head_sha"] == "a" * 40
    assert not crud.crud_flow_execution.mark_stopped(
        db_session,
        execution_id=execution.id,
        error_message="repeated stop",
        commit=False,
    )
    assert db_session.query(models.WebhookDelivery).count() == 1
    db_session.rollback()
    assert db_session.query(models.WebhookDelivery).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "recovery", ["stopped_launch", "captured", "confirmed_crash", "unconfirmed_crash"]
)
async def test_stop_outbox_defers_and_recovers_without_the_original_orchestrator(
    db_session: Session, ci_resources: tuple[Any, ...], recovery: str
) -> None:
    """A stopped execution retains a deliverable event through capture races."""
    from preloop.services.event_webhooks.worker import claim_batch
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    _, _, token = provision(db_session, ci_resources)
    context = crud.crud_ci_principal.authenticate(db_session, token=token)
    crud.crud_ci_subscription.create(
        db_session,
        context=context,
        payload=CiSubscriptionCreate(url="https://example.com/own"),
    )
    execution = crud.crud_ci_execution.create(
        db_session, context=context, binding=review_binding(context), event={}
    )
    moment = datetime.now(timezone.utc)
    crud.crud_flow_execution.update(
        db_session,
        db_obj=execution,
        obj_in={
            "launch_requested_at": moment,
            "agent_session_reference": None
            if recovery == "stopped_launch"
            else "synthetic-runtime",
            "orchestrator_worker_id": "synthetic-worker",
            "orchestrator_heartbeat_at": moment,
        },
    )
    crud.crud_flow_execution.record_completion_evidence(
        db_session,
        execution_id=execution.id,
        facts={"capture_state": "pending"},
    )
    crud.crud_flow_execution.mark_stopped(
        db_session,
        execution_id=execution.id,
        error_message="stopped",
        confirmed=recovery == "confirmed_crash",
        now=moment,
    )
    assert db_session.query(models.WebhookDelivery).count() == 1
    assert (
        claim_batch(
            db_session, now=(moment + timedelta(seconds=1)).replace(tzinfo=None)
        )
        == []
    )
    delivery = db_session.query(models.WebhookDelivery).one()
    assert delivery.status == "pending"
    assert delivery.attempt_count == 0

    if recovery == "stopped_launch":
        orch = FlowExecutionOrchestrator(db_session, execution.flow_id, {}, AsyncMock())
        orch.execution_log = execution
        orch.flow = crud.crud_flow.get(db_session, id=execution.flow_id)
        with (
            patch.object(orch, "_publish_update", AsyncMock()),
            patch.object(orch, "_sync_runtime_session"),
            patch.object(orch, "_update_commit_status", AsyncMock()),
        ):
            await orch._finish_stopped_launch()
    elif recovery == "captured":
        # Capture committed, then the controller died before its final emit.
        crud.crud_flow_execution.record_completion_evidence(
            db_session,
            execution_id=execution.id,
            facts={
                "capture_state": "captured",
                "result_captured_at": moment.isoformat(),
            },
            result={"review": "Done"},
            validation={"valid": True},
        )
    prepared = claim_batch(
        db_session, now=(moment + timedelta(minutes=10)).replace(tzinfo=None)
    )
    if recovery == "unconfirmed_crash":
        assert prepared == []
        assert delivery.status == "pending"
        assert delivery.attempt_count == 0
        assert execution.startup_receipt["_completion"]["capture_state"] == "pending"
    else:
        assert len(prepared) == 1
        ready = crud.crud_ci_subscription.prepare_delivery(
            db_session, delivery_id=prepared[0].delivery_id
        )
        assert ready is not None
        data = ready[0].payload["data"]
        assert data["status"] == "STOPPED"
        assert data["result_ready"] is (recovery == "captured")
        assert execution.startup_receipt["_completion"]["capture_state"] != "pending"
    assert db_session.query(models.WebhookDelivery).count() == 1
