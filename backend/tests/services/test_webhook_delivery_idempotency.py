"""One provider delivery must never create two flow executions.

Regression cover for the 2026-09-08 incident: a single GitHub `labeled`
delivery produced two executions of the issue-implementation flow (and two
pull requests) because the worker pod holding the message was drained
mid-handler by a rolling deploy, naked its in-flight message after the
execution row had already been committed, and the surviving pod processed the
redelivery from scratch.

These tests exercise the durable guard, not the message layer: sequential
redelivery through ``process_event``, a genuine two-connection race on the
partial unique index, the GitLab UUID path, the no-delivery-id fallback, and
the migration's up/down.
"""

import asyncio
import importlib.util
import threading
import uuid
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.services.webhook_delivery_dedupe import (
    CONTENT_PREFIX,
    DELIVERY_PREFIX,
    content_fingerprint,
    delivery_key_for_event,
    find_execution_for_delivery,
)

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop"
    / "models"
    / "alembic"
    / "versions"
    / "20260908_webhook_delivery_key.py"
)

DELIVERY_ID = "34a92ff0-ab81-11f1-8ac0-7e096712461d"


def github_label_event(
    *,
    delivery_id: str | None = DELIVERY_ID,
    account_id: str,
    tracker_id: str | None = None,
    issue_number: int = 506,
) -> dict:
    """The shape ``sync.tasks.process_webhook_event`` builds for the incident."""
    event: dict = {
        "source": "github",
        "type": "issue_labeled",
        "account_id": account_id,
        "tracker_id": tracker_id or str(uuid.uuid4()),
        "payload": {
            "action": "labeled",
            "added_labels": ["agent-ready"],
            "issue": {"id": 991, "number": issue_number, "title": "Ship it"},
            "label": {"name": "agent-ready"},
            "repository": {"id": 42, "full_name": "preloop/preloop"},
        },
    }
    if delivery_id is not None:
        event["delivery_id"] = delivery_id
    return event


def gitlab_label_event(*, delivery_id: str | None, account_id: str) -> dict:
    return {
        "source": "gitlab",
        "type": "issue_labeled",
        "account_id": account_id,
        "tracker_id": str(uuid.uuid4()),
        "delivery_id": delivery_id,
        "payload": {
            "object_kind": "issue",
            "action": "update",
            "added_labels": ["agent-ready"],
            "object_attributes": {"id": 12, "iid": 77, "action": "update"},
            "project": {"id": 9, "path_with_namespace": "preloop/preloop"},
        },
    }


# --------------------------------------------------------------------------
# Key derivation (no database)
# --------------------------------------------------------------------------


def test_github_delivery_header_becomes_the_key() -> None:
    key = delivery_key_for_event(github_label_event(account_id=str(uuid.uuid4())))
    assert key == f"{DELIVERY_PREFIX}{DELIVERY_ID}"


def test_gitlab_event_uuid_becomes_the_key() -> None:
    event = gitlab_label_event(delivery_id="gl-uuid-1", account_id=str(uuid.uuid4()))
    assert delivery_key_for_event(event) == f"{DELIVERY_PREFIX}gl-uuid-1"


def test_missing_delivery_id_falls_back_to_a_content_fingerprint() -> None:
    account = str(uuid.uuid4())
    tracker = str(uuid.uuid4())
    first = github_label_event(delivery_id=None, account_id=account, tracker_id=tracker)
    second = github_label_event(
        delivery_id=None, account_id=account, tracker_id=tracker
    )
    key = delivery_key_for_event(first)

    assert key.startswith(CONTENT_PREFIX)
    assert key == delivery_key_for_event(second)


def test_content_fingerprint_separates_label_delta_and_resource() -> None:
    account = str(uuid.uuid4())
    tracker = str(uuid.uuid4())
    base = github_label_event(delivery_id=None, account_id=account, tracker_id=tracker)

    other_label = github_label_event(
        delivery_id=None, account_id=account, tracker_id=tracker
    )
    other_label["payload"]["added_labels"] = ["needs-triage"]
    other_issue = github_label_event(
        delivery_id=None,
        account_id=account,
        tracker_id=tracker,
        issue_number=507,
    )

    assert content_fingerprint(base) != content_fingerprint(other_label)
    assert content_fingerprint(base) != content_fingerprint(other_issue)


def test_sources_with_their_own_coalescing_are_left_alone() -> None:
    for source in ("webhook", "schedule", "manual"):
        assert (
            delivery_key_for_event(
                {"source": source, "type": source, "payload": {"a": 1}}
            )
            is None
        )


def test_event_without_any_identity_gets_no_key() -> None:
    assert (
        delivery_key_for_event({"source": "github", "type": "ping", "payload": {}})
        is None
    )
    assert delivery_key_for_event({}) is None


# --------------------------------------------------------------------------
# Database-backed behaviour
# --------------------------------------------------------------------------


@pytest.fixture
def flow(db_session: Session, test_user) -> models.Flow:
    row = models.Flow(
        name="Issue implementation",
        prompt_template="implement {{issue}}",
        agent_type="codex",
        agent_config={},
        account_id=test_user.account_id,
        trigger_event_source="github",
        trigger_event_types=["issue_labeled"],
        is_enabled=True,
    )
    db_session.add(row)
    db_session.flush()
    return row


def executions_for(db: Session, flow_id) -> list[models.FlowExecution]:
    return (
        db.query(models.FlowExecution)
        .filter(models.FlowExecution.flow_id == flow_id)
        .all()
    )


async def deliver(service: FlowTriggerService, flow: models.Flow, event: dict) -> None:
    """Run ``process_event`` with dispatch stubbed out, as the worker would."""
    with (
        patch(
            "preloop.services.flow_trigger_service.crud_flow.get_by_trigger",
            return_value=[flow],
        ),
        patch(
            "preloop.services.flow_trigger_service.get_nats_client",
            new=AsyncMock(return_value=None),
        ),
        patch("preloop.services.flow_trigger_service.asyncio.create_task"),
    ):
        await service.process_event(dict(event))


@pytest.mark.asyncio
async def test_redelivered_github_message_creates_exactly_one_execution(
    db_session: Session, flow: models.Flow, test_user
) -> None:
    service = FlowTriggerService(db_session)
    event = github_label_event(account_id=str(test_user.account_id))

    await deliver(service, flow, event)
    await deliver(service, flow, event)
    await deliver(service, flow, event)

    rows = executions_for(db_session, flow.id)
    assert len(rows) == 1
    assert rows[0].webhook_delivery_key == f"{DELIVERY_PREFIX}{DELIVERY_ID}"
    assert rows[0].trigger_event_details["delivery_id"] == DELIVERY_ID


@pytest.mark.asyncio
async def test_a_second_distinct_delivery_still_runs(
    db_session: Session, flow: models.Flow, test_user
) -> None:
    service = FlowTriggerService(db_session)
    account = str(test_user.account_id)

    await deliver(service, flow, github_label_event(account_id=account))
    await deliver(
        service,
        flow,
        github_label_event(delivery_id="second-delivery", account_id=account),
    )

    keys = {row.webhook_delivery_key for row in executions_for(db_session, flow.id)}
    assert keys == {
        f"{DELIVERY_PREFIX}{DELIVERY_ID}",
        f"{DELIVERY_PREFIX}second-delivery",
    }


@pytest.mark.asyncio
async def test_redelivered_gitlab_message_creates_exactly_one_execution(
    db_session: Session, flow: models.Flow, test_user
) -> None:
    service = FlowTriggerService(db_session)
    event = gitlab_label_event(
        delivery_id="97f2b3d1-uuid", account_id=str(test_user.account_id)
    )

    await deliver(service, flow, event)
    await deliver(service, flow, event)

    rows = executions_for(db_session, flow.id)
    assert len(rows) == 1
    assert rows[0].webhook_delivery_key == f"{DELIVERY_PREFIX}97f2b3d1-uuid"


@pytest.mark.asyncio
async def test_delivery_without_an_id_is_deduplicated_on_content(
    db_session: Session, flow: models.Flow, test_user
) -> None:
    service = FlowTriggerService(db_session)
    event = github_label_event(delivery_id=None, account_id=str(test_user.account_id))

    await deliver(service, flow, event)
    await deliver(service, flow, event)

    rows = executions_for(db_session, flow.id)
    assert len(rows) == 1
    assert rows[0].webhook_delivery_key.startswith(CONTENT_PREFIX)


def test_content_key_lookup_is_bounded_to_the_redelivery_window(
    db_session: Session, flow: models.Flow
) -> None:
    """A fingerprint repeats legitimately; only a fresh one is a redelivery."""
    key = f"{CONTENT_PREFIX}" + "a" * 32
    old = models.FlowExecution(
        flow_id=flow.id,
        status="SUCCEEDED",
        webhook_delivery_key=key,
        start_time=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2),
    )
    db_session.add(old)
    db_session.flush()

    assert (
        find_execution_for_delivery(db_session, flow_id=flow.id, delivery_key=key)
        is None
    )

    recent = models.FlowExecution(
        flow_id=flow.id,
        status="RUNNING",
        webhook_delivery_key=key,
        start_time=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=30),
    )
    db_session.add(recent)
    db_session.flush()

    found = find_execution_for_delivery(db_session, flow_id=flow.id, delivery_key=key)
    assert found is not None and found.id == recent.id


def test_legacy_row_with_the_id_only_in_jsonb_is_still_found(
    db_session: Session, flow: models.Flow
) -> None:
    """Rows written before the column existed (or by precreate paths) count.

    This is what makes the guard work across the very deploy that introduces
    it: the old pod's row has no ``webhook_delivery_key``.
    """
    legacy = models.FlowExecution(
        flow_id=flow.id,
        status="RUNNING",
        trigger_event_details={"source": "github", "delivery_id": DELIVERY_ID},
    )
    db_session.add(legacy)
    db_session.flush()

    found = find_execution_for_delivery(
        db_session, flow_id=flow.id, delivery_key=f"{DELIVERY_PREFIX}{DELIVERY_ID}"
    )
    assert found is not None and found.id == legacy.id


@pytest.mark.asyncio
async def test_a_retry_of_the_same_delivery_is_still_allowed(
    db_session: Session, flow: models.Flow, test_user
) -> None:
    """Retrying a run is deliberate: it must not collide with the original."""
    service = FlowTriggerService(db_session)
    event = github_label_event(account_id=str(test_user.account_id))
    await deliver(service, flow, event)
    original = executions_for(db_session, flow.id)[0]

    with (
        patch(
            "preloop.services.flow_trigger_service.get_nats_client",
            new=AsyncMock(return_value=None),
        ),
        patch("preloop.services.flow_trigger_service.asyncio.create_task"),
    ):
        retry = await service._start_flow_execution(
            flow=flow,
            event_data=dict(event),
            nats_client=None,
            retry_of_execution_id=original.id,
        )

    assert retry.id != original.id
    assert retry.webhook_delivery_key is None
    assert len(executions_for(db_session, flow.id)) == 2


def test_unique_index_rejects_a_second_row_for_the_same_delivery(
    db_session: Session, flow: models.Flow
) -> None:
    key = f"{DELIVERY_PREFIX}{DELIVERY_ID}"
    db_session.add(
        models.FlowExecution(
            flow_id=flow.id, status="PENDING", webhook_delivery_key=key
        )
    )
    db_session.flush()

    db_session.add(
        models.FlowExecution(
            flow_id=flow.id, status="PENDING", webhook_delivery_key=key
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_unique_index_is_scoped_to_the_flow_and_to_delivery_keys(
    db_session: Session, flow: models.Flow, test_user
) -> None:
    other_flow = models.Flow(
        name="Second flow",
        prompt_template="x",
        agent_type="codex",
        agent_config={},
        account_id=test_user.account_id,
    )
    db_session.add(other_flow)
    db_session.flush()

    key = f"{DELIVERY_PREFIX}{DELIVERY_ID}"
    content_key = f"{CONTENT_PREFIX}" + "b" * 32
    db_session.add_all(
        [
            models.FlowExecution(
                flow_id=flow.id, status="PENDING", webhook_delivery_key=key
            ),
            # Same delivery, different flow: both flows must run.
            models.FlowExecution(
                flow_id=other_flow.id, status="PENDING", webhook_delivery_key=key
            ),
            # Content fingerprints repeat over time and are not constrained.
            models.FlowExecution(
                flow_id=flow.id, status="PENDING", webhook_delivery_key=content_key
            ),
            models.FlowExecution(
                flow_id=flow.id, status="PENDING", webhook_delivery_key=content_key
            ),
        ]
    )
    db_session.flush()

    assert len(executions_for(db_session, flow.id)) == 3


def test_two_concurrent_sessions_create_exactly_one_execution(
    db_engine, db_session: Session, test_user
) -> None:
    """Two pods holding the same redelivered message, on real connections.

    The conftest session runs inside a rolled-back transaction, so this test
    owns its committed rows and cleans them up itself.
    """
    account_id = test_user.account_id
    setup = Session(bind=db_engine)
    flow_id = None
    try:
        account = setup.execute(
            text("SELECT id FROM account WHERE id = :id"), {"id": account_id}
        ).first()
        if account is None:
            # test_user lives in the rolled-back transaction; make a real one.
            from preloop.models.crud import crud_account

            created = crud_account.create(
                setup, obj_in={"organization_name": "Race test", "is_active": True}
            )
            setup.flush()
            account_id = created.id
        row = models.Flow(
            name="Race flow",
            prompt_template="implement",
            agent_type="codex",
            agent_config={},
            account_id=account_id,
            trigger_event_source="github",
            trigger_event_types=["issue_labeled"],
            is_enabled=True,
        )
        setup.add(row)
        setup.commit()
        flow_id = row.id

        event = github_label_event(account_id=str(account_id))
        barrier = threading.Barrier(2)
        results: list[object] = []

        def worker() -> None:
            session = Session(bind=db_engine)
            try:
                service = FlowTriggerService(session)
                flow_row = session.get(models.Flow, flow_id)
                barrier.wait(timeout=10)
                with (
                    patch(
                        "preloop.services.flow_trigger_service.get_nats_client",
                        new=AsyncMock(return_value=None),
                    ),
                    patch("preloop.services.flow_trigger_service.asyncio.create_task"),
                ):
                    results.append(
                        asyncio.run(
                            service._start_flow_execution(
                                flow=flow_row,
                                event_data=dict(event),
                                nats_client=None,
                            )
                        )
                    )
            finally:
                session.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        check = Session(bind=db_engine)
        try:
            rows = executions_for(check, flow_id)
        finally:
            check.close()

        assert len(results) == 2, "both racing callers must return an execution"
        assert len(rows) == 1
        assert rows[0].webhook_delivery_key == f"{DELIVERY_PREFIX}{DELIVERY_ID}"
        assert {str(getattr(result, "id", None)) for result in results} == {
            str(rows[0].id)
        }
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
            if account_id != test_user.account_id:
                cleanup.execute(
                    text("DELETE FROM account WHERE id = :id"), {"id": account_id}
                )
            cleanup.commit()
        finally:
            cleanup.close()
        setup.close()


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------


def load_migration():
    """Import the migration module by path (its name starts with digits)."""
    spec = importlib.util.spec_from_file_location(
        "webhook_delivery_key_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def column_exists(db_session: Session) -> bool:
    return bool(
        db_session.execute(
            text(
                "SELECT 1 FROM information_schema.columns WHERE "
                "table_name = 'flow_execution' AND "
                "column_name = 'webhook_delivery_key'"
            )
        ).first()
    )


def index_names(db_session: Session) -> set[str]:
    return {
        name
        for (name,) in db_session.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'flow_execution'")
        )
    }


def test_migration_downgrade_then_upgrade_is_reversible(db_session: Session) -> None:
    """Run the real down/up inside the test transaction (rolled back after)."""
    migration = load_migration()
    context = MigrationContext.configure(db_session.connection())
    expected = {
        migration.UNIQUE_INDEX,
        migration.CONTENT_INDEX,
        migration.LEGACY_INDEX,
    }

    assert column_exists(db_session)
    assert expected <= index_names(db_session)

    with Operations.context(context):
        migration.downgrade()
    assert not column_exists(db_session)
    assert not (expected & index_names(db_session))

    with Operations.context(context):
        migration.upgrade()
    assert column_exists(db_session)
    assert expected <= index_names(db_session)
